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
MAX_NAME = 200
MAX_MONEY_CENTS = 100_000_000_00      # $100m: a typo, not a project

PROJECT_SELECT = """
    SELECT v.project_id   AS id,
           v.project_name AS name,
           v.job_code, v.status, v.purchase_order_cents,
           v.invoiced_prior_cents, v.orders_in_hand_cents,
           p.needs_resolution, p.project_lead, p.project_no, p.notes,
           p.client_id, p.type_id,
           COALESCE(pt.code, '(untyped)') AS type,
           COALESCE(c.name, '(no client)') AS client
    FROM v_project_orders_in_hand v
    JOIN project p ON p.id = v.project_id
    LEFT JOIN project_type pt ON pt.id = p.type_id
    LEFT JOIN client c ON c.id = p.client_id
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
