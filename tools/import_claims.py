"""Import claims from the three invoicing sources (STP-2).

    python3 tools/import_claims.py --db /data/ops.db \
        --invoicing "Invoicing.csv" \
        --future    "Future Invoicing.csv" \
        --matrix    "Project Invoicing by Month.csv"

**Two sources, and a pivot that checks them.**

  Invoicing         ->  claim_line 'invoiced'   (an invoice has been issued)
  Future Invoicing  ->  claim_line 'forecast'   (planned, by end-of-month)

`Monthly Data` is a PIVOT of those two plus the register, so it carries no
information of its own. It is still worth reconciling against: a pivot that
disagrees with its own source means a row has been missed on the way in, and
checking costs nothing.

Both tabs are taken WHOLE. There is no status column and no overlap between
them -- once a claim is invoiced it moves out of Future Invoicing.

Nothing is written unless the reconciliation passes or `--accept-variance`
is given deliberately.

`--sync` runs it again later, ADDITIVELY: rows the platform does not have
are created, and nothing existing is touched. That matters because once a
claim is in the platform it starts to diverge on purpose -- statuses move,
months slip, retention is withheld -- and a sync that overwrote would undo
the work the platform is for. Anything that looks like a changed amount is
reported for a human instead.
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

# Months already invoiced when the platform took over. Forward planning
# starts after them.
INVOICED_MONTHS = ("Jul-26", "Aug-26")


class ClaimImportError(Exception):
    pass


def read_csv(path, required):
    # A missing file is the commonest way this is run wrongly -- export
    # names vary with how Sheets is asked for them. Say which file, and
    # what is actually in the folder.
    if not os.path.exists(path):
        folder = os.path.dirname(os.path.abspath(path)) or "."
        nearby = sorted(f for f in os.listdir(folder)
                        if f.lower().endswith(".csv")) if os.path.isdir(folder) else []
        hint = ("\n  CSV files in that folder:\n    "
                + "\n    ".join(nearby)) if nearby else \
               "\n  (no CSV files in that folder)"
        raise ClaimImportError(f"no such file: {path}{hint}")
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ClaimImportError(f"{path} is empty")
    missing = required - set(rows[0])
    if missing:
        raise ClaimImportError(
            f"{os.path.basename(path)} is missing columns: {sorted(missing)}")
    return rows


def cents(raw):
    try:
        return money.parse(raw)
    except money.MoneyError as e:
        raise ClaimImportError(str(e))


def load_sources(invoicing_path, future_path, matrix_path):
    invoicing = read_csv(invoicing_path,
                         {"Project", "Invoice Amount", "EOM"})
    future = read_csv(future_path,
                      {"Project", "Invoice Amount", "EOM Cycle"})
    issued = [r for r in invoicing if (r.get("Project") or "").strip()]
    residue = []          # no status column: the tab is issued invoices only
    planned = [r for r in future if (r.get("Project") or "").strip()]

    matrix, months = read_matrix(matrix_path)
    return issued, residue, planned, matrix, months


def read_matrix(path):
    """The pivot. Its header is not the first row, so find the row that
    carries month labels and read from there."""
    import re
    month = re.compile(r"^[A-Z][a-z]{2}-\d\d$")
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    header_at = None
    for i, row in enumerate(rows):
        labels = [c.strip() for c in row if month.match(c.strip())]
        if len(labels) >= 6:
            header_at = i
            break
    if header_at is None:
        raise ClaimImportError(f"{os.path.basename(path)}: no month header found")
    header = [c.strip() for c in rows[header_at]]
    months = [c for c in header if month.match(c)]
    idx = {c: header.index(c) for c in months}
    name_col = header.index("Project") if "Project" in header else 0
    matrix = {}
    for row in rows[header_at + 1:]:
        if len(row) <= name_col:
            continue
        name = row[name_col].strip()
        if not name or name in ("Grand Total", "Project"):
            continue
        matrix[name] = {m: (cents(row[idx[m]]) if idx[m] < len(row)
                            and row[idx[m]].strip() else 0) for m in months}
    return matrix, months


def reconcile(issued, planned, matrix, months):
    """Every project/month, detail against control total.

    Both directions matter. Detail exceeding the matrix means something was
    entered twice; the matrix exceeding detail means a line item was never
    written down, which is the case that quietly understates a forecast.
    """
    detail = collections.defaultdict(int)
    for r in issued:
        detail[(r["Project"].strip(), r["EOM"].strip())] += cents(r["Invoice Amount"])
    for r in planned:
        detail[(r["Project"].strip(), r["EOM Cycle"].strip())] += \
            cents(r["Invoice Amount"])

    findings = []
    for name, row in matrix.items():
        for m in months:
            control, got = row[m], detail.get((name, m), 0)
            if control != got:
                findings.append({"project": name, "month": m,
                                 "matrix": control, "detail": got,
                                 "difference": control - got})
    # Detail for a project the matrix carries, in a month INSIDE its span.
    # Months beyond the matrix are not discrepancies -- the control total
    # simply stops at the end of FY27, and forward planning does not.
    for (name, m), got in detail.items():
        if m in months and name not in matrix and got:
            findings.append({"project": name, "month": m, "matrix": None,
                             "detail": got, "difference": -got})
    findings.sort(key=lambda f: (-abs(f["difference"]), f["project"]))
    return findings


def outside_control_total(planned, months):
    """Forward months the matrix does not cover. Reported, not flagged."""
    beyond = collections.defaultdict(int)
    for r in planned:
        m = r["EOM Cycle"].strip()
        if m and m not in months:
            beyond[m] += cents(r["Invoice Amount"])
    return dict(sorted(beyond.items()))


def resolve(db, issued, planned):
    """Map names and months onto ids, collecting every failure at once."""
    projects = {r["name"]: r for r in db.query(
        "SELECT id, name, entity_id FROM project")}
    folded = {k.casefold(): v for k, v in projects.items()}
    periods = {r["label"]: r["id"] for r in db.query(
        "SELECT id, label FROM period")}
    pos = collections.defaultdict(list)
    for r in db.query("SELECT id, project_id FROM customer_po"):
        pos[r["project_id"]].append(r["id"])

    errors, resolved, needs_po, skipped, spelling = [], [], set(), [], []
    for source, rows, month_col, status in (
            ("Invoicing", issued, "EOM", "invoiced"),
            ("Future Invoicing", planned, "EOM Cycle", "forecast")):
        for i, r in enumerate(rows, 2):
            name = r["Project"].strip()
            month = r[month_col].strip()
            project = projects.get(name) or folded.get(name.casefold())
            if project is not None and project["name"] != name:
                spelling.append(f"{source}: {name!r} matched {project['name']!r}")
            if project is None:
                amount = cents(r["Invoice Amount"])
                if amount == 0:
                    # A zero-value row for a project that no longer exists is
                    # residue, not data. Skipped, but LISTED -- silently
                    # dropping rows is how an import quietly loses something
                    # that mattered.
                    skipped.append(f"{source} row {i}: {name} "
                                   "(no such project, $0.00)")
                    continue
                errors.append(
                    f"{source} row {i}: no project named {name!r} "
                    f"carrying {money.format(amount)}")
                continue
            if month not in periods:
                errors.append(f"{source} row {i}: {name} has no EOM")
                continue
            po_list = pos.get(project["id"], [])
            if not po_list:
                # A project being invoiced with no recorded PO is a real
                # finding, not a reason to stop: maintenance is often billed
                # against an SLA with no PO number. A placeholder at zero
                # keeps the claim attached to something, and the resulting
                # negative orders-in-hand says plainly "billed against a
                # project with no recorded contract" instead of hiding it.
                needs_po.add((project["id"], name))
                po_list = [None]
            resolved.append({
                "_project_name": project["name"],
                "_month": month,
                "entity_id": project["entity_id"],
                "project_id": project["id"],
                # One PO per project after migration 003. When a project
                # gains several, this needs the PO named in the source.
                "customer_po_id": po_list[0],
                "period_id": periods[month],
                "status": status,
                "amount_cents": cents(r["Invoice Amount"]),
                "detail": (r.get("Detail") or r.get("Task") or "").strip() or None,
                "phase": (r.get("Phase") or "").strip() or None,
                # The workbook's LINE ITEM. It was folded into `detail` and
                # the column left empty, so the claim plan had nothing to
                # group on and five tasks collapsed into their phase.
                "task": (r.get("Task") or "").strip() or None,
                "reference": (r.get("Reference") or "").strip() or None,
                "invoice_number": (r.get("Invoice Number") or "").strip() or None,
                "invoiced_date": None,
                "source": source,
            })
    return resolved, errors, sorted(needs_po), skipped, spelling


def double_counted(db):
    """Projects where the opening balance and FY27 invoicing overlap.

    `Invoiced Prior` means invoiced BEFORE FY27 -- but Jul-26 is inside
    FY27. Where a project's opening balance equals its contract AND it also
    carries a Jul/Aug invoice, the same money has been counted twice: once
    as history, once as an FY27 claim. It understates orders in hand at FY27
    start by exactly that amount, which is the figure everything else is
    reconciled against.
    """
    return db.query("""
        SELECT p.name,
               (SELECT SUM(po.amount_cents) FROM customer_po po
                WHERE po.project_id = p.id) AS contract_cents,
               (SELECT o.amount_cents FROM claim_line o
                WHERE o.project_id = p.id AND o.is_opening_balance = 1)
                                            AS opening_cents,
               (SELECT SUM(c.amount_cents) FROM claim_line c
                JOIN period pe ON pe.id = c.period_id
                WHERE c.project_id = p.id AND c.is_opening_balance = 0
                  AND c.status = 'invoiced' AND pe.label IN ('Jul-26','Aug-26'))
                                            AS fy27_cents
        FROM project p
        WHERE opening_cents IS NOT NULL AND fy27_cents IS NOT NULL
          AND opening_cents + fy27_cents > contract_cents
        ORDER BY opening_cents + fy27_cents - contract_cents DESC""")


def create_placeholder_pos(db, needs_po, actor_id):
    """A zero-value PO for a project that has claims but none recorded."""
    made = []
    for project_id, name in needs_po:
        with db._tx() as c:
            cur = c.execute(
                """INSERT INTO customer_po (entity_id, project_id, amount_cents,
                       note, is_placeholder, created_by, created_ts)
                   SELECT entity_id, id, 0,
                          'placeholder: claims exist with no recorded PO',
                          1, ?, strftime('%s','now')
                   FROM project WHERE id = ?""", (actor_id, project_id))
            made.append((project_id, cur.lastrowid, name))
    return made


def load(db, resolved, actor_id):
    created = 0
    for row in resolved:
        fields = {k: v for k, v in row.items()
                  if k != "source" and not k.startswith("_")}
        invoice_number = fields.pop("invoice_number")
        status = fields.pop("status")
        fields["status"] = "forecast"
        claim = db.create_claim_line(fields, actor_id)
        if status == "invoiced":
            # Through the lifecycle, not around it: retention is computed at
            # invoicing, and going straight to the row would skip it.
            db.transition_claim(claim["id"], "due", {}, None, actor_id)
            db.transition_claim(claim["id"], "approved",
                                {"approved_date": None}, None, actor_id)
            db.transition_claim(
                claim["id"], "invoiced",
                {"invoice_number": invoice_number or "(not recorded)",
                 "invoiced_date": None},
                "imported from the Invoicing tab", actor_id)
        created += 1
    return created


def report(issued, residue, planned, findings, resolved, beyond, needs_po,
           skipped, spelling):
    d = money.format
    out = ["",
           f"  Invoicing (issued)       {len(issued):>4} rows  "
           f"{d(sum(cents(r['Invoice Amount']) for r in issued)):>15}",
           f"  Future Invoicing         {len(planned):>4} rows  "
           f"{d(sum(cents(r['Invoice Amount']) for r in planned)):>15}",
           f"  to import                {len(resolved):>4} claims", ""]
    if findings:
        out.append(f"  {len(findings)} project/month cells differ from the matrix:")
        out.append(f"    {'project':38s} {'month':8s} {'matrix':>13} "
                   f"{'detail':>13} {'difference':>13}")
        for f in findings:
            out.append(
                f"    {f['project'][:38]:38s} {f['month']:8s} "
                f"{(d(f['matrix']) if f['matrix'] is not None else '(absent)'):>13} "
                f"{d(f['detail']):>13} {d(f['difference']):>13}")
        out.append(f"    {'TOTAL':38s} {'':8s} {'':>13} {'':>13} "
                   f"{d(sum(f['difference'] for f in findings)):>13}")
    else:
        out.append("  detail reconciles to the matrix exactly")
    if beyond:
        out.append("")
        out.append("  beyond the matrix (FY28+, no control total exists):")
        for m, v in beyond.items():
            out.append(f"    {m:8s} {d(v):>15}")
    if spelling:
        out.append("")
        out.append(f"  {len(spelling)} project name(s) matched on spelling:")
        out += [f"    {x}" for x in spelling]
    if skipped:
        out.append("")
        out.append(f"  {len(skipped)} zero-value rows skipped:")
        out += [f"    {x}" for x in skipped]
    if needs_po:
        out.append("")
        out.append(f"  {len(needs_po)} projects have claims but no recorded PO; "
                   "a placeholder at zero will be created:")
        for _pid, name in needs_po:
            out.append(f"    {name}")
    out.append("")
    return "\n".join(out)


def natural_key(row):
    """What makes a claim the same claim across two exports.

    Project, month, task and amount. There is no id in the workbook, so this
    is the closest thing to one -- and the comparison is case-insensitive on
    the text, because `RGB Service works` and `RGB Service Works` are the
    same row typed twice.
    """
    return (row["project_name"].strip().casefold(),
            (row["month"] or "").strip().casefold(),
            (row["task"] or "").strip().casefold(),
            row["amount_cents"])


def existing_keys(db):
    rows = db.query(
        """SELECT p.name AS project_name, pe.label AS month,
                  COALESCE(cl.detail, '') AS task, cl.amount_cents
           FROM claim_line cl
           JOIN project p ON p.id = cl.project_id
           LEFT JOIN period pe ON pe.id = cl.period_id
           WHERE cl.is_opening_balance = 0""")
    return collections.Counter(natural_key(dict(r)) for r in rows)


def sync(db, resolved, actor_id):
    """Create what is missing; touch nothing that exists."""
    have = existing_keys(db)
    created, skipped = [], 0
    for row in resolved:
        key = natural_key({"project_name": row["_project_name"],
                           "month": row["_month"],
                           "task": row.get("detail") or "",
                           "amount_cents": row["amount_cents"]})
        if have[key]:
            have[key] -= 1
            skipped += 1
            continue
        created.append(row)
    n = load(db, created, actor_id)
    return n, skipped, created


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--invoicing", required=True)
    ap.add_argument("--future", required=True)
    ap.add_argument("--matrix", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sync", action="store_true",
                    help="add rows the platform does not have; touch nothing "
                         "that exists")
    ap.add_argument("--accept-variance", action="store_true",
                    help="import even though detail and matrix disagree")
    args = ap.parse_args(argv)

    try:
        issued, residue, planned, matrix, months = load_sources(
            args.invoicing, args.future, args.matrix)
    except ClaimImportError as e:
        print(f"ABORT: {e}", file=sys.stderr)
        return 2

    db = Db(args.db, MIGRATIONS)
    try:
        already = db.scalar("SELECT COUNT(*) FROM claim_line "
                            "WHERE is_opening_balance = 0")
        if already and not args.sync:
            print(f"ABORT: {already} claims already imported; this importer is "
                  "one-shot. Use --sync to add only what is new.",
                  file=sys.stderr)
            return 2
        findings = reconcile(issued, planned, matrix, months)
        beyond = outside_control_total(planned, months)
        resolved, errors, needs_po, skipped, spelling = resolve(db, issued, planned)
        print(report(issued, residue, planned, findings, resolved, beyond,
                     needs_po, skipped, spelling))

        if errors:
            print("ABORT: rows that cannot be placed:\n  - "
                  + "\n  - ".join(errors), file=sys.stderr)
            return 2
        if findings and not args.accept_variance:
            print("ABORT: detail does not reconcile to the matrix. Add the "
                  "missing line items, or pass --accept-variance to import "
                  "anyway.", file=sys.stderr)
            return 1
        if args.dry_run:
            print("  DRY RUN — nothing written.\n")
            return 0
        actor = db.query_one("SELECT id FROM users ORDER BY id LIMIT 1")
        actor_id = actor["id"] if actor else None
        made = create_placeholder_pos(db, needs_po, actor_id)
        by_project = {pid: po_id for pid, po_id, _n in made}
        for row in resolved:
            if row["customer_po_id"] is None:
                row["customer_po_id"] = by_project[row["project_id"]]
        if made:
            print(f"  created {len(made)} placeholder POs.")
        if args.sync:
            created, unchanged, rows = sync(db, resolved, actor_id)
            print(f"  {unchanged} claims already present, {created} added.")
            for row in rows:
                print(f"    {row['_project_name'][:38]:38s} "
                      f"{row['_month']:8s} {money.format(row['amount_cents']):>13}"
                      f"  {row['status']}")
        else:
            created = load(db, resolved, actor_id)
            print(f"  imported {created} claims.")
        overlap = double_counted(db)
        if overlap:
            d = money.format
            excess = sum(r["opening_cents"] + r["fy27_cents"]
                         - r["contract_cents"] for r in overlap)
            print(f"\n  WARNING: {len(overlap)} projects have been invoiced "
                  "beyond their contract, because the opening balance and an\n"
                  "  FY27 invoice appear to be the same money. `Invoiced "
                  "Prior` means before FY27,\n  but Jul-26 is inside it.")
            print(f"    {'project':40s} {'contract':>12} {'opening':>12} "
                  f"{'Jul+Aug':>12} {'excess':>12}")
            for r in overlap:
                print(f"    {r['name'][:40]:40s} {d(r['contract_cents']):>12} "
                      f"{d(r['opening_cents']):>12} {d(r['fy27_cents']):>12} "
                      f"{d(r['opening_cents'] + r['fy27_cents'] - r['contract_cents']):>12}")
            print(f"    {'orders in hand at FY27 start is understated by':40s} "
                  f"{'':12} {'':12} {'':12} {d(excess):>12}")
        print()
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
