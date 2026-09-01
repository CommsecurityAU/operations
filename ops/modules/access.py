"""Access module — who can do what (STP-1, §9).

Roles are enumerated and NO ROLE IMPLIES ANOTHER. An admin who is not also
a viewer cannot read the register, which has caught us twice and is
deliberate: the alternative is a hierarchy where granting one thing quietly
grants three.

Everything here is admin-only, and an admin cannot remove the last admin on
an entity -- including themselves. A system nobody can administer is one
that needs a database client to recover, which is exactly the situation
this screen exists to end.
"""

from typing import Any

from ops.db import Db
from ops.http_util import HttpError, Router

ROLE_ADMIN = "admin"
#: `finance` opens the office-expense screen -- the rent, the
#: subscriptions, the total cost of running the business. `payroll` is what
#: shows individual salaries, and it is a SEPARATE grant: somebody can see
#: that wages cost $96,250.01 in July without seeing what Justin earns.
#: Neither implies the other, and admin implies neither.
ROLES = ("viewer", "operations", "approver", "admin", "finance",
         "payroll")


def entity_ids(user: dict[str, Any]) -> list[int]:
    return sorted({r["entity_id"] for r in user["roles"]})


def admin_on(user: dict[str, Any], entity_id: int) -> bool:
    return any(r["role"] == ROLE_ADMIN and r["entity_id"] == entity_id
               for r in user["roles"])


def register(router: Router, db: Db) -> None:

    def require_admin(user, entity_id):
        if not admin_on(user, entity_id):
            raise HttpError(403, f"admin on entity {entity_id} is required")

    @router.route("/api/users", role=ROLE_ADMIN)
    def list_users(handler, user):
        ids = entity_ids(user)
        admin_ids = [e for e in ids if admin_on(user, e)]
        if not admin_ids:
            raise HttpError(403, "admin on an entity is required")
        users = db.users_with_roles()
        # Grants on entities this admin does not administer are shown but
        # not editable: hiding them would make a user look less privileged
        # than they are, which is the wrong way for that to be wrong.
        for row in users:
            for grant in row["roles"]:
                grant["editable"] = grant["entity_id"] in admin_ids
        # Only entities that are actually in use. The schema is
        # multi-entity from migration 001, but the interface stays
        # single-entity until a second one has something in it -- otherwise
        # this screen shows three rows per person for one real decision.
        # An entity appears the moment it gains a project or a grant.
        entities = db.query(
            """SELECT e.id, e.name FROM entity e
               WHERE EXISTS (SELECT 1 FROM project p WHERE p.entity_id = e.id)
                  OR EXISTS (SELECT 1 FROM user_entity_role r
                             WHERE r.entity_id = e.id)
               ORDER BY e.id""")
        if not entities:
            entities = db.query("SELECT id, name FROM entity ORDER BY id LIMIT 1")
        return 200, {
            "users": users,
            "roles": list(ROLES),
            "entities": entities,
            "administers": admin_ids,
            "me": user["id"],
        }

    @router.route("/api/users/{user_id}/roles", role=ROLE_ADMIN, method="POST")
    def grant(handler, user, user_id):
        payload = handler.read_json()
        errors = {}
        target = db.query_one("SELECT id, display_name FROM users WHERE id = ?",
                              (user_id,))
        if target is None:
            raise HttpError(404, "not found")
        role = (payload.get("role") or "").strip()
        if role not in ROLES:
            errors["role"] = f"must be one of {', '.join(ROLES)}"
        entity_id = db.scalar("SELECT id FROM entity WHERE id = ?",
                              (payload.get("entity_id"),))
        if entity_id is None:
            errors["entity_id"] = "required"
        if errors:
            raise HttpError(400, "validation failed", errors)
        require_admin(user, entity_id)
        db.grant_role(target["id"], entity_id, role, user["id"])
        return 200, {"granted": role, "entity_id": entity_id,
                     "user": target["display_name"]}

    @router.route("/api/users/{user_id}/roles", role=ROLE_ADMIN, method="DELETE")
    def revoke(handler, user, user_id):
        payload = handler.read_json()
        target = db.query_one("SELECT id, display_name FROM users WHERE id = ?",
                              (user_id,))
        if target is None:
            raise HttpError(404, "not found")
        role = (payload.get("role") or "").strip()
        entity_id = db.scalar("SELECT id FROM entity WHERE id = ?",
                              (payload.get("entity_id"),))
        if role not in ROLES or entity_id is None:
            raise HttpError(400, "validation failed",
                            {"role": "unknown", "entity_id": "unknown"})
        require_admin(user, entity_id)
        if role == ROLE_ADMIN:
            remaining = [uid for uid in db.admins_on(entity_id)
                         if uid != target["id"]]
            if not remaining:
                # Including yourself. A system nobody can administer needs a
                # database client to recover.
                raise HttpError(
                    409, "that is the last admin on this entity; grant "
                         "another before removing this one")
        db.revoke_role(target["id"], entity_id, role, user["id"])
        return 200, {"revoked": role, "entity_id": entity_id,
                     "user": target["display_name"]}

    @router.route("/api/users/{user_id}", role=ROLE_ADMIN, method="PATCH")
    def set_active(handler, user, user_id):
        payload = handler.read_json()
        target = db.query_one(
            "SELECT id, display_name, is_active FROM users WHERE id = ?",
            (user_id,))
        if target is None:
            raise HttpError(404, "not found")
        if "is_active" not in payload:
            raise HttpError(400, "validation failed",
                            {"is_active": "required"})
        active = bool(payload["is_active"])
        # Only an admin of an entity the user has a role on, so an admin of
        # one company cannot switch off someone who works for another.
        theirs = {g["entity_id"] for g in db.query(
            "SELECT entity_id FROM user_entity_role WHERE user_id = ?",
            (target["id"],))}
        mine = {e for e in entity_ids(user) if admin_on(user, e)}
        if theirs and not (theirs & mine):
            raise HttpError(403, "this user has no role on an entity you "
                                 "administer")
        if not active:
            if target["id"] == user["id"]:
                raise HttpError(409, "you cannot switch off your own account")
            for entity_id in theirs & mine:
                remaining = [uid for uid in db.admins_on(entity_id)
                             if uid != target["id"]]
                if not remaining:
                    raise HttpError(
                        409, f"that is the last admin on entity {entity_id}")
        changed = db.set_user_active(target["id"], active, user["id"])
        return 200, {"changed": changed, "is_active": active,
                     "user": target["display_name"]}
