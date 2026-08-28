"""Import the orange-flagged estimates from the Project Expenses matrix.

    python3 tools/import_estimates.py --db /data/ops.db --csv "PE Orange.csv"
    python3 tools/import_estimates.py ... --apply

WHAT THESE ARE. Early estimates of future procurement, entered against a
project and a month and flagged orange by whoever put them there. They are
not orders: nothing has been quoted, approved or committed. They belong in
the platform because they ARE the forecast — 31 cells, $1,576,928.29 —
which is ten times the value of the real orders and would make committed
cost meaningless if the two were added together.

So each becomes a `procurement_line` with `is_estimate = 1`, no supplier,
no quote and no order, sitting in the month the matrix put it in.

THE LIFECYCLE. An estimate is REPLACED, not deleted. When the work is
quoted, the same line gains a supplier, a quote and a real cost, and the
flag clears — so the forecast it was holding does not vanish from the month
while somebody types the real one in.

THE COLOUR CANNOT BE READ FROM AN EXPORT. `list-orange-expenses.gs` runs
inside the workbook and writes the `PE Orange` tab this reads. Worth
knowing that the flag is `#FF9900`, Google's palette orange, and NOT the
`#F26722` in the legend cell: the legend records the brand colour, and
whoever flags a cell picks the nearest swatch.
"""

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from ops import money  # noqa: E402
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "ops", "migrations"))

ITEM = "Expense estimate"
NOTE = ("early estimate from the Project Expenses matrix; not yet quoted "
        "or ordered")


def read_flags(path):
    if not os.path.exists(path):
        folder = os.path.dirname(os.path.abspath(path)) or "."
        nearby = sorted(f for f in os.listdir(folder)
                        if f.lower().endswith(".csv")) if os.path.isdir(folder) else []
        raise SystemExit(
            f"no such file: {path}"
            + (("\n  CSV files in that folder:\n    " + "\n    ".join(nearby))
               if nearby else ""))
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    out, problems = [], []
    for n, row in enumerate(rows, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        name = row.get("Project", "")
        month = row.get("EOM", "")
        if not name or not month:
            problems.append(f"row {n}: no project or no month")
            continue
        raw = row.get("Amount", "")
        try:
            # The script writes the raw number, not the formatted cell, so
            # `16974` rather than `$16,974.00`.
            cents = money.parse(raw if raw.startswith("$") else f"${raw}")
        except money.MoneyError:
            problems.append(f"row {n}: {name} {month}: {raw!r} is not an amount")
            continue
        out.append({"project": name, "job_code": row.get("Job Code", ""),
                    "month": month, "cents": cents})
    return out, problems


def resolve(db, flags, entity_id):
    placed, missing = [], []
    for flag in flags:
        project = None
        job = flag["job_code"]
        # The job code first: it is the thing that identifies a project to
        # everyone outside this system. `#N/A` and `TBA` are not codes.
        if job and job not in ("TBA", "#N/A", "\\#N/A", "na"):
            project = db.query_one(
                "SELECT id FROM project WHERE job_code = ? AND entity_id = ?",
                (job, entity_id))
        if project is None:
            project = db.query_one(
                """SELECT id FROM project
                   WHERE name = ? COLLATE NOCASE AND entity_id = ?""",
                (flag["project"], entity_id))
        period_id = db.scalar("SELECT id FROM period WHERE label = ?",
                              (flag["month"],))
        if project is None or period_id is None:
            missing.append((flag, project is None, period_id is None))
            continue
        placed.append((flag, project["id"], period_id))
    return placed, missing


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--entity", type=int, default=1)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    flags, problems = read_flags(args.csv)
    db = Db(args.db, MIGRATIONS)
    try:
        already = db.scalar(
            "SELECT COUNT(*) FROM procurement_line WHERE is_estimate = 1")
        if already:
            print(f"ABORT: {already} estimate(s) are already imported. This "
                  "importer is one-shot; the estimates it made are meant to "
                  "be replaced in place, not re-imported over.",
                  file=sys.stderr)
            return 2

        placed, missing = resolve(db, flags, args.entity)
        total = sum(f["cents"] for f, _p, _q in placed)
        print()
        print(f"  {len(flags)} flagged cell(s)")
        print(f"  {len(placed)} can be placed, {money.format(total)}")
        if problems:
            print(f"\n  {len(problems)} row(s) unreadable:")
            for text in problems:
                print(f"    {text}")
        if missing:
            print(f"\n  {len(missing)} cannot be placed:")
            for flag, no_project, no_period in missing:
                why = " and ".join(
                    w for w, bad in (("no such project", no_project),
                                     ("no such month", no_period)) if bad)
                print(f"    {flag['project'][:38]:38s} {flag['month']:8s} "
                      f"{money.format(flag['cents']):>12}  {why}")
        if not placed:
            print("\n  nothing to import\n")
            return 1

        print(f"\n  by month:")
        by_month = {}
        for flag, _p, _q in placed:
            by_month[flag["month"]] = by_month.get(flag["month"], 0) + flag["cents"]
        for month, cents in sorted(
                by_month.items(),
                key=lambda kv: db.scalar(
                    "SELECT month_start FROM period WHERE label = ?", (kv[0],))
                or ""):
            print(f"    {month:8s} {money.format(cents):>13}")

        if not args.apply:
            print("\n  DRY RUN — nothing written. Re-run with --apply.\n")
            return 0

        actor = db.query_one("SELECT id FROM users ORDER BY id LIMIT 1")
        actor_id = actor["id"] if actor else None
        for flag, project_id, period_id in placed:
            db.create_procurement_line({
                "entity_id": args.entity, "project_id": project_id,
                "period_id": period_id,
                "item": ITEM,
                "description": f"{flag['month']} estimate, from the Project "
                               "Expenses matrix",
                "quantity": 1, "currency": "AUD",
                "unit_cost_cents": flag["cents"],
                "total_cents": flag["cents"],
                "is_estimate": 1,
                # An estimate has not been ordered, and saying so plainly
                # keeps it out of the delivery and payment counts.
                "stated_state": "to be ordered",
                "note": NOTE,
            }, actor_id)
        print(f"\n  imported {len(placed)} estimate(s), {money.format(total)}.")
        print("  They carry no supplier and no order. When one is quoted, "
              "replace it in place\n  rather than adding a second line: the "
              "month keeps its forecast either way.\n")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
