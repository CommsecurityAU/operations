"""Procurement module (STP-3).

The register a project engineer fills in and accounts works from, with the
two things the sheet cannot do: recording WHEN something was delivered or
paid rather than what state someone last typed, and showing committed cost
against the project it belongs to.

Payment and delivery are independent facts, so they are dates and the state
is derived. `Paid - Pending Delivery` and `Delivered, unpaid` both exist,
and neither is a stage in a sequence.
"""

from typing import Any
from urllib.parse import parse_qs, urlparse

from ops.db import Db
from ops.http_util import HttpError, Router

ROLE_READ = "viewer"
ROLE_WRITE = "operations"
ROLE_APPROVE = "approver"

MAX_MONEY_CENTS = 100_000_000_00

#: The dates a line carries. Each is a FACT, recorded when it happens.
DATE_FIELDS = ("requested_date", "ordered_date", "invoiced_date",
               "delivered_date", "paid_date", "cancelled_date")

#: States someone may assert from the grid without stopping to find the
#: date. The register's own vocabulary, because that is what people say to
#: each other. `cancelled` is absent on purpose: it needs a reason, so it
#: goes through the dates dialog.
STATES = {"to be ordered", "create po", "ordered", "invoice received",
          "paid - pending delivery", "delivered", "complete"}


def _current_fy_label() -> str:
    """The Australian financial year runs July to June, so anything from
    July belongs to the year named for the following calendar year."""
    import time as _time
    now = _time.localtime()
    fy = now.tm_year + 1 if now.tm_mon >= 7 else now.tm_year
    return f"FY{str(fy)[2:]}"


def entity_ids(user: dict[str, Any]) -> list[int]:
    return sorted({r["entity_id"] for r in user["roles"]})


def register(router: Router, db: Db) -> None:

    def scoped(user):
        ids = entity_ids(user)
        if not ids:
            raise HttpError(403, "no entity access")
        return ids

    def owned_line(user, line_id):
        row = db.query_one(
            "SELECT * FROM v_procurement_line WHERE id = ?", (line_id,))
        if row is None or row["entity_id"] not in entity_ids(user):
            raise HttpError(404, "not found")
        return row

    @router.route("/api/procurement", role=ROLE_READ)
    def list_lines(handler, user):
        ids = scoped(user)
        marks = ",".join("?" * len(ids))
        query = parse_qs(urlparse(handler.path).query)
        where = [f"l.entity_id IN ({marks})"]
        params = list(ids)
        if query.get("project"):
            where.append("l.project_id = ?")
            params.append(query["project"][0])
        if query.get("fy"):
            where.append("pe.fy = ?")
            params.append(query["fy"][0])
        rows = db.query(
            f"""SELECT l.*, pe.fy_label FROM v_procurement_line l
                LEFT JOIN period pe ON pe.id = l.period_id
                WHERE {' AND '.join(where)}
                ORDER BY pe.month_start, l.project_name, l.id""",
            tuple(params))
        return 200, {
            "lines": rows,
            "totals": {
                # Everything not cancelled, whether ordered or only
                # estimated. The four figures beside it each leave
                # something out, so none of them is the total anyone means
                # when they ask what this lot is worth.
                "total_cents": sum(
                    r["total_cents"] for r in rows if not r["cancelled_date"]),
                "committed_cents": sum(
                    r["total_cents"] for r in rows
                    if not r["cancelled_date"] and not r["is_estimate"]),
                "estimated_cents": sum(
                    r["total_cents"] for r in rows
                    if not r["cancelled_date"] and r["is_estimate"]),
                # `is_paid` and `is_delivered` come from the view, which
                # reads a date where there is one and the stated state
                # where there is not -- the same source the state column
                # uses, so the figures cannot disagree with the rows.
                "paid_cents": sum(
                    r["total_cents"] for r in rows
                    if r["is_paid"] and not r["is_estimate"]),
                "undelivered_cents": sum(
                    r["total_cents"] for r in rows
                    if not r["is_delivered"] and not r["cancelled_date"]
                    and not r["is_estimate"]),
            },
            "suppliers": db.query(
                f"""SELECT id, name, default_currency FROM supplier
                    WHERE entity_id IN ({marks}) AND is_active = 1
                    ORDER BY name""", tuple(ids)),
            # Everything a line can be pointed at. Small enough to send
            # whole -- 22 quotes and 28 orders -- and sending it means the
            # dialog opens without a round trip per field.
            "projects": db.query(
                f"""SELECT id, name, job_code FROM project
                    WHERE entity_id IN ({marks}) ORDER BY name""", tuple(ids)),
            "quotes": db.query(
                f"""SELECT q.id, q.quote_ref, q.currency, q.fx_rate_bp,
                           q.supplier_id, s.name AS supplier_name
                    FROM supplier_quote q JOIN supplier s ON s.id = q.supplier_id
                    WHERE q.entity_id IN ({marks})
                    ORDER BY s.name, q.quote_ref""", tuple(ids)),
            "pos": db.query(
                f"""SELECT id, po_number, project_id, supplier_id, po_date
                    FROM supplier_po WHERE entity_id IN ({marks})
                    ORDER BY po_number""", tuple(ids)),
            "periods": db.query(
                """SELECT id, label, fy_label, month_start FROM period
                   ORDER BY month_start"""),
            "invoices": db.query(
                f"""SELECT id, invoice_ref, supplier_id FROM supplier_invoice
                    WHERE entity_id IN ({marks}) ORDER BY invoice_ref""",
                tuple(ids)),
            "date_fields": list(DATE_FIELDS),
            "states": sorted(STATES),
            # The year we are IN, so the grid opens on it. Computed here
            # rather than in the browser: the same rule already lives on
            # the dashboard, and a financial year worked out in two places
            # is a financial year that will eventually disagree with
            # itself.
            "current_fy_label": _current_fy_label(),
        }

    @router.route("/api/procurement", role=ROLE_WRITE, method="POST")
    def add_line(handler, user):
        ids = scoped(user)
        payload = handler.read_json()
        errors = {}
        project = db.query_one(
            "SELECT id, entity_id FROM project WHERE id = ?",
            (payload.get("project_id"),))
        if project is None or project["entity_id"] not in ids:
            errors["project_id"] = "required; a project on this entity"
        supplier_id = None
        if payload.get("supplier_id"):
            supplier_id = db.scalar(
                "SELECT id FROM supplier WHERE id = ? AND entity_id = ?",
                (payload["supplier_id"], project["entity_id"] if project else 0))
            if supplier_id is None:
                errors["supplier_id"] = "not a supplier on this entity"
        try:
            quantity = int(payload.get("quantity", 1))
        except (TypeError, ValueError):
            quantity = 0
        if quantity < 1:
            errors["quantity"] = "at least one"
        try:
            unit = int(payload.get("unit_cost_cents", 0))
        except (TypeError, ValueError):
            unit = -1
        if unit < 0 or unit > MAX_MONEY_CENTS:
            errors["unit_cost_cents"] = "must be a whole number of cents"
        currency = (payload.get("currency") or "AUD").strip()
        if currency not in ("AUD", "USD"):
            errors["currency"] = "AUD or USD"
        period_id = None
        if payload.get("period_id"):
            period_id = db.scalar("SELECT id FROM period WHERE id = ?",
                                  (payload["period_id"],))
        if errors:
            raise HttpError(400, "validation failed", errors)

        # A foreign-currency line needs a rate, and the rate lives on the
        # quote. Without one the line cannot be costed in AUD, so it is
        # refused rather than stored at par.
        rate = None
        quote_id = payload.get("supplier_quote_id") or None
        if quote_id:
            quote = db.query_one(
                "SELECT id, fx_rate_bp, currency FROM supplier_quote WHERE id = ?",
                (quote_id,))
            if quote is None:
                raise HttpError(400, "validation failed",
                                {"supplier_quote_id": "not found"})
            rate = quote["fx_rate_bp"]
        if currency != "AUD" and not rate:
            raise HttpError(400, "validation failed", {
                "supplier_quote_id":
                    "a USD line needs a quote carrying the rate agreed with "
                    "the supplier"})

        # `project` is not None here: an unknown one puts `project_id` in
        # `errors`, and errors raise above.
        if project is None:
            raise HttpError(400, "validation failed",
                            {"project_id": "required"})
        line = db.create_procurement_line({
            "entity_id": project["entity_id"], "project_id": project["id"],
            "supplier_id": supplier_id,
            "supplier_quote_id": quote_id,
            "supplier_po_id": payload.get("supplier_po_id") or None,
            "period_id": period_id,
            "item": (payload.get("item") or "").strip() or None,
            "description": (payload.get("description") or "").strip() or None,
            "quantity": quantity, "currency": currency,
            "unit_cost_cents": unit,
            "total_cents": Db.extend(unit, quantity, rate),
            "requested_date": (payload.get("requested_date") or "").strip()
            or None,
            "note": (payload.get("note") or "").strip() or None,
        }, user["id"])
        return 201, line

    @router.route("/api/procurement/{line_id}", role=ROLE_WRITE,
                  method="PATCH")
    def update_line(handler, user, line_id):
        row = owned_line(user, line_id)
        payload = handler.read_json()
        fields, errors = {}, {}

        for key in DATE_FIELDS:
            if key in payload:
                fields[key] = (payload[key] or "").strip() or None
        if "cancelled_date" in fields and fields["cancelled_date"]:
            if not (payload.get("cancel_reason") or "").strip():
                # A line that vanishes from the cost without a reason is a
                # figure nobody can explain at month end.
                errors["cancel_reason"] = "required to cancel a line"
            else:
                fields["cancel_reason"] = payload["cancel_reason"].strip()

        # A state chosen from the grid, with no date behind it. The same
        # fact the sheet recorded, so the same column carries it. Validated
        # HERE, above the errors check -- below it, an unknown state was
        # accepted and written.
        if "stated_state" in payload:
            wanted = (payload["stated_state"] or "").strip().casefold() or None
            if wanted and wanted not in STATES:
                errors["stated_state"] = f"one of {', '.join(sorted(STATES))}"
            else:
                fields["stated_state"] = wanted

        for key in ("item", "description", "note"):
            if key in payload:
                fields[key] = (payload[key] or "").strip() or None
        if "project_id" in payload:
            target = db.query_one(
                "SELECT id, entity_id FROM project WHERE id = ?",
                (payload["project_id"],))
            if target is None or target["entity_id"] != row["entity_id"]:
                errors["project_id"] = "not a project on this entity"
            else:
                fields["project_id"] = target["id"]
        if "supplier_id" in payload:
            if payload["supplier_id"]:
                found = db.scalar(
                    "SELECT id FROM supplier WHERE id = ? AND entity_id = ?",
                    (payload["supplier_id"], row["entity_id"]))
                if found is None:
                    errors["supplier_id"] = "not a supplier on this entity"
                else:
                    fields["supplier_id"] = found
            else:
                fields["supplier_id"] = None
        if "currency" in payload:
            currency = (payload["currency"] or "AUD").strip()
            if currency not in ("AUD", "USD"):
                errors["currency"] = "AUD or USD"
            else:
                fields["currency"] = currency
        if "supplier_quote_id" in payload:
            fields["supplier_quote_id"] = payload["supplier_quote_id"] or None
        if "is_estimate" in payload:
            # An estimate becomes real in place, so the month keeps its
            # forecast while somebody types the real figure.
            fields["is_estimate"] = 1 if payload["is_estimate"] else 0
        if "supplier_invoice_id" in payload:
            fields["supplier_invoice_id"] = payload["supplier_invoice_id"] or None
        if "period_id" in payload:
            fields["period_id"] = payload["period_id"] or None
        if "supplier_po_id" in payload:
            fields["supplier_po_id"] = payload["supplier_po_id"] or None

        # Quantity or unit cost means recomputing the total, at the extended
        # amount and once (ADR-15): converting per unit loses a cent a line.
        if "quantity" in payload or "unit_cost_cents" in payload:
            try:
                quantity = int(payload.get("quantity", row["quantity"]))
                unit = int(payload.get("unit_cost_cents",
                                       row["unit_cost_cents"]))
            except (TypeError, ValueError):
                quantity, unit = 0, -1
            if quantity < 1:
                errors["quantity"] = "at least one"
            if unit < 0 or unit > MAX_MONEY_CENTS:
                errors["unit_cost_cents"] = "must be a whole number of cents"
            if not errors:
                fields["quantity"] = quantity
                fields["unit_cost_cents"] = unit
                # The rate of the quote being SET, not the one it had: a
                # line moved to a different quote is costed at that quote's
                # rate, which is the whole reason the rate lives there.
                rate = row["fx_rate_bp"]
                if "supplier_quote_id" in fields:
                    rate = db.scalar(
                        "SELECT fx_rate_bp FROM supplier_quote WHERE id = ?",
                        (fields["supplier_quote_id"],)) \
                        if fields["supplier_quote_id"] else None
                currency = fields.get("currency", row["currency"])
                if currency != "AUD" and not rate:
                    errors["supplier_quote_id"] = (
                        "a USD line needs a quote carrying the rate")
                else:
                    fields["total_cents"] = Db.extend(unit, quantity, rate)
        if errors:
            raise HttpError(400, "validation failed", errors)

        # A real date supersedes a stated one: `when` beats `what someone
        # said`, and leaving both would let them disagree.
        if any(fields.get(k) for k in ("ordered_date", "invoiced_date",
                                       "delivered_date", "paid_date")):
            fields["stated_state"] = None

        result = db.update_procurement_line(
            line_id, fields, user["id"],
            (payload.get("reason") or "").strip() or None)
        if result is None:
            raise HttpError(404, "not found")
        return 200, result

    @router.route("/api/procurement/quotes", role=ROLE_WRITE, method="POST")
    def add_quote(handler, user):
        """A quote carries the FX rate, so a USD line cannot exist without
        one. Created here rather than only by import, or the first foreign
        purchase after go-live has nowhere to hang its rate."""
        ids = scoped(user)
        payload = handler.read_json()
        errors = {}
        supplier = db.query_one(
            "SELECT id, entity_id FROM supplier WHERE id = ?",
            (payload.get("supplier_id"),))
        if supplier is None or supplier["entity_id"] not in ids:
            raise HttpError(400, "validation failed",
                            {"supplier_id": "required"})
        currency = (payload.get("currency") or "AUD").strip()
        if currency not in ("AUD", "USD"):
            errors["currency"] = "AUD or USD"
        rate = None
        if currency != "AUD":
            try:
                rate = int(round(float(payload.get("fx_rate", 0)) * 10_000_000))
            except (TypeError, ValueError):
                rate = 0
            if rate <= 0:
                errors["fx_rate"] = ("required for a foreign quote, e.g. "
                                     "1.388561 AUD per USD")
        if errors:
            raise HttpError(400, "validation failed", errors)
        return 201, db.create_supplier_quote({
            "entity_id": supplier["entity_id"], "supplier_id": supplier["id"],
            "quote_ref": (payload.get("quote_ref") or "").strip() or None,
            "quote_date": (payload.get("quote_date") or "").strip() or None,
            "currency": currency, "fx_rate_bp": rate,
            "email_subject": (payload.get("email_subject") or "").strip() or None,
        }, user["id"])

    @router.route("/api/procurement/pos", role=ROLE_WRITE, method="POST")
    def add_po(handler, user):
        ids = scoped(user)
        payload = handler.read_json()
        errors = {}
        project = db.query_one(
            "SELECT id, entity_id FROM project WHERE id = ?",
            (payload.get("project_id"),))
        supplier_id = db.scalar("SELECT id FROM supplier WHERE id = ?",
                                (payload.get("supplier_id"),))
        if project is None or project["entity_id"] not in ids:
            errors["project_id"] = "required"
        if supplier_id is None:
            errors["supplier_id"] = "required"
        if not (payload.get("po_number") or "").strip():
            errors["po_number"] = "required"
        if errors or project is None:
            raise HttpError(400, "validation failed",
                            errors or {"project_id": "required"})
        return 201, db.create_supplier_po({
            "entity_id": project["entity_id"], "project_id": project["id"],
            "supplier_id": supplier_id,
            "supplier_quote_id": payload.get("supplier_quote_id") or None,
            "po_number": payload["po_number"].strip(),
            "po_date": (payload.get("po_date") or "").strip() or None,
            "approved_by": (payload.get("approved_by") or "").strip() or None,
            "approved_date": (payload.get("approved_date") or "").strip() or None,
        }, user["id"])

    @router.route("/api/procurement/{line_id}", role=ROLE_APPROVE,
                  method="DELETE")
    def remove_line(handler, user, line_id):
        """A row that should never have existed.

        Approver, not operations: deleting is the one action that leaves
        nothing on the screen to notice, so it needs the person who signs
        things off rather than the person entering them.
        """
        row = owned_line(user, line_id)
        payload = handler.read_json() if handler.headers.get("Content-Length") \
            else {}
        blocked = db.procurement_line_is_deletable(row["id"])
        if blocked:
            raise HttpError(409, blocked)
        try:
            db.delete_procurement_line(
                row["id"], payload.get("reason"), user["id"])
        except ValueError as e:
            raise HttpError(400, "validation failed", {"reason": str(e)})
        return 200, {"deleted": row["id"], "item": row["item"],
                     "project": row["project_name"]}

    @router.route("/api/procurement/{line_id}/invoice", role=ROLE_WRITE,
                  method="POST")
    def attach_invoice(handler, user, line_id):
        """One supplier invoice regularly covers several orders, so it is
        found by reference rather than created per line."""
        row = owned_line(user, line_id)
        payload = handler.read_json()
        ref = (payload.get("invoice_ref") or "").strip()
        if not ref:
            raise HttpError(400, "validation failed",
                            {"invoice_ref": "required"})
        if not row["supplier_id"]:
            raise HttpError(409, "this line has no supplier; an invoice "
                                 "belongs to one")
        invoice, created = db.find_or_create_supplier_invoice(
            row["entity_id"], row["supplier_id"], ref, user["id"],
            invoice_date=(payload.get("invoice_date") or "").strip() or None,
            due_date=(payload.get("due_date") or "").strip() or None)
        db.update_procurement_line(
            line_id, {"supplier_invoice_id": invoice["id"],
                      "invoiced_date": (payload.get("invoice_date") or "").strip()
                      or None,
                      "stated_state": None},
            user["id"], f"invoice {ref}")
        return 200, {"invoice": invoice, "created": created}

    @router.route("/api/procurement/{line_id}/history", role=ROLE_READ)
    def history(handler, user, line_id):
        owned_line(user, line_id)
        return 200, {"revisions": db.query(
            """SELECT r.field, r.old_value, r.new_value, r.reason,
                      r.changed_ts, u.display_name AS changed_by
               FROM procurement_line_revision r
               LEFT JOIN users u ON u.id = r.changed_by
               WHERE r.line_id = ? ORDER BY r.changed_ts, r.id""",
            (line_id,))}
