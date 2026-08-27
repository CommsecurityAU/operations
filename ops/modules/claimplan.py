"""Claim planning module (STP-2).

How a contract becomes a forecast, replacing the progress-claim workbooks.

  A contract splits into ITEMS with values.
  Each item is spread across months by PERCENTAGE.
  A month's claim is the SUM of that month's contributions.

Three things the workbooks check by hand and this checks continuously:
items sum to the contract, each item's allocations reach 100%, and the
cumulative claim lands on exactly 100%. None of them is enforced as a
constraint — a plan under construction is legitimately incomplete, and
refusing to save it would mean building the whole thing in one sitting.
They are REPORTED, so the gap is visible while you work.
"""

from typing import Any

from ops.db import Db
from ops.http_util import HttpError, Router

ROLE_READ = "viewer"
ROLE_WRITE = "operations"
ROLE_AMEND = "approver"

MAX_MONEY_CENTS = 100_000_000_00


def entity_ids(user: dict[str, Any]) -> list[int]:
    return sorted({r["entity_id"] for r in user["roles"]})


def has_role(user: dict[str, Any], role: str) -> bool:
    return any(r["role"] == role for r in user.get("roles", []))


def money_field(raw, errors, key, allow_zero=True):
    try:
        amount = int(raw)
    except (TypeError, ValueError):
        errors[key] = "must be a whole number of cents"
        return 0
    if amount < 0:
        errors[key] = "cannot be negative"
    elif not allow_zero and amount == 0:
        errors[key] = "required"
    elif amount > MAX_MONEY_CENTS:
        errors[key] = f"looks like a typo (over ${MAX_MONEY_CENTS // 100:,})"
    return amount


def register(router: Router, db: Db) -> None:

    def owned_project(user, project_id):
        row = db.query_one(
            "SELECT id, entity_id, name FROM project WHERE id = ?",
            (project_id,))
        if row is None or row["entity_id"] not in entity_ids(user):
            raise HttpError(404, "not found")
        return row

    def owned_item(user, item_id):
        row = db.query_one(
            """SELECT i.*, p.name AS project_name FROM claim_item i
               JOIN project p ON p.id = i.project_id WHERE i.id = ?""",
            (item_id,))
        if row is None or row["entity_id"] not in entity_ids(user):
            raise HttpError(404, "not found")
        return row

    @router.route("/api/projects/{project_id}/plan", role=ROLE_READ)
    def get_plan(handler, user, project_id):
        project = owned_project(user, project_id)
        health = db.plan_health(project["id"])
        health["project_name"] = project["name"]
        # Claims that exist with no plan behind them. The panel has to say
        # so rather than reporting an empty plan as an empty project.
        health["unplanned_claims"] = db.query_one(
            """SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS cents
               FROM claim_line
               WHERE project_id = ? AND is_opening_balance = 0
                 AND from_plan = 0 AND period_id IS NOT NULL""",
            (project["id"],))
        # Every allocation, so the grid can be drawn without a request per
        # cell.
        health["allocations"] = db.query(
            """SELECT a.id, a.claim_item_id, a.period_id, a.percent_bp,
                      a.amount_cents, a.note,
                      a.locked_claim_id IS NOT NULL AS is_locked,
                      pe.label, pe.month_start, pe.fy_label
               FROM claim_allocation a
               JOIN claim_item i ON i.id = a.claim_item_id
               JOIN period pe ON pe.id = a.period_id
               WHERE i.project_id = ? ORDER BY pe.month_start, i.sequence""",
            (project["id"],))
        health["claims"] = db.query(
            """SELECT cl.id, cl.period_id, cl.status, cl.amount_cents,
                      cl.invoice_number, cl.from_plan, pe.label
               FROM claim_line cl JOIN period pe ON pe.id = cl.period_id
               WHERE cl.project_id = ? AND cl.is_opening_balance = 0
               ORDER BY pe.month_start""", (project["id"],))
        return 200, health

    @router.route("/api/projects/{project_id}/plan/items", role=ROLE_WRITE,
                  method="POST")
    def add_item(handler, user, project_id):
        project = owned_project(user, project_id)
        payload = handler.read_json()
        errors = {}
        name = (payload.get("name") or "").strip()
        if not name:
            # It becomes the label on every claim it contributes to.
            errors["name"] = "required; it names this part of the contract"
        value = money_field(payload.get("value_cents"), errors, "value_cents",
                            allow_zero=False)
        if errors:
            raise HttpError(400, "validation failed", errors)
        return 201, db.create_claim_item({
            "entity_id": project["entity_id"], "project_id": project["id"],
            "name": name, "value_cents": value,
            "is_variation": 1 if payload.get("is_variation") else 0,
            "note": (payload.get("note") or "").strip() or None,
        }, user["id"])

    @router.route("/api/plan/items/{item_id}", role=ROLE_WRITE, method="PATCH")
    def update_item(handler, user, item_id):
        item = owned_item(user, item_id)
        payload = handler.read_json()
        unknown = [k for k in payload if k not in db.ITEM_MUTABLE]
        if unknown:
            raise HttpError(400, "validation failed",
                            {k: "not an editable field" for k in unknown})
        errors = {}
        fields = {}
        if "name" in payload:
            name = (payload["name"] or "").strip()
            if not name:
                errors["name"] = "required"
            else:
                fields["name"] = name
        if "value_cents" in payload:
            fields["value_cents"] = money_field(
                payload["value_cents"], errors, "value_cents", allow_zero=False)
        for key in ("sequence", "is_variation"):
            if key in payload:
                fields[key] = int(bool(payload[key])) if key == "is_variation" \
                    else int(payload[key])
        if "note" in payload:
            fields["note"] = (payload["note"] or "").strip() or None
        if errors:
            raise HttpError(400, "validation failed", errors)
        result = db.update_claim_item(item["id"], fields, user["id"])
        if result is None:
            raise HttpError(404, "not found")
        return 200, result

    @router.route("/api/plan/items/{item_id}", role=ROLE_WRITE, method="DELETE")
    def delete_item(handler, user, item_id):
        item = owned_item(user, item_id)
        blocked = db.claim_item_is_deletable(item["id"])
        if blocked:
            raise HttpError(409, blocked)
        db.delete_claim_item(item["id"], user["id"])
        handler._send(204, b"", content_type="application/json")
        return None

    @router.route("/api/plan/items/{item_id}/allocate", role=ROLE_WRITE,
                  method="POST")
    def allocate(handler, user, item_id):
        """One item's share of one month.

        Both the percentage and the amount are sent: the amount is the fact
        and the percentage is how it was expressed. `33.33%` of $79,444 is
        $26,478.69 while the agreed figure is $26,481.33 — a third,
        displayed rounded — so deriving one from the other would move money.
        """
        item = owned_item(user, item_id)
        payload = handler.read_json()
        errors = {}
        period_id = db.scalar("SELECT id FROM period WHERE id = ?",
                              (payload.get("period_id"),))
        if period_id is None:
            errors["period_id"] = "required"
        amount = money_field(payload.get("amount_cents"), errors, "amount_cents")
        try:
            percent = int(payload.get("percent_bp", 0))
        except (TypeError, ValueError):
            errors["percent_bp"] = "must be basis points"
            percent = 0
        if errors:
            raise HttpError(400, "validation failed", errors)
        try:
            result = db.set_allocation(
                item["id"], period_id, percent, amount, user["id"],
                (payload.get("note") or "").strip() or None)
        except ValueError as e:
            raise HttpError(409, str(e))
        return 200, result

    @router.route("/api/projects/{project_id}/plan/adopt", role=ROLE_WRITE,
                  method="POST")
    def adopt(handler, user, project_id):
        """Build the plan from the claims already there.

        A panel saying `no plan yet` beside thirteen forecast claims is
        lying by omission, and asking for the same forecast to be typed
        twice is how a tool stops being used.
        """
        project = owned_project(user, project_id)
        payload = handler.read_json() if handler.headers.get("Content-Length") \
            else {}
        return 200, db.adopt_claims_into_plan(
            project["id"], user["id"], rebuild=bool(payload.get("rebuild")))

    @router.route("/api/projects/{project_id}/plan/generate", role=ROLE_WRITE,
                  method="POST")
    def generate(handler, user, project_id):
        project = owned_project(user, project_id)
        payload = handler.read_json() if handler.headers.get("Content-Length") \
            else {}
        return 200, db.generate_plan_claims(
            project["id"], user["id"], payload.get("customer_po_id"))

    @router.route("/api/claims/{claim_id}/amend", role=ROLE_AMEND,
                  method="POST")
    def amend(handler, user, claim_id):
        """Change a claim that has already been invoiced. Rare, and real.

        Approver only: the figure has left the building, and someone outside
        this system has it.
        """
        ids = entity_ids(user)
        row = db.query_one(
            """SELECT cl.id, p.entity_id FROM claim_line cl
               JOIN project p ON p.id = cl.project_id WHERE cl.id = ?""",
            (claim_id,))
        if row is None or row["entity_id"] not in ids:
            raise HttpError(404, "not found")
        payload = handler.read_json()
        errors = {}
        amount = money_field(payload.get("amount_cents"), errors, "amount_cents")
        if not (payload.get("reason") or "").strip():
            errors["reason"] = "required; the invoice said something else"
        if errors:
            raise HttpError(400, "validation failed", errors)
        try:
            result = db.amend_invoiced_claim(
                row["id"], amount, payload["reason"], user["id"])
        except ValueError as e:
            raise HttpError(409, str(e))
        if result is None:
            raise HttpError(404, "not found")
        return 200, result

    @router.route("/api/claims/{claim_id}/amendments", role=ROLE_READ)
    def amendments(handler, user, claim_id):
        ids = entity_ids(user)
        row = db.query_one(
            """SELECT p.entity_id FROM claim_line cl
               JOIN project p ON p.id = cl.project_id WHERE cl.id = ?""",
            (claim_id,))
        if row is None or row["entity_id"] not in ids:
            raise HttpError(404, "not found")
        return 200, {"amendments": db.query(
            """SELECT a.invoice_number, a.invoiced_cents, a.amended_cents,
                      a.reason, a.amended_ts, u.display_name AS amended_by
               FROM claim_amendment a
               LEFT JOIN users u ON u.id = a.amended_by
               WHERE a.claim_line_id = ? ORDER BY a.amended_ts""", (claim_id,))}
