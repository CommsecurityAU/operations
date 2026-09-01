"""Import the Office Expenses matrix.

    python3 tools/import_expenses.py --db /data/ops.db --csv "Office Expenses.csv"
    python3 tools/import_expenses.py ... --apply

A KEY (the category), a line, and eighteen months of figures. The header
sits on the fourth row under a title block and a totals row.

WHAT IT INFERS, AND WHAT IT REFUSES TO.

A WAGE IS ANNUAL SALARY OVER TWELVE. `Finau` goes from $5,833.33 to
$7,083.33 in Oct-26, which is $70,000 to $85,000. The importer detects the
change and records a SALARY REVISION from that month, so a rise is one fact
rather than twelve figures. Where a monthly figure is not a clean twelfth of
anything, it is stored as entered and said so.

`(Forecasted)` in a name means a cost that is real for planning and not yet
real for paying. The flag is kept and the label is left alone.

THE STATE IS NOT IN THE SHEET. One employee is in NSW and it changes both
Work Cover and Payroll Tax, so it is passed in: `--nsw "Justin Anders"`.
Guessing it from a name would be guessing at somebody's employment.

THE STATUTORY AMOUNTS ARE IMPORTED AS STATED. VIC Work Cover is exactly
1.785% of VIC wages plus VIC super, but VIC payroll tax comes to a constant
1.1237 times that same base, and the NSW figures do not track the one NSW
employee. Three of the four bases are not derivable from the values, so the
rate and the state are recorded and the figures are the sheet's own.
"""

import argparse
import csv
import os
import re
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from ops import money  # noqa: E402
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "ops", "migrations"))

MONTH = re.compile(r"^[A-Z][a-z]{2}-\d\d$")

#: Which categories drive the statutory bases. Everything else is an
#: ordinary cost.
KINDS = {"wages": "wages", "superannuation": "super",
         "work cover": "statutory", "payroll tax": "statutory"}

#: A rate stated in the line's own name: `Work Cover 1.785%`,
#: `Payroll Tax (NSW) 5.45`. Read rather than assumed, because the rates
#: change and the name is where they are written down.
RATE = re.compile(r"(\d+(?:\.\d+)?)\s*%?\s*$")


def read_matrix(path):
    if not os.path.exists(path):
        folder = os.path.dirname(os.path.abspath(path)) or "."
        nearby = sorted(f for f in os.listdir(folder)
                        if f.lower().endswith(".csv")) if os.path.isdir(folder) else []
        raise SystemExit(
            f"no such file: {path}"
            + (("\n  CSV files in that folder:\n    " + "\n    ".join(nearby))
               if nearby else ""))
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    header_at = None
    for i, row in enumerate(rows[:20]):
        cells = [c.strip() for c in row]
        if cells and cells[0] == "Key" and any(MONTH.match(c) for c in cells):
            header_at = i
            break
    if header_at is None:
        raise SystemExit(f"{os.path.basename(path)}: no header row starting "
                         "with `Key` and carrying months")
    header = [c.strip() for c in rows[header_at]]
    months = [(i, c) for i, c in enumerate(header) if MONTH.match(c)]
    lines = []
    for row in rows[header_at + 1:]:
        cells = [c.strip() for c in row] + [""] * len(header)
        if not cells[0] or not cells[1]:
            continue
        amounts = {}
        for i, label in months:
            raw = cells[i]
            if not raw:
                continue
            try:
                amounts[label] = money.parse(raw)
            except money.MoneyError:
                continue
        lines.append({"category": cells[0], "name": cells[1],
                      "amounts": amounts})
    return lines, [label for _i, label in months]


def salary_steps(amounts, months):
    """Where a run of equal monthly figures is a clean twelfth of an annual
    salary, the salary is the fact and the months are its consequence.

    Returns [(month, annual_cents)] at each change, or None where the
    figures are not salary-shaped -- which is most lines.
    """
    steps, last = [], None
    for label in months:
        cents = amounts.get(label)
        if cents is None:
            continue
        annual = cents * 12
        # A twelfth of a whole number of dollars, give or take the rounding
        # the sheet itself does: $70,000/12 is $5,833.333...
        if abs(annual - round(annual, -2)) > 12:
            return None
        annual = round(annual, -2)
        if annual != last:
            steps.append((label, annual))
            last = annual
    return steps or None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--entity", type=int, default=1)
    ap.add_argument("--nsw", action="append", default=[], metavar="NAME",
                    help="an employee based in NSW; repeatable")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    lines, months = read_matrix(args.csv)
    nsw = {n.strip().casefold() for n in args.nsw}
    db = Db(args.db, MIGRATIONS)
    try:
        if db.scalar("SELECT COUNT(*) FROM expense_line"):
            print("ABORT: office expenses are already imported; this "
                  "importer is one-shot.", file=sys.stderr)
            return 2

        unknown_months = [m for m in months
                          if db.scalar("SELECT id FROM period WHERE label = ?",
                                       (m,)) is None]
        categories = {}
        for line in lines:
            categories.setdefault(line["category"], 0)
            categories[line["category"]] += 1

        salaried, entered, empty = [], [], []
        for line in lines:
            if not line["amounts"]:
                empty.append(line)
            elif KINDS.get(line["category"].casefold()) == "wages" \
                    and salary_steps(line["amounts"], months):
                salaried.append(line)
            else:
                entered.append(line)

        total = sum(sum(l["amounts"].values()) for l in lines)
        print()
        print(f"  {len(lines)} line(s) in {len(categories)} categories, "
              f"{len(months)} months, {money.format(total)}")
        if unknown_months:
            print(f"  {len(unknown_months)} month(s) are not periods: "
                  + ", ".join(unknown_months))
        print(f"  {len(salaried)} salaried, {len(entered)} entered, "
              f"{len(empty)} with no figures")
        print("\n  categories:")
        for name, count in categories.items():
            kind = KINDS.get(name.casefold(), "expense")
            print(f"    {name[:34]:34s} {count:>3} line(s)   {kind}")
        if salaried:
            print("\n  salaries, and where they change:")
            for line in salaried:
                steps = salary_steps(line["amounts"], months) or []
                where = ", ".join(f"{m} {money.format(a)}" for m, a in steps)
                flag = " (forecast)" if "forecast" in line["name"].lower() else ""
                state = "NSW" if line["name"].casefold() in nsw else "VIC"
                print(f"    {line['name'][:30]:30s} {state}  {where}{flag}")
        if nsw:
            found = {l["name"].casefold() for l in lines}
            for name in sorted(nsw - found):
                print(f"\n  --nsw {name!r} matches no line in the sheet")
        else:
            print("\n  no --nsw given: everyone is treated as VIC, and Work "
                  "Cover and Payroll Tax\n  are state schemes at different "
                  "rates.")
        if empty:
            print(f"\n  {len(empty)} line(s) carry no figures and are created "
                  "anyway, so they can be filled in:")
            for line in empty[:8]:
                print(f"    {line['category'][:22]:22s} {line['name']}")
            if len(empty) > 8:
                print(f"    ... and {len(empty) - 8} more")

        if not args.apply:
            print("\n  DRY RUN — nothing written. Re-run with --apply.\n")
            return 0

        actor = db.query_one("SELECT id FROM users ORDER BY id LIMIT 1")
        actor_id = actor["id"] if actor else None
        made = db.import_expense_matrix(
            args.entity, lines, months, nsw, KINDS, salary_steps, actor_id)
        print(f"\n  imported {made['categories']} categories, "
              f"{made['lines']} lines, {made['amounts']} monthly figures, "
              f"{made['salaries']} salary revision(s).\n")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
