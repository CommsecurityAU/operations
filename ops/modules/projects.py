"""Projects module (CS-OP-ARCH-002 §6, CS-OP-STP-001 STP-1).

Registers its own routes and owns its own validation. Modules never import
each other; shared behaviour lives in `db` or `http_util`.

The read query lives here as SQL, which §4 permits. Every write goes through
a `Db` method.
"""

from typing import Any

from ops.db import Db
from ops.http_util import HttpError, Router

ROLE_READ = "viewer"
ROLE_WRITE = "operations"
ROLE_DELETE = "admin"

STATUSES = ("Active", "DLP", "SLA", "Complete")
# NO "issue" here, deliberately. Job numbers still come from iTrade, so a
# number this platform allocated could collide with one iTrade hands out
# tomorrow -- and the collision would not surface until both reached Xero.
# Creation therefore records the code we were GIVEN, or records that we do
# not have one yet. Allocation stays in the worklist, as an explicit act by
# someone who knows the number is ours to give (ADR-28).
JOB_CODE_MODES = ("existing", "defer")
JOB_CODE_PATTERN = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9\-_/]{0,39}$")
DEFERRED = "TBA"
MAX_NAME = 200
MAX_MONEY_CENTS = 100_000_000_00      # $100m: a typo, not a project

PROJECT_SELECT = """
    SELECT v.project_id   AS id,
           v.project_name AS name,
           v.job_code, v.status, v.purchase_order_cents,
           v.contract_value_cents, v.ordered_cents, v.ordered_unbilled_cents,
           v.invoiced_prior_cents, v.orders_in_hand_cents,
           p.needs_resolution, p.project_lead, p.project_no, p.notes,
           p.client_id, p.type_id,
           COALESCE(pt.code, '(untyped)') AS type,
           COALESCE(c.name, '(no client)') AS client,
           -- Held and not yet released: a position, not a period figure.
           COALESCE(r.held_cents, 0) AS retention_held_cents,
           -- The PO a schedule would bill against. One per project after
           -- migration 003; when a project gains several this has to be
           -- chosen rather than assumed.
           (SELECT po.id FROM customer_po po
            WHERE po.project_id = p.id ORDER BY po.id LIMIT 1)
                                     AS customer_po_id,
           p.practical_completion_date, p.dlp_end_date
    FROM v_project_orders_in_hand v
    JOIN project p ON p.id = v.project_id
    LEFT JOIN project_type pt ON pt.id = p.type_id
    LEFT JOIN client c ON c.id = p.client_id
    LEFT JOIN v_project_retention r ON r.project_id = p.id
"""


def entity_ids(user: dict[str, Any]) -> list[int]:
    return sorted({r["entity_id"] for r in user["roles"]})


def _int_or_none(value, field, errors, minimum=0, maximum=MAX_MONEY_CENTS):
    if value in (None, ""):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        errors[field] = "must be a whole number of cents"
        return None
    if n < minimum:
        errors[field] = "cannot be negative"
    elif n > maximum:
        errors[field] = f"looks like a typo (over ${maximum // 100:,})"
    return n


def validate(db: Db, payload: dict[str, Any], entity_id: int,
             existing: int | None = None):
    """Returns (fields, errors). Collects EVERY problem rather than stopping
    at the first, because a form that reveals one error at a time turns a
    single correction into four round trips.
    """
    errors = {}
    fields = {}
    required = existing is None

    name = (payload.get("name") or "").strip()
    if required or "name" in payload:
        if not name:
            errors["name"] = "required"
        elif len(name) > MAX_NAME:
            errors["name"] = f"longer than {MAX_NAME} characters"
        else:
            clash = db.query_one(
                "SELECT id FROM project WHERE entity_id = ? AND name = ?",
                (entity_id, name))
            if clash and (existing is None or clash["id"] != existing):
                errors["name"] = "another project already has this name"
            else:
                fields["name"] = name

    lead = (payload.get("project_lead") or "").strip()
    if required or "project_lead" in payload:
        # STP-1: a project cannot exist without a lead. An unowned project is
        # how work goes unclaimed and unbilled.
        if not lead:
            errors["project_lead"] = "required"
        else:
            fields["project_lead"] = lead

    status = (payload.get("status") or "").strip()
    if required or "status" in payload:
        if status not in STATUSES:
            errors["status"] = f"must be one of {', '.join(STATUSES)}"
        else:
            fields["status"] = status

    # A client may arrive as an id (picked from the list) or as a name
    # (typed, possibly new). Resolution happens in the route, not here,
    # because creating a client is a write and validate() must not write.
    if required or "client_id" in payload or "client_name" in payload:
        client_id = payload.get("client_id")
        client_name = (payload.get("client_name") or "").strip()
        if client_id:
            row = db.query_one(
                "SELECT id FROM client WHERE id = ? AND entity_id = ?",
                (client_id, entity_id))
            if row is None:
                errors["client_id"] = "not a client on this entity"
            else:
                fields["client_id"] = row["id"]
        elif client_name:
            if len(client_name) > MAX_NAME:
                errors["client_name"] = f"longer than {MAX_NAME} characters"
            else:
                fields["client_name"] = client_name
        else:
            errors["client_name"] = "required; pick one or type a new name"

    if required or "type_id" in payload:
        type_id = payload.get("type_id")
        row = db.query_one("SELECT id FROM project_type WHERE id = ?",
                           (type_id,)) if type_id else None
        if row is None:
            errors["type_id"] = "required"
        else:
            fields["type_id"] = row["id"]

    # How the project gets its number. Always allocating was wrong: two
    # projects created in the UI already had codes of their own, so the
    # platform issued numbers nobody wanted and burnt them from the sequence.
    if required:
        # Defaults to deferring. A default that allocates is how two
        # projects ended up with numbers nobody wanted.
        mode = payload.get("job_code_mode") or "defer"
        if mode not in JOB_CODE_MODES:
            errors["job_code_mode"] = f"must be one of {', '.join(JOB_CODE_MODES)}"
        elif mode == "existing":
            code = (payload.get("job_code") or "").strip()
            if not code:
                errors["job_code"] = "required when the project already has one"
            elif not JOB_CODE_PATTERN.match(code):
                errors["job_code"] = "letters, digits, - _ / only"
            else:
                clash = db.query_one(
                    "SELECT name FROM project WHERE job_code = ?", (code,))
                if clash is not None:
                    # Naming the other project matters: "already used" sends
                    # someone hunting, "already used by Oasis Health Group
                    # Gym" ends the question.
                    errors["job_code"] = f"already used by {clash['name']}"
                else:
                    fields["job_code"] = code
        elif mode == "defer":
            # Deliberately NOT allocating. The worklist entry created
            # alongside makes the deferral visible.
            fields["job_code"] = DEFERRED

    if "project_no" in payload:
        fields["project_no"] = (payload.get("project_no") or "").strip() or None
    if "notes" in payload:
        fields["notes"] = (payload.get("notes") or "").strip() or None

    po = prior = None
    if required or "purchase_order_cents" in payload:
        po = _int_or_none(payload.get("purchase_order_cents", 0),
                          "purchase_order_cents", errors)
        fields["purchase_order_cents"] = po or 0
    if required or "invoiced_prior_cents" in payload:
        prior = _int_or_none(payload.get("invoiced_prior_cents", 0),
                             "invoiced_prior_cents", errors)
        fields["invoiced_prior_cents"] = prior or 0

    # The schema CHECK would refuse this anyway, but an IntegrityError
    # reaches the user as "internal error". Say what is wrong instead.
    effective_po = fields.get("purchase_order_cents")
    effective_prior = fields.get("invoiced_prior_cents")
    if existing is not None:
        current = db.query_one(
            """SELECT purchase_order_cents, invoiced_prior_cents
               FROM project WHERE id = ?""", (existing,))
        if current:
            if effective_po is None:
                effective_po = current["purchase_order_cents"]
            if effective_prior is None:
                effective_prior = current["invoiced_prior_cents"]
    if (effective_po is not None and effective_prior is not None
            and "purchase_order_cents" not in errors
            and "invoiced_prior_cents" not in errors
            and effective_prior > effective_po):
        errors["invoiced_prior_cents"] = (
            "cannot exceed the contract value; a project cannot have been "
            "invoiced more than it is worth")

    return fields, errors


def _resolve_client_field(db: Db, fields: dict[str, Any], entity_id: int,
                          actor_id: int):
    """Turn a typed client_name into a client_id, creating one if needed.

    Reports back when a near-miss reused an existing record, so the user is
    told their "M Squared" became "MSquared" rather than quietly finding
    out later.
    """
    name = fields.pop("client_name", None)
    if not name:
        return None
    client_id, created, matched = db.resolve_client(entity_id, name, actor_id)
    fields["client_id"] = client_id
    return {"id": client_id, "name": matched, "created": created,
            "typed": name, "reused_existing_spelling": matched != name}


def _money_field(raw, errors, key):
    try:
        amount = int(raw)
    except (TypeError, ValueError):
        errors[key] = "must be a whole number of cents"
        return 0
    if amount < 0:
        errors[key] = "cannot be negative"
    elif amount > MAX_MONEY_CENTS:
        errors[key] = f"looks like a typo (over ${MAX_MONEY_CENTS // 100:,})"
    return amount


def _owned_po(db: Db, user: dict[str, Any], po_id):
    row = db.query_one(
        """SELECT h.customer_po_id, p.entity_id FROM v_customer_po_history h
           JOIN project p ON p.id = h.project_id
           WHERE h.customer_po_id = ?""", (po_id,))
    if row is None or row["entity_id"] not in entity_ids(user):
        raise HttpError(404, "not found")
    return row


def register(router: Router, db: Db) -> None:
    """Annotated: an untyped `router` makes every @router.route below an
    unknown decorator, which erases the type of the handler it wraps."""

    @router.route("/api/projects", role=ROLE_READ)
    def list_projects(handler, user):
        ids = entity_ids(user)
        if not ids:
            return 200, {"projects": []}
        marks = ",".join("?" * len(ids))
        return 200, {"projects": db.query(
            f"{PROJECT_SELECT} WHERE v.entity_id IN ({marks}) "
            "ORDER BY v.project_name", tuple(ids))}

    @router.route("/api/projects/{project_id}", role=ROLE_READ)
    def get_project(handler, user, project_id):
        ids = entity_ids(user)
        if not ids:
            raise HttpError(404, "not found")
        marks = ",".join("?" * len(ids))
        row = db.query_one(
            f"{PROJECT_SELECT} WHERE v.project_id = ? AND v.entity_id IN ({marks})",
            (project_id, *ids))
        if row is None:
            raise HttpError(404, "not found")
        return 200, row

    # ------------------------------------------------------ customer POs
    @router.route("/api/projects/{project_id}/pos", role=ROLE_READ)
    def list_pos(handler, user, project_id):
        ids = entity_ids(user)
        row = db.query_one("SELECT entity_id FROM project WHERE id = ?",
                           (project_id,))
        if row is None or row["entity_id"] not in ids:
            raise HttpError(404, "not found")
        pos = db.query(
            """SELECT h.*, po.note, po.is_placeholder,
                      po.retention_applies, po.retention_rate_bp,
                      po.retention_cap_bp, po.release_policy, po.release_split_bp,
                      r.held_cents
               FROM v_customer_po_history h
               JOIN customer_po po ON po.id = h.customer_po_id
               LEFT JOIN v_po_retention_position r
                      ON r.customer_po_id = h.customer_po_id
               WHERE h.project_id = ? ORDER BY h.customer_po_id""", (project_id,))
        return 200, {
            "pos": pos,
            "contract_value_cents": db.scalar(
                "SELECT contract_value_cents FROM project WHERE id = ?",
                (project_id,)),
            # What is left to invoice, and what has actually been planned.
            # They SHOULD agree: everything still to bill ought to sit in a
            # month somewhere. Where they do not, the gap is either work
            # nobody has forecast yet or a forecast that has outrun the
            # contract -- and both are worth seeing on the project rather
            # than discovered in a month-end total.
            "remaining_cents": db.scalar(
                "SELECT orders_in_hand_cents FROM v_project_orders_in_hand "
                "WHERE project_id = ?", (project_id,)),
            "forecast_cents": db.scalar(
                """SELECT COALESCE(SUM(amount_cents), 0) FROM claim_line
                   WHERE project_id = ? AND status IN ('forecast','due','approved')""",
                (project_id,)),
            "revisions": db.query(
                """SELECT r.customer_po_id, r.old_value, r.new_value, r.kind,
                          r.reason, r.effective_date, r.changed_ts,
                          u.display_name AS changed_by
                   FROM customer_po_revision r
                   JOIN customer_po po ON po.id = r.customer_po_id
                   LEFT JOIN users u ON u.id = r.changed_by
                   WHERE po.project_id = ? ORDER BY r.changed_ts, r.id""",
                (project_id,)),
            "kinds": list(db.PO_KINDS),
        }

    @router.route("/api/projects/{project_id}/pos", role=ROLE_WRITE,
                  method="POST")
    def add_po(handler, user, project_id):
        ids = entity_ids(user)
        row = db.query_one("SELECT id, entity_id FROM project WHERE id = ?",
                           (project_id,))
        if row is None or row["entity_id"] not in ids:
            raise HttpError(404, "not found")
        payload = handler.read_json()
        errors = {}
        amount = _money_field(payload.get("amount_cents"), errors, "amount_cents")
        number = (payload.get("po_number") or "").strip()
        if number:
            # Named, so it is obvious whether the duplicate is a typo or the
            # same order genuinely reaching two projects.
            clash = db.query_one(
                """SELECT p.name FROM customer_po po
                   JOIN project p ON p.id = po.project_id
                   WHERE po.po_number = ?""", (number,))
            if clash is not None:
                errors["po_number"] = f"already used on {clash['name']}"
        if errors:
            raise HttpError(400, "validation failed", errors)
        return 201, db.create_customer_po({
            "entity_id": row["entity_id"], "project_id": row["id"],
            "po_number": number or None, "amount_cents": amount,
            "issued_date": (payload.get("issued_date") or "").strip() or None,
            "note": (payload.get("note") or "").strip() or None,
        }, user["id"])

    @router.route("/api/pos/{po_id}/revise", role=ROLE_WRITE, method="POST")
    def revise_po(handler, user, po_id):
        po = _owned_po(db, user, po_id)
        payload = handler.read_json()
        errors = {}
        amount = _money_field(payload.get("amount_cents"), errors, "amount_cents")
        kind = payload.get("kind")
        if kind not in db.PO_KINDS:
            errors["kind"] = f"must be one of {', '.join(db.PO_KINDS)}"
        if not (payload.get("reason") or "").strip():
            errors["reason"] = "required"
        if kind == "variation" and not (payload.get("effective_date") or "").strip():
            # The day the contract changed, which is rarely the day someone
            # typed it in -- and without it a past position cannot be
            # reproduced.
            errors["effective_date"] = "a variation needs the date it took effect"
        if errors:
            raise HttpError(400, "validation failed", errors)
        result = db.revise_customer_po(
            po["customer_po_id"], amount, kind,
            payload["reason"].strip(),
            (payload.get("effective_date") or "").strip() or None, user["id"])
        if result is None:
            raise HttpError(404, "not found")
        return 200, result

    @router.route("/api/pos/{po_id}", role=ROLE_WRITE, method="PATCH")
    def update_po(handler, user, po_id):
        po = _owned_po(db, user, po_id)
        payload = handler.read_json()
        if "amount_cents" in payload:
            raise HttpError(400, "validation failed", {
                "amount_cents": "changing the value needs a reason; use revise"})
        result = db.update_customer_po(po["customer_po_id"], payload, user["id"])
        if result is None:
            raise HttpError(404, "not found")
        return 200, result

    @router.route("/api/pos/{po_id}/move", role=ROLE_WRITE, method="POST")
    def move_po(handler, user, po_id):
        """Put an order on the right project.

        Not admin-only: putting a PO on the wrong project is an ordinary
        slip made while typing, and requiring an admin to undo it would make
        the mistake more expensive than it is. The claim guard is what keeps
        it safe.
        """
        po = _owned_po(db, user, po_id)
        payload = handler.read_json()
        target = db.query_one(
            "SELECT id, entity_id FROM project WHERE id = ?",
            (payload.get("project_id"),))
        if target is None or target["entity_id"] not in entity_ids(user):
            raise HttpError(400, "validation failed",
                            {"project_id": "required; a project on this entity"})
        blocked = db.customer_po_is_movable(po["customer_po_id"])
        if blocked:
            raise HttpError(409, blocked)
        try:
            result = db.move_customer_po(
                po["customer_po_id"], target["id"], user["id"])
        except ValueError as e:
            raise HttpError(409, str(e))
        if result is None:
            raise HttpError(404, "not found")
        return 200, result

    @router.route("/api/pos/{po_id}", role=ROLE_DELETE, method="DELETE")
    def delete_po(handler, user, po_id):
        po = _owned_po(db, user, po_id)
        blocked = db.customer_po_is_deletable(po["customer_po_id"])
        if blocked:
            raise HttpError(409, blocked)
        db.delete_customer_po(po["customer_po_id"], user["id"])
        handler._send(204, b"", content_type="application/json")
        return None

    @router.route("/api/renewals", role=ROLE_READ)
    def renewals(handler, user):
        """Agreements coming up, for the register to surface.

        They live on the Schedules screen, but nothing sends you there --
        and a maintenance agreement that lapses unnoticed is revenue that
        simply stops. Only what needs attention is returned; a list of
        renewals due in eight months is a list nobody reads.
        """
        ids = entity_ids(user)
        if not ids:
            return 200, {"renewals": []}
        rows = [dict(r) for r in db.upcoming_renewals(entity_ids=ids)
                if r["renewal_state"] in ("overdue", "due")]
        return 200, {"renewals": rows}

    @router.route("/api/reference", role=ROLE_READ)
    def reference(handler, user):
        """Everything a project form needs to render, in one request."""
        ids = entity_ids(user)
        marks = ",".join("?" * len(ids)) if ids else "NULL"
        return 200, {
            "clients": db.query(
                f"SELECT id, name, entity_id FROM client "
                f"WHERE entity_id IN ({marks}) ORDER BY name", tuple(ids)),
            "types": db.query(
                "SELECT id, code, name FROM project_type ORDER BY code"),
            "statuses": list(STATUSES),
            "leads": [r["project_lead"] for r in db.query(
                "SELECT DISTINCT project_lead FROM project "
                "WHERE project_lead IS NOT NULL AND project_lead <> '' "
                "ORDER BY project_lead")],
            "entities": db.query(
                f"SELECT id, code, name FROM entity WHERE id IN ({marks})",
                tuple(ids)),
        }

    @router.route("/api/projects", role=ROLE_WRITE, method="POST")
    def create_project(handler, user):
        payload = handler.read_json()
        ids = entity_ids(user)
        entity_id = payload.get("entity_id") or (ids[0] if ids else None)
        if entity_id not in ids:
            raise HttpError(403, "no access to that entity")
        fields, errors = validate(db, payload, entity_id)
        if errors:
            raise HttpError(400, "validation failed", errors)
        fields["entity_id"] = entity_id
        client = _resolve_client_field(db, fields, entity_id, user["id"])
        # The job number is allocated inside this transaction, so abandoning
        # the form never burns one.
        created = db.create_project(fields, user["id"])
        if client:
            created["client_resolved"] = client
        return 201, created

    @router.route("/api/projects/{project_id}", role=ROLE_WRITE, method="PATCH")
    def update_project(handler, user, project_id):
        ids = entity_ids(user)
        row = db.query_one(
            "SELECT id, entity_id FROM project WHERE id = ?", (project_id,))
        if row is None or row["entity_id"] not in ids:
            raise HttpError(404, "not found")
        payload = handler.read_json()
        unknown = [k for k in payload
                   if k not in db.MUTABLE and k not in ("id", "client_name")]
        if unknown:
            # Silently ignoring an unknown field means a typo'd key looks
            # like a successful save that changed nothing.
            raise HttpError(400, "validation failed",
                            {k: "not an editable field" for k in unknown})
        fields, errors = validate(db, payload, row["entity_id"],
                                  existing=row["id"])
        if errors:
            raise HttpError(400, "validation failed", errors)
        client = _resolve_client_field(db, fields, row["entity_id"], user["id"])
        result = db.update_project(row["id"], fields, user["id"])
        if result is None:
            raise HttpError(404, "not found")
        if client:
            result["client_resolved"] = client
        return 200, result

    @router.route("/api/projects/{project_id}/job-code", role=ROLE_DELETE,
                  method="POST")
    def change_job_code(handler, user, project_id):
        """Correcting a job code is admin-only and needs a reason.

        Separate from PATCH on purpose: `job_code` stays immutable through
        the ordinary edit path, because reassigning one breaks every
        downstream reference. This is the deliberate exception, priced
        accordingly.
        """
        ids = entity_ids(user)
        row = db.query_one(
            "SELECT id, entity_id FROM project WHERE id = ?", (project_id,))
        if row is None or row["entity_id"] not in ids:
            raise HttpError(404, "not found")
        payload = handler.read_json()
        code = (payload.get("job_code") or "").strip()
        reason = (payload.get("reason") or "").strip()
        errors = {}
        if not code:
            errors["job_code"] = "required"
        elif not JOB_CODE_PATTERN.match(code):
            errors["job_code"] = "letters, digits, - _ / only"
        if not reason:
            errors["reason"] = "required: say why the current code is wrong"
        if errors:
            raise HttpError(400, "validation failed", errors)
        blocked = db.job_code_is_changeable(row["id"])
        if blocked:
            raise HttpError(409, blocked)
        try:
            result = db.change_job_code(row["id"], code, reason, user["id"])
        except ValueError as e:
            raise HttpError(409, str(e))
        if result is None:
            raise HttpError(404, "not found")
        return 200, result

    @router.route("/api/projects/{project_id}", role=ROLE_DELETE,
                  method="DELETE")
    def delete_project(handler, user, project_id):
        ids = entity_ids(user)
        row = db.query_one(
            "SELECT id, entity_id FROM project WHERE id = ?", (project_id,))
        if row is None or row["entity_id"] not in ids:
            raise HttpError(404, "not found")
        reason = db.project_is_deletable(row["id"])
        if reason:
            raise HttpError(409, reason)
        db.delete_project(row["id"], user["id"])
        handler._send(204, b"", content_type="application/json")
        return None
