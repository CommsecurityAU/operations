"""Import the FY27 project register (Operations workbook, Project List tab).

One-shot migration tool. Never shipped in the image.

    python3 tools/import_register.py --csv register.csv --db /data/ops.db [--dry-run]

Export the Project List tab as CSV first: File > Download > CSV, with the
header row `Project, Client, Job Code, ..., Invoiced Prior, Contract Value FY27`.

The register asserts itself (ADR-22):

    Purchase Order == Invoiced Prior + Contract Value FY27

per row. That is checked, not derived. A failure is a hard stop, because the
one defect this import must not produce is a project whose opening position is
silently wrong -- it reconciles at every total and stays invisible until
someone questions a single project.

Ambiguous job codes import FLAGGED, never blocked (ADR-23).
"""

import argparse
import csv
import re
import sqlite3
import os
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))
from ops import money  # noqa: E402

ENTITY_CODE = "CSSB"  # all current projects belong to Smart Buildings

CANONICAL = re.compile(r"^JN-\d+$")
# Valid codes that are not JN-####. The normaliser must leave these ALONE;
# a clever normaliser corrupts them.
KNOWN_GOOD = {"JN-CommS"}
KNOWN_GOOD_PREFIX = ("P-",)
PLACEHOLDERS = {"tba", "various", "na", "n/a", "-", ""}


class ImportError_(Exception):
    pass


def cents(raw: str) -> int:
    """Delegates to ops.money -- ONE rounding function, one place (ADR-15).

    This used to parse inline and keep only the first two decimals, which
    TRUNCATED anything finer. The FY27 register contains no sub-cent values
    so nothing was lost here, but truncation drifts consistently downward
    and would have bitten silently on the office-expense grids and on
    anything arriving from Xero.
    """
    try:
        return money.parse(raw)
    except money.MoneyError as e:
        raise ImportError_(str(e))


def classify(code: str):
    """-> (class, canonical_code, legacy_code_or_None).

    A = format variant, mechanically canonicalised, original kept as an alias
    B = placeholder, needs a job number issued or a not-project-work decision
    None = already canonical, or a known-good non-JN code
    """
    raw = (code or "").strip()
    if raw in KNOWN_GOOD or raw.startswith(KNOWN_GOOD_PREFIX):
        return None, raw, None
    if CANONICAL.match(raw):
        return None, raw, None
    if raw.lower() in PLACEHOLDERS:
        return "B", raw or "(blank)", None
    # Only canonicalise what we can prove: JN, optional separator, digits.
    m = re.fullmatch(r"(?i)jn[\s\-_]*(\d+)", raw)
    if m:
        return "A", f"JN-{m.group(1)}", raw
    return "B", raw, None


def read_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ImportError_("register CSV is empty")
    required = {"Project", "Job Code", "Purchase Order",
                "Invoiced Prior", "Contract Value FY27", "Status"}
    missing = required - set(rows[0].keys())
    if missing:
        raise ImportError_(
            f"register CSV missing columns: {sorted(missing)}. "
            "Expected the renamed 'Invoiced Prior' column, not 'Invoiced FY26' -- "
            "the two are different quantities and using the wrong one understates "
            "opening balances (ADR-22)."
        )
    return [r for r in rows if (r.get("Project") or "").strip()]


def validate(rows):
    """Per-row assertion, then aggregate. Raises on any failure."""
    errors, parsed = [], []
    for i, r in enumerate(rows, start=2):  # 1 = header
        name = r["Project"].strip()
        try:
            po = cents(r["Purchase Order"])
            prior = cents(r["Invoiced Prior"])
            cv = cents(r["Contract Value FY27"])
        except ImportError_ as e:
            errors.append(f"row {i} ({name}): {e}")
            continue
        if po != prior + cv:
            errors.append(
                f"row {i} ({name}): Purchase Order {po} != Invoiced Prior {prior} "
                f"+ Contract Value FY27 {cv}  (out by {po - prior - cv})"
            )
        if prior < 0 or po < 0:
            errors.append(f"row {i} ({name}): negative money value")
        parsed.append((i, r, po, prior, cv))

    seen = defaultdict(list)
    for i, r, *_ in parsed:
        seen[r["Project"].strip()].append(i)
    for name, at in seen.items():
        if len(at) > 1:
            errors.append(f"duplicate project name {name!r} at rows {at}")

    if errors:
        raise ImportError_(
            "register failed validation; nothing written:\n  - " + "\n  - ".join(errors)
        )
    return parsed


def load(conn, parsed):
    """Writes inside the caller's open transaction. Does NOT commit -- the
    caller commits or rolls back after reading the summary, so --dry-run
    reports on exactly the state it is about to discard."""
    now = int(time.time())
    cur = conn.cursor()
    entity_id = cur.execute(
        "SELECT id FROM entity WHERE code = ?", (ENTITY_CODE,)
    ).fetchone()[0]

    types = {c.lower(): i for i, c in cur.execute(
        "SELECT id, code FROM project_type")}
    clients, issues, aliases = {}, [], []

    for row_no, r, po, prior, _cv in parsed:
        name = r["Project"].strip()
        cls, code, legacy = classify(r.get("Job Code", ""))

        client_name = (r.get("Client") or "").strip()
        client_id = None
        if client_name and client_name.lower() not in PLACEHOLDERS:
            if client_name not in clients:
                cur.execute(
                    "INSERT OR IGNORE INTO client (entity_id, name) VALUES (?, ?)",
                    (entity_id, client_name))
                clients[client_name] = cur.execute(
                    "SELECT id FROM client WHERE entity_id = ? AND name = ?",
                    (entity_id, client_name)).fetchone()[0]
            client_id = clients[client_name]

        type_code = (r.get("Type") or "").strip().lower()
        status = (r.get("Status") or "Active").strip() or "Active"

        cur.execute(
            """INSERT INTO project
               (entity_id, name, job_code, project_no, client_id, type_id, status,
                project_lead, purchase_order_cents, invoiced_prior_cents,
                needs_resolution, notes, source_row, created_ts)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (entity_id, name, code, (r.get("Project No") or "").strip() or None,
             client_id, types.get(type_code), status,
             (r.get("Project Lead") or "").strip() or None,
             po, prior, 1 if cls else 0,
             (r.get("Notes") or "").strip() or None, row_no, now))
        pid = cur.lastrowid

        if legacy:
            aliases.append((legacy, pid))
            cur.execute(
                """INSERT INTO job_code_alias (legacy_code, project_id, note, created_ts)
                   VALUES (?,?,?,?)""",
                (legacy, pid, "format variant canonicalised at import", now))
        if cls:
            issues.append((code, cls, name))
            cur.execute(
                """INSERT INTO job_code_issue (raw_code, class, project_id, created_ts)
                   VALUES (?,?,?,?)""",
                (code, cls, pid, now))

    # Shared codes: legitimate one-to-many, not a defect. Recorded as class C
    # so they surface on the worklist and get an explicit decision.
    shared = cur.execute(
        """SELECT job_code, COUNT(*) FROM project
           WHERE job_code LIKE 'JN-%' GROUP BY job_code HAVING COUNT(*) > 1"""
    ).fetchall()
    for code, _n in shared:
        for (pid,) in cur.execute(
                "SELECT id FROM project WHERE job_code = ?", (code,)).fetchall():
            cur.execute(
                """INSERT OR IGNORE INTO job_code_alias
                   (legacy_code, project_id, note, created_ts) VALUES (?,?,?,?)""",
                (code, pid, "shared customer job number across work types", now))
            cur.execute(
                """INSERT INTO job_code_issue (raw_code, class, project_id, created_ts)
                   VALUES (?,'C',?,?)""", (code, pid, now))
        cur.execute("UPDATE project SET needs_resolution = 1 WHERE job_code = ?", (code,))

    # Seed job number issuance above the highest legacy JN- code.
    high = cur.execute(
        """SELECT MAX(CAST(substr(job_code, 4) AS INTEGER)) FROM project
           WHERE job_code GLOB 'JN-[0-9]*'""").fetchone()[0] or 0
    cur.execute("UPDATE job_number_sequence SET next_value = ? WHERE id = 1", (high + 1,))

    cur.execute(
        """INSERT INTO audit_log (ts, action, target_type, target_id, detail)
           VALUES (?, 'register_import', 'project', NULL, ?)""",
        (now, f"{len(parsed)} projects; {len(issues)} flagged; job numbers from {high + 1}"))

    return issues, shared, high + 1


def summarise(conn, parsed, issues, shared, next_jn):
    cur = conn.cursor()
    po, prior, oih = cur.execute(
        """SELECT SUM(purchase_order_cents), SUM(invoiced_prior_cents),
                  SUM(purchase_order_cents - invoiced_prior_cents) FROM project"""
    ).fetchone()
    opening = cur.execute(
        "SELECT COUNT(*) FROM project WHERE invoiced_prior_cents > 0").fetchone()[0]
    d = lambda c: f"${c / 100:,.2f}"
    out = [
        "",
        f"  projects imported          {len(parsed)}",
        f"  Purchase Order             {d(po)}",
        f"  Invoiced Prior             {d(prior)}   ({opening} opening claim lines due at STP-2)",
        f"  Orders in Hand, FY27 start {d(oih)}",
        f"  next job number            JN-{next_jn}",
        "",
        f"  worklist                   {len(issues) + sum(1 for _ in shared) * 2} rows",
    ]
    by_class = defaultdict(list)
    for code, cls, name in issues:
        by_class[cls].append(f"{code}  ({name})")
    for cls in sorted(by_class):
        out.append(f"    class {cls}:")
        out += [f"      {x}" for x in sorted(by_class[cls])]
    if shared:
        out.append("    class C (shared customer job numbers, not defects):")
        out += [f"      {code} x{n}" for code, n in shared]
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True, help="Project List tab exported as CSV")
    ap.add_argument("--db", required=True, help="SQLite database, migrated to >= 001")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and report, roll back before commit")
    args = ap.parse_args(argv)

    try:
        rows = read_rows(args.csv)
        parsed = validate(rows)
    except ImportError_ as e:
        print(f"ABORT: {e}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        if conn.execute("SELECT COUNT(*) FROM project").fetchone()[0]:
            print("ABORT: project table is not empty; this importer is one-shot.",
                  file=sys.stderr)
            return 2
        issues, shared, next_jn = load(conn, parsed)
        # Summarise INSIDE the transaction, then decide its fate: a dry run
        # must report on exactly the state it is about to discard.
        print(summarise(conn, parsed, issues, shared, next_jn))
        if args.dry_run:
            conn.rollback()
            print("\n  DRY RUN -- rolled back, nothing written.\n")
        else:
            conn.commit()
            print("\n  committed.\n")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
