"""Rebuild claim plans from the claims that exist.

    python3 tools/rebuild_plans.py --db /data/ops.db
    python3 tools/rebuild_plans.py --db /data/ops.db --apply
    python3 tools/rebuild_plans.py --db /data/ops.db --project "200 Victoria - IBP" --apply

A plan is DERIVED from the claims, so it can always be rebuilt from them.
That matters after anything that changes what the claims say -- a
re-import, a `backfill_task` run, or a correction at source -- because the
plan was built from what the claims said BEFORE.

The panel has a button for this, but it rebuilds one project and the panel
closes as it reloads, so the result is not where the action was. This does
the lot and prints what changed.

Nothing is written without `--apply`.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from ops import money  # noqa: E402
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "ops", "migrations"))


def describe(db, project_id):
    return [(i["name"], i["value_cents"], i["allocation_count"])
            for i in db.plan_health(project_id)["items"]]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--project", help="one project by name; default is every "
                                      "project that has a plan")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    db = Db(args.db, MIGRATIONS)
    try:
        if args.project:
            rows = db.query("SELECT id, name FROM project WHERE name = ?",
                            (args.project,))
            if not rows:
                raise SystemExit(f"no project named {args.project!r}")
        else:
            rows = db.query(
                """SELECT DISTINCT p.id, p.name FROM project p
                   JOIN claim_item i ON i.project_id = p.id
                   ORDER BY p.name""")
        if not rows:
            print("\n  no project has a plan yet; nothing to rebuild\n")
            return 0

        actor = db.query_one("SELECT id FROM users ORDER BY id LIMIT 1")
        actor_id = actor["id"] if actor else None
        print()
        changed = 0
        for row in rows:
            before = describe(db, row["id"])
            if not args.apply:
                # Say what it WOULD be, without writing: adoption is the
                # only thing that knows how the claims group, so the shape
                # is described from the claims rather than predicted.
                claims = db.query(
                    """SELECT task, phase FROM claim_line
                       WHERE project_id = ? AND is_opening_balance = 0
                         AND period_id IS NOT NULL AND amount_cents <> 0""",
                    (row["id"],))
                names = {db.plan_group_name(c["task"] or c["phase"], row["name"])
                         for c in claims}
                if len(names) != len(before):
                    changed += 1
                    print(f"  {row['name']}")
                    print(f"    now  {len(before)} item(s): "
                          f"{', '.join(n for n, _v, _a in before)[:80]}")
                    print(f"    would be {len(names)} item(s): "
                          f"{', '.join(sorted(names))[:80]}")
                continue
            result = db.adopt_claims_into_plan(row["id"], actor_id, rebuild=True)
            after = describe(db, row["id"])
            if [n for n, _v, _a in before] != [n for n, _v, _a in after]:
                changed += 1
                print(f"  {row['name']}: {len(before)} -> {len(after)} item(s)")
                for name, value, months in after:
                    print(f"    {name[:44]:44s} {money.format(value):>12}  "
                          f"{months} month(s)")
            else:
                print(f"  {row['name']}: unchanged ({result['items']} items)")
        if not args.apply:
            print(f"\n  {changed} plan(s) would change. "
                  "DRY RUN — nothing written.\n")
        else:
            print(f"\n  {changed} plan(s) changed.\n")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
