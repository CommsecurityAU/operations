"""Schedules module — recurring claims and renewals (STP-2).

Maintenance is one agreement spread over a year, not twelve claims someone
typed. `36 Wellington` is $22,689 as twelve payments of $1,890.75; entering
those by hand is the monthly copy-forward ritual in another guise, and next
year is twelve more.

Two operations, and the order matters:

  ADOPT     attach claims that already exist to the schedule describing them
  GENERATE  create the ones that do not exist yet

Every recurring project in the register arrived with its twelve rows already
typed, so generating first would double the year. Adopt is therefore offered
first and generation reports how many periods were already covered.

The renewal date is the point of the whole thing. A maintenance agreement
that lapses unnoticed is revenue that simply stops, and nothing in a
spreadsheet of twelve rows tells you it is about to.
"""

from typing import Any

from ops.db import Db
from ops.http_util import HttpError, Router

ROLE_READ = "viewer"
ROLE_WRITE = "operations"

FREQUENCIES = ("monthly", "quarterly", "annual")


def entity_ids(user: dict[str, Any]) -> list[int]:
    return sorted({r["entity_id"] for r in user["roles"]})


def validate(db: Db, payload: dict[str, Any], entity_id: int,
             existing: dict[str, Any] | None = None):
    errors: dict[str, str] = {}
    fields: dict[str, Any] = {}
    creating = existing is None

    if creating:
        project = db.query_one(
            "SELECT id, entity_id FROM project WHERE id = ?",
            (payload.get("project_id"),))
        if project is None or project["entity_id"] != entity_id:
            errors["project_id"] = "required; a project on this entity"
        else:
            fields["project_id"] = project["id"]
            po = db.query_one(
                "SELECT id FROM customer_po WHERE id = ? AND project_id = ?",
                (payload.get("customer_po_id"), project["id"]))
            if po is None:
                errors["customer_po_id"] = "required; which PO this bills against"
            else:
                fields["customer_po_id"] = po["id"]

    if creating or "description" in payload:
        text = (payload.get("description") or "").strip()
        if not text:
            # It becomes the detail on every claim it makes, so a blank one
            # produces a year of unlabelled rows.
            errors["description"] = "required; it labels every claim this makes"
        else:
            fields["description"] = text

    if creating or "amount_cents" in payload:
        try:
            amount = int(payload.get("amount_cents", 0))
        except (TypeError, ValueError):
            amount = 0
        if amount <= 0:
            errors["amount_cents"] = "required; the amount per occurrence"
        else:
            fields["amount_cents"] = amount

    if creating or "frequency" in payload:
        freq = (payload.get("frequency") or "").strip()
        if freq not in FREQUENCIES:
            errors["frequency"] = f"must be one of {', '.join(FREQUENCIES)}"
        else:
            fields["frequency"] = freq

    for key in ("start_period_id", "end_period_id"):
        if creating or key in payload:
            row = db.query_one("SELECT id, month_start FROM period WHERE id = ?",
                               (payload.get(key),))
            if row is None:
                errors[key] = "required"
            else:
                fields[key] = row["id"]
    if "start_period_id" in fields and "end_period_id" in fields:
        span = db.query_one(
            """SELECT (SELECT month_start FROM period WHERE id = ?) AS start,
                      (SELECT month_start FROM period WHERE id = ?) AS finish""",
            (fields["start_period_id"], fields["end_period_id"]))
        if span and span["start"] > span["finish"]:
            errors["end_period_id"] = "the schedule would end before it starts"

    if "renewal_date" in payload:
        fields["renewal_date"] = (payload.get("renewal_date") or "").strip() or None
    if "renewal_notice_days" in payload:
        try:
            fields["renewal_notice_days"] = max(0, int(payload["renewal_notice_days"]))
        except (TypeError, ValueError):
            errors["renewal_notice_days"] = "must be a whole number of days"
    if "renewal_note" in payload:
        fields["renewal_note"] = (payload.get("renewal_note") or "").strip() or None
    if "is_active" in payload:
        fields["is_active"] = 1 if payload["is_active"] else 0

    return fields, errors


def register(router: Router, db: Db) -> None:

    def scoped(user):
        ids = entity_ids(user)
        if not ids:
            raise HttpError(403, "no entity access")
        return ids

    def owned(user, schedule_id):
        ids = scoped(user)
        row = db.query_one(
            "SELECT * FROM claim_schedule WHERE id = ?", (schedule_id,))
        if row is None or row["entity_id"] not in ids:
            raise HttpError(404, "not found")
        return row

    @router.route("/api/schedules", role=ROLE_READ)
    def list_schedules(handler, user):
        ids = scoped(user)
        marks = ",".join("?" * len(ids))
        rows = db.query(
            f"""SELECT c.*,
                       (SELECT COUNT(*) FROM claim_line cl
                        WHERE cl.schedule_id = c.schedule_id) AS claim_count
                FROM v_upcoming_renewals c
                WHERE c.entity_id IN ({marks})
                ORDER BY c.days_until IS NULL, c.days_until""", tuple(ids))
        # Inactive schedules are excluded from the renewals view; list them
        # too, or a schedule someone switched off becomes unreachable.
        inactive = db.query(
            f"""SELECT * FROM v_schedule_coverage
                WHERE entity_id IN ({marks}) AND is_active = 0""", tuple(ids))
        out = rows + inactive
        # How many periods the schedule COVERS, so the count can be read as
        # a fraction. "12 claims" says nothing about whether that is all of
        # them; "12 / 12" says the year is complete and "6 / 12" says it is
        # not, which is the only reason to press Generate.
        for row in out:
            row["expected_count"] = len(db.schedule_periods(row["schedule_id"]))
        return 200, {"schedules": out, "frequencies": list(FREQUENCIES)}

    @router.route("/api/schedules", role=ROLE_WRITE, method="POST")
    def create_schedule(handler, user):
        payload = handler.read_json()
        ids = scoped(user)
        entity_id = payload.get("entity_id") or ids[0]
        if entity_id not in ids:
            raise HttpError(403, "no access to that entity")
        fields, errors = validate(db, payload, entity_id)
        if errors:
            raise HttpError(400, "validation failed", errors)
        fields["entity_id"] = entity_id
        schedule = db.create_schedule(fields, user["id"])
        # Adopt immediately: the claims almost certainly already exist, and
        # a schedule that appears to cover nothing invites someone to press
        # Generate and double the year.
        adopted = db.adopt_claims_into_schedule(schedule["id"], user["id"])
        return 201, {"schedule": schedule, "adopted": adopted}

    @router.route("/api/schedules/{schedule_id}", role=ROLE_WRITE,
                  method="PATCH")
    def update_schedule(handler, user, schedule_id):
        row = owned(user, schedule_id)
        payload = handler.read_json()
        unknown = [k for k in payload if k not in db.SCHEDULE_MUTABLE]
        if unknown:
            raise HttpError(400, "validation failed",
                            {k: "not an editable field" for k in unknown})
        fields, errors = validate(db, payload, row["entity_id"],
                                  existing=dict(row))
        if errors:
            raise HttpError(400, "validation failed", errors)
        result = db.update_schedule(row["id"], fields, user["id"])
        if result is None:
            raise HttpError(404, "not found")
        return 200, result

    @router.route("/api/schedules/{schedule_id}/adopt", role=ROLE_WRITE,
                  method="POST")
    def adopt(handler, user, schedule_id):
        row = owned(user, schedule_id)
        return 200, db.adopt_claims_into_schedule(row["id"], user["id"])

    @router.route("/api/schedules/{schedule_id}/generate", role=ROLE_WRITE,
                  method="POST")
    def generate(handler, user, schedule_id):
        row = owned(user, schedule_id)
        if not row["is_active"]:
            raise HttpError(409, "this schedule is not active")
        return 200, db.generate_schedule_claims(row["id"], user["id"])

    @router.route("/api/schedules/{schedule_id}/preview", role=ROLE_READ)
    def preview(handler, user, schedule_id):
        """Which months it covers, and what each already holds. Pressing
        Generate should never be the way to find out what it will do."""
        row = owned(user, schedule_id)
        out = []
        for period in db.schedule_periods(row["id"]):
            claims = db.query(
                """SELECT id, amount_cents, schedule_id, status, detail,
                          invoice_number
                   FROM claim_line
                   WHERE project_id = ? AND period_id = ?
                     AND is_opening_balance = 0 ORDER BY id""",
                (row["project_id"], period["id"]))
            mine = next((c for c in claims if c["schedule_id"] == row["id"]), None)
            # Every claim in the month, not just the one the schedule owns.
            # A period showing one claim when it holds two is how the Jul-26
            # surprise happened in the first place.
            others = [c for c in claims if c["schedule_id"] != row["id"]]
            out.append({
                "period_id": period["id"], "label": period["label"],
                "claim": dict(mine) if mine else None,
                "others": [dict(c) for c in others],
                "state": ("mine" if mine
                          else "unattached" if others else "missing"),
            })
        return 200, {"periods": out, "amount_cents": row["amount_cents"],
                     "description": row["description"]}
