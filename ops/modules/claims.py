"""Claims module — the invoicing fact table (STP-2).

Replaces the Invoicing and Future Invoicing tabs. The monthly copy-forward
disappears because a row does not move between tabs; it changes status.

**EOM is the organising axis.** We invoice monthly, so a claim is assigned
to the end-of-month it targets, and everything — the review grid, the
forecast — reads along that axis. `period_id` is therefore mandatory on
every claim except opening balances, which sit at Jun-26 by definition.

`period_id` is the month a claim is ASSIGNED to; `invoiced_date` is when the
invoice actually went out. Those differ by design — invoices are raised in
Xero from about the 18th for that EOM — and the gap is reportable rather
than hidden.
"""

from typing import Any

from ops.db import Db
from ops.http_util import HttpError, Router

ROLE_READ = "viewer"
ROLE_WRITE = "operations"
ROLE_APPROVE = "approver"

STATUSES = ("forecast", "due", "approved", "invoiced", "paid", "cancelled")

# Legal moves, stated as data so they can be read rather than traced through
# branches. Backward moves exist -- work slips, invoices get voided -- but
# they are never accidental.
TRANSITIONS = {
    "forecast":  {"due", "cancelled"},
    "due":       {"approved", "forecast", "cancelled"},
    "approved":  {"invoiced", "due", "cancelled"},
    "invoiced":  {"paid", "approved"},
    "paid":      {"invoiced"},
    "cancelled": {"forecast"},
}

# Stepping back out of these means undoing something that exists in Xero, so
# it takes an approver and a reason. Everything earlier just takes a reason.
NEEDS_APPROVER = {("invoiced", "approved"), ("paid", "invoiced")}

# What a status requires before it can be entered.
REQUIRED_ON_ENTRY = {
    "approved": ("approved_date",),
    "invoiced": ("invoice_number", "invoiced_date"),
    "paid": ("paid_date",),
}

CLAIM_SELECT = """
    SELECT cl.id, cl.project_id, cl.customer_po_id, cl.period_id, cl.status,
           cl.amount_cents, cl.retention_cents, cl.percent_bp,
           cl.phase, cl.task, cl.detail, cl.reference,
           cl.claim_date, cl.approved_date, cl.invoice_number,
           cl.invoiced_date, cl.paid_date,
           cl.is_opening_balance, cl.is_retention_release,
           p.name AS project_name, p.job_code,
           po.po_number, po.is_placeholder AS po_is_placeholder,
           pe.label AS period_label, pe.fy_label, pe.month_end,
           pe.fy, pe.month_start,
           COALESCE(pt.code, '(untyped)') AS type,
           -- Retention as a STATE, because an amount cannot be filtered:
           -- a multiselect of "$1,896.00, $2,500.00, ..." is unusable.
           -- Three values, because "applies but nothing withheld yet" is
           -- exactly what you look for when forecasting cash.
           CASE
             WHEN cl.retention_cents > 0 THEN 'Withheld'
             WHEN EXISTS (SELECT 1 FROM customer_po po2
                          WHERE po2.project_id = cl.project_id
                            AND po2.retention_applies = 1) THEN 'Applies'
             ELSE 'None'
           END AS retention_state,
           COALESCE(c.name, '(no client)') AS client
    FROM claim_line cl
    JOIN project p ON p.id = cl.project_id
    LEFT JOIN customer_po po ON po.id = cl.customer_po_id
    LEFT JOIN period pe ON pe.id = cl.period_id
    LEFT JOIN project_type pt ON pt.id = p.type_id
    LEFT JOIN client c ON c.id = p.client_id
"""


def entity_ids(user: dict[str, Any]) -> list[int]:
    return sorted({r["entity_id"] for r in user["roles"]})


def has_role(user: dict[str, Any], role: str) -> bool:
    return any(r["role"] == role for r in user.get("roles", []))


def validate(db: Db, payload: dict[str, Any], entity_id: int,
             existing: dict[str, Any] | None = None):
    errors: dict[str, str] = {}
    fields: dict[str, Any] = {}
    creating = existing is None

    if creating or "project_id" in payload:
        row = db.query_one(
            "SELECT id, entity_id FROM project WHERE id = ?",
            (payload.get("project_id"),))
        if row is None or row["entity_id"] != entity_id:
            errors["project_id"] = "required; a project on this entity"
        else:
            fields["project_id"] = row["id"]

    # Mandatory, and not merely nullable-by-accident: a claim with no EOM
    # cannot appear in the month it belongs to, which is the only view
    # anyone works from.
    if creating or "period_id" in payload:
        row = db.query_one("SELECT id FROM period WHERE id = ?",
                           (payload.get("period_id"),))
        if row is None:
            errors["period_id"] = "required; the end-of-month this targets"
        else:
            fields["period_id"] = row["id"]

    if creating or "customer_po_id" in payload:
        po_id = payload.get("customer_po_id")
        if po_id:
            project_id = fields.get("project_id") or (
                existing["project_id"] if existing else None)
            row = db.query_one(
                "SELECT id FROM customer_po WHERE id = ? AND project_id = ?",
                (po_id, project_id))
            if row is None:
                errors["customer_po_id"] = "not a PO on this project"
            else:
                fields["customer_po_id"] = row["id"]
        elif creating:
            # Only an opening balance may float free of a PO (ADR-22), and
            # those are made by migration, never here.
            errors["customer_po_id"] = "required; which PO is this claimed against"

    if creating or "amount_cents" in payload:
        try:
            amount = int(payload.get("amount_cents", 0))
        except (TypeError, ValueError):
            errors["amount_cents"] = "must be a whole number of cents"
            amount = 0
        if amount == 0:
            errors["amount_cents"] = "required"
        elif amount < 0:
            errors["amount_cents"] = "cannot be negative; cancel the claim instead"
        else:
            fields["amount_cents"] = amount

    if creating:
        status = payload.get("status", "forecast")
        if status not in ("forecast", "due"):
            # A claim starts as intent. Arriving already invoiced would skip
            # every check between the two.
            errors["status"] = "a new claim starts as forecast or due"
        else:
            fields["status"] = status

    for key in ("percent_bp", "phase", "task", "detail", "reference",
                "claim_date"):
        if key in payload:
            fields[key] = payload[key] or None

    return fields, errors


def register(router: Router, db: Db) -> None:

    def scoped(user):
        ids = entity_ids(user)
        if not ids:
            raise HttpError(403, "no entity access")
        return ids

    @router.route("/api/periods", role=ROLE_READ)
    def periods(handler, user):
        """The EOM axis. Everything is grouped or filtered by it."""
        return 200, {"periods": db.query(
            """SELECT id, fy, fy_label, month_no, label, month_start, month_end
               FROM period WHERE fy BETWEEN 2026 AND 2030
               ORDER BY month_start""")}

    @router.route("/api/claims", role=ROLE_READ)
    def list_claims(handler, user):
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(handler.path).query)
        ids = scoped(user)
        where = [f"p.entity_id IN ({','.join('?' * len(ids))})"]
        params: list[Any] = list(ids)
        if q.get("period"):
            where.append("cl.period_id = ?")
            params.append(q["period"][0])
        # A financial year, not a month: forecasting means looking across
        # months and moving work between them, which one month at a time
        # cannot show.
        if q.get("fy"):
            where.append("pe.fy = ?")
            params.append(q["fy"][0])
        if q.get("project"):
            where.append("cl.project_id = ?")
            params.append(q["project"][0])
        if q.get("status"):
            marks = ",".join("?" * len(q["status"]))
            where.append(f"cl.status IN ({marks})")
            params.extend(q["status"])
        rows = db.query(
            f"{CLAIM_SELECT} WHERE {' AND '.join(where)} "
            "ORDER BY pe.month_start, p.name", tuple(params))
        # Retention HELD is a position, not a period figure: withheld less
        # released, for the projects in view. The per-claim column already
        # shows what this month withheld, and conflating the two would put a
        # cumulative number under a monthly heading.
        held = 0
        project_ids = {r["project_id"] for r in rows}
        if project_ids:
            marks = ",".join("?" * len(project_ids))
            held = db.scalar(
                f"""SELECT COALESCE(SUM(held_cents), 0) FROM v_project_retention
                    WHERE project_id IN ({marks})""", tuple(project_ids)) or 0
        # Held per project, so the grid can sum it over whatever the
        # filters leave without counting a project once per claim.
        by_project = {}
        if project_ids:
            marks2 = ",".join("?" * len(project_ids))
            by_project = {r["project_id"]: r["held_cents"] for r in db.query(
                f"""SELECT project_id, held_cents FROM v_project_retention
                    WHERE project_id IN ({marks2}) AND held_cents <> 0""",
                tuple(project_ids))}
        return 200, {
            "claims": rows,
            "retention_by_project": by_project,
            "totals": {
                s: sum(r["amount_cents"] for r in rows if r["status"] == s)
                for s in STATUSES
            },
            "retention_withheld_cents": sum(r["retention_cents"] for r in rows),
            "retention_held_cents": held,
            "transitions": {k: sorted(v) for k, v in TRANSITIONS.items()},
        }

    @router.route("/api/claims", role=ROLE_WRITE, method="POST")
    def create_claim(handler, user):
        payload = handler.read_json()
        ids = scoped(user)
        entity_id = payload.get("entity_id") or ids[0]
        if entity_id not in ids:
            raise HttpError(403, "no access to that entity")
        fields, errors = validate(db, payload, entity_id)
        if errors:
            raise HttpError(400, "validation failed", errors)
        fields["entity_id"] = entity_id
        return 201, db.create_claim_line(fields, user["id"])

    @router.route("/api/claims/{claim_id}", role=ROLE_WRITE, method="PATCH")
    def update_claim(handler, user, claim_id):
        ids = scoped(user)
        row = db.query_one(
            """SELECT cl.*, p.entity_id AS project_entity FROM claim_line cl
               JOIN project p ON p.id = cl.project_id WHERE cl.id = ?""",
            (claim_id,))
        if row is None or row["project_entity"] not in ids:
            raise HttpError(404, "not found")
        if row["is_opening_balance"]:
            raise HttpError(409, "opening balances are immutable; they are the "
                                 "boundary of what this platform knows")
        payload = handler.read_json()
        unknown = [k for k in payload
                   if k not in db.CLAIM_MUTABLE and k != "reason"]
        if unknown:
            raise HttpError(400, "validation failed",
                            {k: "not an editable field" for k in unknown})
        fields, errors = validate(db, payload, row["project_entity"],
                                  existing=dict(row))
        if errors:
            raise HttpError(400, "validation failed", errors)

        # Moving a FORECAST claim between months is planning, not slippage --
        # it is the whole activity. Requiring a justification for each one
        # would make re-forecasting unusable, which is the opposite of the
        # point. Once a claim is due or approved it has been committed to a
        # month, and moving it IS slippage: that needs a reason, because a
        # forecast quietly rewritten always looks like it was right.
        moving = ("period_id" in fields
                  and fields["period_id"] != row["period_id"])
        if (moving and row["status"] != "forecast"
                and not (payload.get("reason") or "").strip()):
            raise HttpError(400, "validation failed", {
                "reason": f"this claim is {row['status']}, so moving it is "
                          "slippage; say why"})

        result = db.update_claim_line(claim_id, fields, user["id"],
                                      (payload.get("reason") or "").strip() or None)
        if result is None:
            raise HttpError(404, "not found")
        return 200, result

    @router.route("/api/claims/{claim_id}/status", role=ROLE_WRITE,
                  method="POST")
    def move_claim(handler, user, claim_id):
        ids = scoped(user)
        row = db.query_one(
            """SELECT cl.*, p.entity_id AS project_entity FROM claim_line cl
               JOIN project p ON p.id = cl.project_id WHERE cl.id = ?""",
            (claim_id,))
        if row is None or row["project_entity"] not in ids:
            raise HttpError(404, "not found")
        if row["is_opening_balance"]:
            raise HttpError(409, "opening balances are immutable")

        payload = handler.read_json()
        to_status = payload.get("status")
        reason = (payload.get("reason") or "").strip()
        allowed = TRANSITIONS.get(row["status"], set())
        if to_status not in STATUSES:
            raise HttpError(400, "validation failed",
                            {"status": f"must be one of {', '.join(STATUSES)}"})
        if to_status not in allowed:
            raise HttpError(409,
                            f"a {row['status']} claim cannot go straight to "
                            f"{to_status}; allowed: {', '.join(sorted(allowed))}")

        move = (row["status"], to_status)
        if move in NEEDS_APPROVER:
            # Undoing something that exists in Xero.
            if not has_role(user, ROLE_APPROVE):
                raise HttpError(403, f"moving {row['status']} back to "
                                     f"{to_status} needs the approver role")
            if not reason:
                raise HttpError(400, "validation failed",
                                {"reason": "required when reversing an invoice"})
        backward = to_status in ("forecast", "due", "approved") and \
            STATUSES.index(to_status) < STATUSES.index(row["status"])
        if backward and not reason:
            raise HttpError(400, "validation failed",
                            {"reason": "required when moving a claim backwards"})

        missing = {k: "required to enter this status"
                   for k in REQUIRED_ON_ENTRY.get(to_status, ())
                   if not (payload.get(k) or row[k])}
        if missing:
            raise HttpError(400, "validation failed", missing)

        result = db.transition_claim(
            claim_id, to_status,
            {k: payload[k] for k in REQUIRED_ON_ENTRY.get(to_status, ())
             if k in payload},
            reason or None, user["id"])
        if result is None:
            raise HttpError(404, "not found")
        return 200, result

    @router.route("/api/claims/{claim_id}/po", role=ROLE_WRITE, method="POST")
    def po_for_claim(handler, user, claim_id):
        """Create the order this claim bills against, and attach it.

        Some jobs raise a PO per invoice, so this happens constantly and
        should not mean leaving the month view to go and find the project.
        The amount defaults to the claim's but is editable: one order
        commonly covers several claims.
        """
        ids = scoped(user)
        row = db.query_one(
            """SELECT cl.*, p.entity_id AS project_entity FROM claim_line cl
               JOIN project p ON p.id = cl.project_id WHERE cl.id = ?""",
            (claim_id,))
        if row is None or row["project_entity"] not in ids:
            raise HttpError(404, "not found")
        payload = handler.read_json()
        number = (payload.get("po_number") or "").strip()
        errors = {}
        try:
            amount = int(payload.get("amount_cents", row["amount_cents"]))
        except (TypeError, ValueError):
            amount = 0
            errors["amount_cents"] = "must be a whole number of cents"
        if amount < 0:
            errors["amount_cents"] = "cannot be negative"
        if number:
            clash = db.query_one(
                """SELECT p.name FROM customer_po po
                   JOIN project p ON p.id = po.project_id
                   WHERE po.po_number = ?""", (number,))
            if clash is not None:
                errors["po_number"] = f"already used on {clash['name']}"
        if errors:
            raise HttpError(400, "validation failed", errors)
        po = db.create_customer_po({
            "entity_id": row["project_entity"], "project_id": row["project_id"],
            "po_number": number or None, "amount_cents": amount,
            "issued_date": (payload.get("issued_date") or "").strip() or None,
            "note": (payload.get("note") or "").strip() or None,
        }, user["id"])
        result = db.update_claim_line(
            claim_id, {"customer_po_id": po["id"]}, user["id"],
            "order raised for this claim")
        return 201, {"po": po, "claim": result["claim"] if result else None}

    @router.route("/api/claims/{claim_id}/history", role=ROLE_READ)
    def history(handler, user, claim_id):
        ids = scoped(user)
        row = db.query_one(
            """SELECT p.entity_id FROM claim_line cl
               JOIN project p ON p.id = cl.project_id WHERE cl.id = ?""",
            (claim_id,))
        if row is None or row["entity_id"] not in ids:
            raise HttpError(404, "not found")
        return 200, {"revisions": db.query(
            """SELECT r.field, r.old_value, r.new_value, r.reason,
                      r.changed_ts, u.display_name AS changed_by
               FROM claim_line_revision r
               LEFT JOIN users u ON u.id = r.changed_by
               WHERE r.claim_line_id = ? ORDER BY r.changed_ts, r.id""",
            (claim_id,))}
