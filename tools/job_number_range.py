"""Reserve the block of job numbers this platform may allocate (ADR-29).

    python3 tools/job_number_range.py --db /data/ops.db            # show
    python3 tools/job_number_range.py --db /data/ops.db \
        --from 9000 --to 9999 --note "agreed with <name>, <date>"

Until a range is reserved, allocation REFUSES. That is the safe default and
not an oversight: iTrade still issues from the general series, so a number
allocated here without an agreed block could be handed out there tomorrow,
and the collision would surface only when both reached Xero with invoices
against each.

The range must be agreed with whoever runs iTrade before it is set here.
This tool records the agreement; it cannot make one.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "ops", "migrations"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--from", dest="start", type=int)
    ap.add_argument("--to", dest="end", type=int)
    ap.add_argument("--note", default="")
    args = ap.parse_args(argv)

    db = Db(args.db, MIGRATIONS)
    try:
        # Tools do not migrate -- the application does, at boot (§4). But a
        # raw "no such column" tells whoever runs this nothing about what to
        # do, so check and say it.
        pending = [v for v in sorted(os.listdir(MIGRATIONS))
                   if v.endswith(".sql")]
        applied = {r["version"] for r in db.query(
            "SELECT version FROM schema_migrations")} \
            if db.query_one("SELECT name FROM sqlite_master "
                            "WHERE name='schema_migrations'") else set()
        missing = [v for v in pending if v not in applied]
        if missing:
            print(f"\n  {args.db} is behind: {', '.join(missing)} not applied."
                  "\n  Migrations run when the app starts -- restart it, then "
                  "try again.\n", file=sys.stderr)
            return 2

        if args.start is None and args.end is None:
            r = db.job_number_range()
            # job_number_sequence has a single row, guaranteed by its own
            # CHECK (id = 1). If it is absent the database is not migrated.
            if r is None:
                print("job_number_sequence is missing; is the database "
                      "migrated?", file=sys.stderr)
                return 2
            if r["range_start"] is None:
                print("\n  No range reserved. Allocation is refused.\n"
                      "  Record the code iTrade gives you instead, or agree a\n"
                      "  block and set it here.\n")
                return 0
            print(f"\n  reserved   JN-{r['range_start']}..JN-{r['range_end']}"
                  f"\n  next       JN-{r['next_value']}"
                  f"\n  remaining  {r['range_end'] - r['next_value'] + 1}"
                  f"\n  note       {r['range_note']}\n")
            return 0

        if args.start is None or args.end is None:
            print("give both --from and --to", file=sys.stderr)
            return 2
        if not args.note.strip():
            # Who agreed it, and when. A reserved range with no provenance is
            # a number someone will later be unable to defend.
            print("--note is required: who agreed this block, and when",
                  file=sys.stderr)
            return 2
        actor = db.query_one("SELECT id FROM users ORDER BY id LIMIT 1")
        try:
            result = db.reserve_job_number_range(
                args.start, args.end, args.note.strip(),
                actor["id"] if actor else None)
        except ValueError as e:
            print(f"REFUSED: {e}", file=sys.stderr)
            return 1
        print(f"\n  reserved JN-{result['range_start']}..JN-{result['range_end']}"
              f"\n  next     JN-{result['next_value']}\n")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
