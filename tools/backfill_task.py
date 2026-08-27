"""Fill `claim_line.task` from the workbook, for claims already imported.

    python3 tools/backfill_task.py --db /data/ops.db \
        --invoicing "Invoicing.csv" --future "Future Invoicing.csv"
    python3 tools/backfill_task.py ... --apply

The importer mapped the workbook's `Phase` column and not its `Task`,
folding the task into `detail` and leaving `claim_line.task` empty on every
imported claim. `Task` is the LINE ITEM -- `Client Training`, `SAT`,
`Design - Stage 2` -- and the claim plan groups on it, so five tasks
collapsed into the single phase above them and four became invisible.

The importer is fixed, but that only helps a fresh import. This fills in
what is already there, without touching anything else.

MATCHING. A claim carries no workbook row id, so rows are matched on
project, month and amount -- the same natural key the incremental sync
uses. Where a project/month holds several claims of the SAME amount, the
tasks are assigned in file order: `200 Victoria` has five Sep-26 claims at
$17,700 each, and the workbook lists them in the order they were typed. That
is an assumption, so it is REPORTED rather than made quietly, and the claim
that already carries a matching detail is preferred first.

Nothing is written without `--apply`.
"""

import argparse
import collections
import csv
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from ops import money  # noqa: E402
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "ops", "migrations"))


def read_rows(path, month_column):
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
    out = []
    for row in rows:
        row = {(k or "").replace("\n", " ").strip(): v for k, v in row.items()}
        name = (row.get("Project") or "").strip()
        task = (row.get("Task") or "").strip()
        month = (row.get(month_column) or "").strip()
        if not name or not task or not month:
            continue
        try:
            cents = money.parse(row.get("Invoice Amount"))
        except money.MoneyError:
            continue
        out.append({"project": name, "month": month, "cents": cents,
                    "task": task, "detail": (row.get("Detail") or "").strip()})
    return out


def plan(db, rows):
    """Which claims would gain a task, and where the answer is a guess."""
    wanted = collections.defaultdict(list)
    for row in rows:
        wanted[(row["project"].casefold(), row["month"], row["cents"])].append(row)

    claims = db.query(
        """SELECT cl.id, p.name AS project, pe.label AS month,
                  cl.amount_cents, cl.detail, cl.task
           FROM claim_line cl
           JOIN project p ON p.id = cl.project_id
           JOIN period pe ON pe.id = cl.period_id
           WHERE cl.is_opening_balance = 0
           ORDER BY cl.id""")

    by_key = collections.defaultdict(list)
    for claim in claims:
        by_key[(claim["project"].casefold(), claim["month"],
                claim["amount_cents"])].append(claim)

    updates, guessed, unmatched = [], [], []
    for key, group in by_key.items():
        candidates = list(wanted.get(key, []))
        if not candidates:
            unmatched.extend(c for c in group if not c["task"])
            continue
        remaining = [c for c in group if not c["task"]]
        # Prefer an exact detail match: where the importer wrote the task
        # into `detail`, the pairing is certain rather than positional.
        for claim in list(remaining):
            match = next((r for r in candidates
                          if r["task"] and claim["detail"]
                          and r["task"].casefold() == claim["detail"].casefold()),
                         None)
            if match:
                updates.append((claim["id"], match["task"], claim, False))
                candidates.remove(match)
                remaining.remove(claim)
        # Whatever is left pairs in file order, which is an assumption.
        for claim, row in zip(remaining, candidates):
            updates.append((claim["id"], row["task"], claim, True))
            guessed.append((claim, row["task"]))
    return updates, guessed, unmatched


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--invoicing", required=True)
    ap.add_argument("--future", required=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    rows = read_rows(args.invoicing, "EOM") + read_rows(args.future, "EOM Cycle")
    db = Db(args.db, MIGRATIONS)
    try:
        already = db.scalar(
            "SELECT COUNT(*) FROM claim_line WHERE task IS NOT NULL")
        updates, guessed, unmatched = plan(db, rows)
        print()
        print(f"  {len(rows)} workbook rows carry a task")
        print(f"  {already} claim(s) already have one")
        print(f"  {len(updates)} claim(s) would gain one")
        if guessed:
            print(f"\n  {len(guessed)} matched BY POSITION, because several "
                  "claims share a project, month and amount:")
            for claim, task in guessed[:20]:
                print(f"    {claim['project'][:34]:34s} {claim['month']:8s} "
                      f"{money.format(claim['amount_cents']):>12}  -> {task}")
            if len(guessed) > 20:
                print(f"    ... and {len(guessed) - 20} more")
        if unmatched:
            print(f"\n  {len(unmatched)} claim(s) have no matching workbook "
                  "row and keep an empty task:")
            for claim in unmatched[:10]:
                print(f"    {claim['project'][:34]:34s} {claim['month']:8s} "
                      f"{money.format(claim['amount_cents']):>12}")
            if len(unmatched) > 10:
                print(f"    ... and {len(unmatched) - 10} more")
        if not updates:
            print("\n  nothing to do\n")
            return 0
        if not args.apply:
            print("\n  DRY RUN — nothing written. Re-run with --apply.\n")
            return 0
        actor = db.query_one("SELECT id FROM users ORDER BY id LIMIT 1")
        db.set_claim_tasks([(claim_id, task) for claim_id, task, _c, _g
                            in updates], actor["id"] if actor else None)
        print(f"\n  filled {len(updates)} task(s).")
        print("  Rebuild the claim plan on any project that has one, so it "
              "picks up the line items.\n")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
