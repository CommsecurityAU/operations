"""Bring the platform back into line with a corrected register (ADR-27).

    python3 tools/sync_register.py --csv register.csv --db /data/ops.db \
        --reason "Invoiced Prior corrected at source, 25 Aug"
    python3 tools/sync_register.py ... --apply

`drift_check.py` finds differences and deliberately never writes. This is
the other half: it applies them, and only the ones that are safe to apply
without a human deciding each case.

  project_lead, status, client, type   updated in place
  Retention %                          applied to the project's PO(s)
  invoiced_prior_cents                 CORRECTED (see below)
  missing projects                     created with --create-missing

A project is created using THE JOB CODE THE REGISTER CARRIES. That is not
the platform inventing a number (ADR-28 forbids that) -- it is recording a
decision already made in the workbook. A blank or placeholder code becomes
`TBA` and the project lands on the worklist, which is the same outcome as
creating one through the UI without a number.

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

# Accepted spellings for the milestone dates. The register may name them
# either way; both mean the same thing.
DATE_FIELDS = [("practical_completion_date",
                ("Practical Completion", "PC Date", "Practical Completion Date")),
               ("dlp_end_date",
                ("DLP End", "DLP End Date", "End of DLP", "DLP"))]

# The register carries one number: the CAP, as a percentage of contract.
# The rest is the standard agreement (25 Aug): 10% withheld from each claim
# until the cap is reached, then half released at practical completion and
# half at the end of the DLP, which typically runs 12 months from PC.
RETENTION_RATE_BP = 1000        # 10% per claim
RETENTION_POLICY = "split"
RETENTION_SPLIT_BP = 5000       # half at practical completion


def normalise_date(raw):
    """`31/03/2027`, `2027-03-31` and `31-03-2027` all mean the same day.

    Returns None rather than guessing when the text is not a date, so an
    unparseable cell is REPORTED instead of silently skipped -- a milestone
    date that quietly failed to import is a retention release that never
    appears in a forecast.
    """
    text = (raw or "").strip()
    if not text:
        return None
    for sep in ("-", "/"):
        parts = text.split(sep)
        if len(parts) != 3 or not all(p.strip().isdigit() for p in parts):
            continue
        a, b, c = (int(p) for p in parts)
        if a > 31:                       # yyyy-mm-dd
            y, m, d = a, b, c
        else:                            # dd/mm/yyyy
            d, m, y = a, b, c
            if y < 100:
                y += 2000
        if 1 <= m <= 12 and 1 <= d <= 31 and 2000 <= y <= 2100:
            return f"{y:04d}-{m:02d}-{d:02d}"
    return None


def retention_cap_bp(raw):
    """`5.00%` -> 500 basis points. Blank or zero means none."""
    text = (raw or "").strip().replace("%", "")
    if not text:
        return 0
    try:
        whole, _, frac = text.partition(".")
        return int(whole or 0) * 100 + int((frac + "00")[:2])
    except ValueError:
        return 0


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
    text, opening, missing, retention, dates = [], [], [], [], []
    platform = {r["name"]: r for r in db.query(
        "SELECT id, name, project_lead, status, invoiced_prior_cents, "
        "practical_completion_date, dlp_end_date FROM project")}
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
        for column, headers in DATE_FIELDS:
            raw = next(((r.get(h) or "").strip() for h in headers
                        if (r.get(h) or "").strip()), "")
            if not raw:
                continue
            iso = normalise_date(raw)
            if iso is None:
                dates.append((p["id"], name, column, p[column],
                              f"UNPARSEABLE: {raw!r}"))
            elif iso != (p[column] or ""):
                dates.append((p["id"], name, column, p[column], iso))

        want_cap = retention_cap_bp(r.get("Retention %"))
        have_cap = db.scalar(
            """SELECT COALESCE(MAX(retention_cap_bp), 0) FROM customer_po
               WHERE project_id = ? AND retention_applies = 1""", (p["id"],))
        if want_cap != have_cap:
            retention.append((p["id"], name, have_cap, want_cap))

        try:
            want_cents = money.parse(r.get("Invoiced Prior"))
        except money.MoneyError:
            continue
        have_cents = db.scalar(
            """SELECT COALESCE(SUM(amount_cents), 0) FROM claim_line
               WHERE project_id = ? AND is_opening_balance = 1""", (p["id"],))
        if want_cents != have_cents:
            opening.append((p["id"], name, have_cents, want_cents))
    return text, opening, missing, retention, dates


PLACEHOLDER = {"", "TBA", "NA", "N/A", "TBC", "TBD", "-", "VARIOUS"}


def create_missing(db, register, names, actor_id):
    """Create projects the register has and the platform does not."""
    made = []
    for name in names:
        r = register[name]
        code = (r.get("Job Code") or "").strip()
        if code.upper() in PLACEHOLDER:
            code = "TBA"
        clash = db.query_one(
            "SELECT name FROM project WHERE job_code = ? AND job_code <> 'TBA'",
            (code,))
        if clash:
            made.append((name, None, f"job code {code} already used by "
                                     f"{clash['name']}"))
            continue
        client_name = (r.get("Client") or "").strip()
        client_id = None
        if client_name:
            client_id, _created, _matched = db.resolve_client(
                1, client_name, actor_id)
        type_row = db.query_one(
            "SELECT id FROM project_type WHERE code = ?",
            ((r.get("Type") or "").strip(),))
        try:
            po = money.parse(r.get("Purchase Order"))
            prior = money.parse(r.get("Invoiced Prior"))
        except money.MoneyError as e:
            made.append((name, None, str(e)))
            continue
        project = db.create_project({
            "entity_id": 1, "name": name, "job_code": code,
            "client_id": client_id,
            "type_id": type_row["id"] if type_row else None,
            "status": (r.get("Status") or "Active").strip() or "Active",
            "project_lead": (r.get("Project Lead") or "").strip() or None,
            "project_no": (r.get("Project No") or "").strip() or None,
            "purchase_order_cents": po, "invoiced_prior_cents": prior,
        }, actor_id)
        made.append((name, project["job_code"], None))
    return made


def unknown_statuses(db, changes):
    """Statuses the register uses and the platform does not know.

    Caught HERE, not at the database. `Lost` arrived in the register, the
    apply got as far as one project and raised on a CHECK, leaving the rest
    unattempted -- an importer must fail before it writes (ADR-42).
    """
    known = {r["code"] for r in db.query("SELECT code FROM project_status")}
    return sorted({
        row[3] for row in changes
        if row[2] == "status" and row[3] not in known})


def apply_text(db, changes, actor_id):
    for project_id, _name, column, _old, new in changes:
        db.update_project(project_id, {column: new}, actor_id)
    return len(changes)


def apply_opening(db, changes, reason, actor_id):
    """Correct opening balances, with the immutability triggers stood down
    for exactly as long as the correction takes (`Db._opening_balances_writable`)."""
    now = int(time.time())
    with db._opening_balances_writable() as c:
        for project_id, name, old_cents, new_cents in changes:
            if new_cents == 0:
                c.execute("""DELETE FROM claim_line
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
                        (new_cents, existing["id"]))
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
                        (new_cents, actor_id, now, project_id))
            # The legacy column too: the previous release still reads it
            # until the contraction migration (§4).
            c.execute("UPDATE project SET invoiced_prior_cents = ? WHERE id = ?",
                      (new_cents, project_id))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'opening_balance_correct','project',?,?)""",
                (now, actor_id, str(project_id),
                 f"{name}: {money.format(old_cents)} -> "
                 f"{money.format(new_cents)} ({reason})"))
    return len(changes)


def apply_dates(db, changes, actor_id):
    """Practical completion and DLP end. Without them a retention release
    cannot be placed in a month, and forecasting the release is the point."""
    now = int(time.time())
    with db._tx() as c:
        for project_id, name, column, _old, new in changes:
            c.execute(f"UPDATE project SET {column} = ? WHERE id = ?",
                      (new or None, project_id))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'milestone_date','project',?,?)""",
                (now, actor_id, str(project_id), f"{name}: {column} = {new}"))
    return len(changes)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True, help="Project register as CSV")
    ap.add_argument("--db", required=True)
    ap.add_argument("--reason", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--create-missing", action="store_true",
                    help="create projects the register has and the platform "
                         "does not, using the register's own job code")
    args = ap.parse_args(argv)

    register = read_register(args.csv)
    db = Db(args.db, MIGRATIONS)
    try:
        text, opening, missing, retention, dates = plan(db, register)
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
        bad = [d for d in dates if str(d[4]).startswith("UNPARSEABLE")]
        good = [d for d in dates if d not in bad]
        if good:
            print(f"  {len(good)} milestone date(s) to set")
            for _id, name, column, old_v, new_v in good:
                print(f"    {name[:40]:40s} {column:26s} "
                      f"{(old_v or '(none)'):>12} -> {new_v}")
            print()
        if bad:
            print(f"  {len(bad)} date(s) could NOT be read and will be skipped:")
            for _id, name, column, _o, problem in bad:
                print(f"    {name[:40]:40s} {column:26s} {problem}")
            print()
        if retention:
            print(f"  {len(retention)} project(s) with retention terms to set")
            for _id, name, old_bp, new_bp in retention:
                shown = (f"{new_bp / 100:.2f}% cap, {RETENTION_RATE_BP / 100:.0f}% "
                         f"per claim, split at PC/DLP" if new_bp else "none")
                print(f"    {name[:44]:44s} {old_bp / 100:>5.2f}% -> {shown}")
            print()
        if missing:
            verb = ("will be created" if args.create_missing
                    else "NOT created; pass --create-missing")
            print(f"  {len(missing)} project(s) in the workbook but not the "
                  f"platform ({verb}):")
            for name in missing:
                code = (register[name].get("Job Code") or "").strip() or "(none)"
                print(f"    {name[:44]:44s} job code {code}")
            print()
        if not (text or opening or missing or retention or dates):
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
        if missing and args.create_missing:
            for name, code, problem in create_missing(db, register, missing,
                                                      actor_id):
                print(f"    {name[:44]:44s} "
                      + (f"created as {code}" if code else f"REFUSED: {problem}"))
            print()
            text, _o, _m, retention, dates = plan(db, register)
        apply_dates(db, good, actor_id)
        for project_id, _name, _old_bp, new_bp in retention:
            db.set_retention_terms(
                project_id, new_bp,
                RETENTION_RATE_BP if new_bp else None,
                RETENTION_POLICY if new_bp else None,
                RETENTION_SPLIT_BP if new_bp else None, actor_id)
        unknown = unknown_statuses(db, text)
        if unknown:
            print("\n  ABORT: the register uses status(es) the platform does "
                  "not know:", file=sys.stderr)
            for name in unknown:
                print(f"    {name!r}", file=sys.stderr)
            print("  Add one with:\n"
                  "    INSERT INTO project_status (code, label, is_open) "
                  "VALUES ('Lost','Lost',0);\n"
                  "  `is_open` decides whether it counts as an active "
                  "project.\n", file=sys.stderr)
            return 1
        n = apply_text(db, text, actor_id)
        m = apply_opening(db, opening, args.reason.strip(), actor_id) if opening else 0
        print(f"  updated {n} field(s), corrected {m} opening balance(s), "
              f"set retention on {len(retention)} project(s), "
              f"{len(good)} milestone date(s).\n")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
