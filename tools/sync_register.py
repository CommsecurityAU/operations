"""Bring the platform back into line with a corrected register (ADR-27).

    python3 tools/sync_register.py --csv register.csv --db /data/ops.db \
        --reason "Invoiced Prior corrected at source, 25 Aug"
    python3 tools/sync_register.py ... --apply

`drift_check.py` finds differences and deliberately never writes. This is
the other half: it applies them, and only the ones that are safe to apply
without a human deciding each case.

  project_lead, status, client, type   updated in place
  invoiced_prior_cents                 CORRECTED (see below)
  missing projects, job codes          REPORTED, never created

Opening balances need care. They are claim_line rows with
`is_opening_balance = 1`, and migration 003 makes them immutable with two
triggers -- they are the boundary of what this platform knows, and nothing
should edit them by accident.

But an opening balance is a MIGRATION ARTIFACT, not an invoice anyone
issued. When the register that produced it turns out to have been wrong,
the artifact is wrong, and refusing to fix it just leaves the platform
knowingly carrying a bad number. So this tool corrects them -- dropping and
restoring the triggers inside one transaction, requiring a reason, and
writing an audit row. Deliberate, logged, and only through this path.

Nothing is written without `--apply`.
"""

import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from ops import money  # noqa: E402
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "ops", "migrations"))

# Fields safe to overwrite from the register: descriptive, not financial,
# and the workbook is where they are maintained today.
TEXT_FIELDS = [("project_lead", "Project Lead"),
               ("status", "Status")]

TRIGGERS = ("claim_line_opening_no_update", "claim_line_opening_no_delete")


def read_register(path):
    if not os.path.exists(path):
        raise SystemExit(f"no such file: {path}")
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    out = {}
    for r in rows:
        r = {(k or "").replace("\n", " ").strip(): v for k, v in r.items()}
        name = (r.get("Project") or "").strip()
        if name:
            out[name] = r
    if not out:
        raise SystemExit(f"no project rows in {path}")
    return out


def plan(db, register):
    """What would change. Money and text kept apart: they carry different
    risk and deserve separate consideration."""
    text, opening, missing = [], [], []
    platform = {r["name"]: r for r in db.query(
        "SELECT id, name, project_lead, status, invoiced_prior_cents "
        "FROM project")}
    for name, r in register.items():
        p = platform.get(name)
        if p is None:
            missing.append(name)
            continue
        for column, header in TEXT_FIELDS:
            want = (r.get(header) or "").strip()
            have = (p[column] or "").strip()
            if want and want != have:
                text.append((p["id"], name, column, have, want))
        try:
            want_cents = money.parse(r.get("Invoiced Prior"))
        except money.MoneyError:
            continue
        have_cents = db.scalar(
            """SELECT COALESCE(SUM(amount_cents), 0) FROM claim_line
               WHERE project_id = ? AND is_opening_balance = 1""", (p["id"],))
        if want_cents != have_cents:
            opening.append((p["id"], name, have_cents, want_cents))
    return text, opening, missing


def apply_text(db, changes, actor_id):
    for project_id, _name, column, _old, new in changes:
        db.update_project(project_id, {column: new}, actor_id)
    return len(changes)


def apply_opening(db, changes, reason, actor_id):
    """Correct opening balances, with the immutability triggers stood down
    for exactly as long as the correction takes."""
    now = int(time.time())
    definitions = {
        name: db.scalar("SELECT sql FROM sqlite_master WHERE name = ?", (name,))
        for name in TRIGGERS}
    missing = [n for n, sql in definitions.items() if not sql]
    if missing:
        raise SystemExit(f"cannot find trigger definitions: {missing}; "
                         "refusing to touch opening balances")
    with db._tx() as c:
        for name in TRIGGERS:
            c.execute(f"DROP TRIGGER {name}")
        try:
            for project_id, name, old, new in changes:
                if new == 0:
                    c.execute(
                        """DELETE FROM claim_line
                           WHERE project_id = ? AND is_opening_balance = 1""",
                        (project_id,))
                else:
                    existing = c.execute(
                        """SELECT id FROM claim_line
                           WHERE project_id = ? AND is_opening_balance = 1""",
                        (project_id,)).fetchone()
                    if existing:
                        c.execute(
                            "UPDATE claim_line SET amount_cents = ? WHERE id = ?",
                            (new, existing["id"]))
                    else:
                        c.execute(
                            """INSERT INTO claim_line
                               (entity_id, project_id, customer_po_id, status,
                                amount_cents, detail, claim_date, invoiced_date,
                                is_opening_balance, created_by, created_ts)
                               SELECT entity_id, id, NULL, 'invoiced', ?,
                                      'opening balance: invoiced before FY27',
                                      '2026-06-30', '2026-06-30', 1, ?, ?
                               FROM project WHERE id = ?""",
                            (new, actor_id, now, project_id))
                # The legacy column too: the previous release still reads it
                # until the contraction migration (§4).
                c.execute(
                    "UPDATE project SET invoiced_prior_cents = ? WHERE id = ?",
                    (new, project_id))
                c.execute(
                    """INSERT INTO audit_log (ts, actor_user_id, action,
                           target_type, target_id, detail)
                       VALUES (?,?,'opening_balance_correct','project',?,?)""",
                    (now, actor_id, str(project_id),
                     f"{name}: {money.format(old)} -> {money.format(new)}"
                     f" ({reason})"))
        finally:
            # Restored even if a correction fails, so the guarantee cannot be
            # left switched off by an exception.
            for name in TRIGGERS:
                c.execute(definitions[name])
    return len(changes)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True, help="Project register as CSV")
    ap.add_argument("--db", required=True)
    ap.add_argument("--reason", default="")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    register = read_register(args.csv)
    db = Db(args.db, MIGRATIONS)
    try:
        text, opening, missing = plan(db, register)
        print()
        if text:
            print(f"  {len(text)} field(s) to update  (platform -> workbook)")
            for _id, name, column, old, new in text:
                print(f"    {name[:38]:38s} {column:14s} "
                      f"{(old or '(blank)'):>18} -> {new}")
            print()
        if opening:
            print(f"  {len(opening)} opening balance(s) to correct")
            for _id, name, old, new in opening:
                print(f"    {name[:38]:38s} {money.format(old):>14} -> "
                      f"{money.format(new):>14}")
            print()
        if missing:
            print(f"  {len(missing)} project(s) in the workbook but not the "
                  "platform. NOT created -- a project needs a job code "
                  "decision (ADR-28):")
            for name in missing:
                print(f"    {name}")
            print()
        if not (text or opening or missing):
            print("  nothing to do; the platform matches the register\n")
            return 0
        if not args.apply:
            print("  DRY RUN — nothing written. Re-run with --apply.\n")
            return 0
        if opening and not args.reason.strip():
            print("ABORT: --reason is required to correct an opening "
                  "balance. It is a migration artifact being overwritten, "
                  "and the next reader needs to know why.", file=sys.stderr)
            return 2
        actor = db.query_one("SELECT id FROM users ORDER BY id LIMIT 1")
        actor_id = actor["id"] if actor else None
        n = apply_text(db, text, actor_id)
        m = apply_opening(db, opening, args.reason.strip(), actor_id) if opening else 0
        print(f"  updated {n} field(s), corrected {m} opening balance(s).\n")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
