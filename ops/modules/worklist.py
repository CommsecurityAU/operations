"""Worklist module — resolving ambiguous job codes (ADR-23, STP-1/STP-5).

Codes import FLAGGED rather than blocked, so this screen is where they get
decided. Its emptiness is STP-5's gate: a dashboard over unresolved codes is
worse than no dashboard.

The three classes need different things, which is why "resolve" is four
actions rather than one button:

  A  format variant   — none remain; corrected at source
  B  placeholder      — needs a number ISSUED (TBA, na)
  C  shared code      — needs a DECISION recorded; the code may be
                        legitimately shared across work types at one site
"""

from typing import Any

from ops.db import Db
from ops.http_util import HttpError, Router

ROLE_READ = "viewer"
ROLE_RESOLVE = "operations"

ACTIONS = ("issue", "assign", "keep", "dismiss")
# Both leave the register looking wrong to the next reader -- an unnumbered
# project, or two projects on one code. Unexplained, it gets re-raised.
NEEDS_REASON = ("keep", "dismiss")

CLASS_HELP = {
    "A": "Format variant; canonicalised at import.",
    "B": "Placeholder — needs a job number issued, or dismissing as "
         "non-project work.",
    "C": "One customer job number across more than one project. Often "
         "correct (same site, different work type) — record why, or reissue.",
}


def entity_ids(user: dict[str, Any]) -> list[int]:
    return sorted({r["entity_id"] for r in user["roles"]})


def register(router: Router, db: Db) -> None:

    @router.route("/api/worklist", role=ROLE_READ)
    def worklist(handler, user):
        ids = entity_ids(user)
        if not ids:
            return 200, {"issues": [], "open": 0}
        marks = ",".join("?" * len(ids))
        issues = db.query(
            f"""SELECT i.id, i.raw_code, i.class, i.status, i.reason,
                       i.project_id,
                       p.name AS project_name, p.job_code, p.status AS project_status,
                       COALESCE(pt.code, '(untyped)') AS type,
                       COALESCE(c.name, '(no client)') AS client,
                       (SELECT COUNT(*) FROM project s
                        WHERE s.job_code = p.job_code) AS shared_by
                FROM job_code_issue i
                JOIN project p ON p.id = i.project_id
                LEFT JOIN project_type pt ON pt.id = p.type_id
                LEFT JOIN client c ON c.id = p.client_id
                WHERE i.status = 'open' AND p.entity_id IN ({marks})
                ORDER BY i.class, p.name""", tuple(ids))
        return 200, {"issues": issues, "open": len(issues),
                     "help": CLASS_HELP, "next_job_code":
                     f"JN-{db.scalar('SELECT next_value FROM job_number_sequence')}"}

    @router.route("/api/worklist/{issue_id}/resolve", role=ROLE_RESOLVE,
                  method="POST")
    def resolve(handler, user, issue_id):
        payload = handler.read_json()
        action = (payload.get("action") or "").strip()
        reason = (payload.get("reason") or "").strip() or None
        job_code = (payload.get("job_code") or "").strip() or None
        errors = {}

        if action not in ACTIONS:
            errors["action"] = f"must be one of {', '.join(ACTIONS)}"
        if action in NEEDS_REASON and not reason:
            errors["reason"] = "required: say why, or this gets re-raised later"
        if action == "assign":
            if not job_code:
                errors["job_code"] = "required for assign"
            elif len(job_code) > 40:
                errors["job_code"] = "too long"

        ids = entity_ids(user)
        marks = ",".join("?" * len(ids)) if ids else "NULL"
        issue = db.query_one(
            f"""SELECT i.id, i.class, i.status, i.project_id
                FROM job_code_issue i
                JOIN project p ON p.id = i.project_id
                WHERE i.id = ? AND p.entity_id IN ({marks})""",
            (issue_id, *ids))
        if issue is None:
            raise HttpError(404, "not found")
        if issue["status"] != "open":
            raise HttpError(409, "already resolved")
        # The schema CHECK enforces this too, but an IntegrityError reaches
        # the user as "internal error".
        # Only for the judgement calls. Reissuing a shared code resolves it
        # by definition, so demanding a typed justification for `issue` was
        # blocking the most natural action on a class C row.
        if issue["class"] == "C" and action in NEEDS_REASON and not reason:
            errors.setdefault(
                "reason",
                "required for a shared code: the next reader needs to know "
                "whether this is deliberate")
        if errors:
            raise HttpError(400, "validation failed", errors)

        result = db.resolve_issue(issue["id"], action, user["id"],
                                  job_code=job_code, reason=reason)
        if result is None:
            raise HttpError(409, "already resolved")
        return 200, result
