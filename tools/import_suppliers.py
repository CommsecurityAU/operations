"""Import the supplier list from iTrade.

    python3 tools/import_suppliers.py --db /data/ops.db --csv Suppliers.csv
    python3 tools/import_suppliers.py ... --apply

The iTrade list is the starting authority. Xero has its own reference and is
being brought up to date, so `xero_ref` stays empty until it is -- the
platform reconciles against iTrade in the meantime, the same arrangement the
project register has.

RE-RUNNABLE. Matching is on the iTrade `ID#`, which is stable, so a second
run updates what changed and adds what is new. It never deletes: a supplier
missing from a later export has been retired in iTrade, not erased from
history, and purchase orders may still reference it.

Two columns are dirty in a way worth handling rather than importing:

  `mailto:`   39 rows carry it as a placeholder. An empty column is
              honest; a string that looks like an address is not.
  Phone/Mobile
              Landlines sit in Mobile on nine rows and eight rows fill
              both. They are imported AS THEY STAND into one `phone`
              field, joined -- guessing which is which would replace a
              number that is merely mislabelled with one that is wrong.
"""

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "ops", "migrations"))

#: Named by the Ops Manager, 28 Aug. Any other supplier can be switched over
#: as needed -- the currency on a PURCHASE ORDER is the fact; this is only
#: which one the form offers first.
USD_SUPPLIERS = {"kodelabs", "jinan usr iot technology limited"}

PLACEHOLDER_EMAIL = {"mailto:", "mailto: ", "-", "n/a", "na"}


def read_rows(path):
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
        row = {(k or "").replace("\n", " ").strip(): (v or "").strip()
               for k, v in row.items()}
        name = row.get("Name", "")
        if not name:
            problems.append(f"row {n}: no name")
            continue
        email = row.get("Email", "")
        if email.casefold() in PLACEHOLDER_EMAIL or "@" not in email:
            email = ""
        # Both numbers, as they stand. Which column they were typed into
        # says less about the number than the number does.
        phone = " / ".join(p for p in (row.get("Phone", ""),
                                       row.get("Mobile", "")) if p)
        out.append({
            "itrade_ref": row.get("ID#", "") or None,
            "name": name,
            "contact_name": row.get("Contact", "") or None,
            "phone": phone or None,
            "email": email or None,
            "default_currency": "USD" if name.casefold() in USD_SUPPLIERS
                                else "AUD",
        })
    return out, problems


def plan(db, rows, entity_id):
    existing_by_ref = {s["itrade_ref"]: s for s in db.query(
        "SELECT * FROM supplier WHERE entity_id = ? AND itrade_ref IS NOT NULL",
        (entity_id,))}
    existing_by_name = {s["name"].casefold(): s for s in db.query(
        "SELECT * FROM supplier WHERE entity_id = ?", (entity_id,))}
    new, changed, unchanged = [], [], 0
    seen = set()
    for row in rows:
        found = existing_by_ref.get(row["itrade_ref"]) \
            or existing_by_name.get(row["name"].casefold())
        if found is None:
            new.append(row)
            continue
        seen.add(found["id"])
        diffs = {k: (found[k], v) for k, v in row.items()
                 if k != "default_currency" and found[k] != v}
        # Currency only moves TOWARDS what the list says when the list is
        # explicit: a supplier switched to USD by hand should not revert on
        # the next import.
        if row["default_currency"] == "USD" and found["default_currency"] != "USD":
            diffs["default_currency"] = (found["default_currency"], "USD")
        if diffs:
            changed.append((found, row, diffs))
        else:
            unchanged += 1
    retired = [s for s in existing_by_name.values() if s["id"] not in seen
               and s["is_active"]]
    return new, changed, unchanged, retired


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--entity", type=int, default=1)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    rows, problems = read_rows(args.csv)
    db = Db(args.db, MIGRATIONS)
    try:
        new, changed, unchanged, retired = plan(db, rows, args.entity)
        print()
        print(f"  {len(rows)} supplier(s) in the file")
        print(f"  {len(new)} new, {len(changed)} changed, {unchanged} unchanged")
        for row in new[:12]:
            print(f"    + {row['name'][:44]:44s} {row['default_currency']}"
                  + (f"  iTrade {row['itrade_ref']}" if row["itrade_ref"] else ""))
        if len(new) > 12:
            print(f"    ... and {len(new) - 12} more")
        for found, _row, diffs in changed[:10]:
            for field, (was, now) in diffs.items():
                print(f"    ~ {found['name'][:34]:34s} {field}: "
                      f"{was!r} -> {now!r}")
        if retired:
            # Not deleted: a purchase order may reference it, and a name
            # missing from a later export has been retired rather than
            # erased.
            print(f"\n  {len(retired)} supplier(s) are not in this file and "
                  "stay as they are:")
            for row in retired[:8]:
                print(f"    {row['name']}")
            if len(retired) > 8:
                print(f"    ... and {len(retired) - 8} more")
        if problems:
            print(f"\n  {len(problems)} row(s) skipped:")
            for text in problems[:10]:
                print(f"    {text}")
        usd = [r for r in rows if r["default_currency"] == "USD"]
        print(f"\n  {len(usd)} supplier(s) default to USD: "
              + ", ".join(r["name"] for r in usd))
        if not (new or changed):
            print("\n  nothing to do\n")
            return 0
        if not args.apply:
            print("\n  DRY RUN — nothing written. Re-run with --apply.\n")
            return 0
        actor = db.query_one("SELECT id FROM users ORDER BY id LIMIT 1")
        actor_id = actor["id"] if actor else None
        created = db.create_suppliers(
            [dict(r, entity_id=args.entity) for r in new], actor_id)
        updated = db.update_suppliers(
            [(found["id"], {f: v for f, (_w, v) in diffs.items()})
             for found, _row, diffs in changed], actor_id)
        print(f"\n  added {created}, updated {updated}.\n")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
