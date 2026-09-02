"""Import the procurement register.

    python3 tools/import_procurement.py --db /data/ops.db --csv Procurement.csv
    python3 tools/import_procurement.py ... --apply

The register is a sheet a project engineer fills in and accounts works
from. It is not a table: the first six lines carry a title block and the FX
rate, and the header sits on line seven.

WHAT IT BUILDS. A line item becomes a `procurement_line`. Where the row
names a quote reference, a `supplier_quote` is found or created and carries
the FX RATE from the top of the sheet -- the rate is agreed at quote, so
that is where it belongs even though the sheet keeps one for the lot. Where
it names a PO, a `supplier_po` is found or created for that project. Where
it names an invoice, a `supplier_invoice` is found or created for that
supplier, and lines from several orders may share it.

WHAT IT REFUSES. Supplier names in the register are working names, and only
four of thirteen match the iTrade list: `USR` is Jinan USR IOT Technology,
`Abakus` is spelled `Abukus` in the list, and `Eve` is not there at all.
Fuzzy matching would get `Colterlec` right and `USR` wrong, and a wrong
supplier on an order puts spend against a company that never sold us
anything. So unmatched names are REPORTED with their nearest candidate and
the import refuses until each is resolved -- with `--alias "USR=Jinan USR
IOT Technology Limited"`, recorded once so later imports match silently.

The five states in the register's `Delivery Remaining` column become DATES,
because payment and delivery are independent facts. A state gives the date
it implies, and the date it does not imply stays empty rather than being
invented.
"""

import argparse
import csv
import difflib
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))

from ops import money  # noqa: E402
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "ops", "migrations"))

#: Which dates a register state implies. Anything it does not imply stays
#: empty: `Delivered` says nothing about whether it has been paid.
STATE_DATES = {
    "to be ordered": (),
    "create po": (),
    "ordered": ("ordered_date",),
    "invoice received": ("ordered_date", "invoiced_date"),
    "paid - pending delivery": ("ordered_date", "invoiced_date", "paid_date"),
    "delivered": ("ordered_date", "delivered_date"),
    "complete": ("ordered_date", "invoiced_date", "delivered_date",
                 "paid_date"),
}


class ImportError_(Exception):
    pass


def read_register(path):
    """The header is not the first row: six lines of title block sit above
    it, and the FX rate is in them."""
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
        if "Project" in cells and "Supplier" in cells and "Qty Req" in cells:
            header_at = i
            break
    if header_at is None:
        raise SystemExit(f"{os.path.basename(path)}: no header row found "
                         "(expected columns Project, Supplier, Qty Req)")
    header = [c.replace("\n", " ").strip() for c in rows[header_at]]
    data = [dict(zip(header, row + [""] * (len(header) - len(row))))
            for row in rows[header_at + 1:] if any(c.strip() for c in row)]

    # The rate sits above the header, labelled `USD/AUD`: AUD per USD.
    fx_rate_bp = None
    for row in rows[:header_at]:
        for i, cell in enumerate(row):
            if cell.strip().upper() == "USD/AUD":
                for below in rows[:header_at]:
                    if i < len(below) and below[i].strip():
                        try:
                            fx_rate_bp = int(round(
                                float(below[i].strip()) * 10_000_000))
                        except ValueError:
                            continue
                        if fx_rate_bp:
                            break
    return data, fx_rate_bp


def cents(value):
    text = (value or "").strip()
    if not text:
        return None
    try:
        return money.parse(text)
    except money.MoneyError:
        return None


def resolve(db, rows, entity_id):
    """Suppliers and projects, or the reason a row cannot be placed."""
    suppliers = [s["name"] for s in db.query(
        "SELECT name FROM supplier WHERE entity_id = ?", (entity_id,))]
    unmatched, missing_projects, resolved = {}, set(), []
    for n, row in enumerate(rows, start=1):
        job = (row.get("Job No") or "").strip()
        project = db.query_one(
            "SELECT id FROM project WHERE job_code = ? AND entity_id = ?",
            (job, entity_id)) if job else None
        if project is None:
            project = db.query_one(
                """SELECT id FROM project
                   WHERE name = ? COLLATE NOCASE AND entity_id = ?""",
                ((row.get("Project") or "").strip(), entity_id))
        if project is None:
            missing_projects.add(
                f"{(row.get('Project') or '?')} ({job or 'no job code'})")
            continue
        name = (row.get("Supplier") or "").strip()
        supplier_id = db.resolve_supplier(entity_id, name) if name else None
        if name and supplier_id is None:
            near = difflib.get_close_matches(name.casefold(),
                                             [s.casefold() for s in suppliers],
                                             n=1, cutoff=0.6)
            nearest = next((s for s in suppliers
                            if near and s.casefold() == near[0]), None)
            unmatched.setdefault(name, nearest)
            continue
        resolved.append((n, row, project["id"], supplier_id))
    return resolved, unmatched, sorted(missing_projects)


def build(db, resolved, fx_rate_bp, entity_id, actor_id, apply):
    quotes, pos, invoices = {}, {}, {}
    created = {"lines": 0, "quotes": 0, "pos": 0, "invoices": 0}
    for _n, row, project_id, supplier_id in resolved:
        currency = "USD" if cents(row.get("USD")) else "AUD"
        unit = cents(row.get("USD")) if currency == "USD" else cents(row.get("Cost"))
        try:
            quantity = max(1, int(float((row.get("Qty Req") or "1").strip() or 1)))
        except ValueError:
            quantity = 1
        rate = fx_rate_bp if currency == "USD" else None
        total = Db.extend(unit or 0, quantity, rate)

        quote_ref = (row.get("Quote Ref") or "").strip()
        quote_id = None
        if quote_ref and supplier_id:
            key = (supplier_id, quote_ref)
            if key not in quotes:
                if apply:
                    quotes[key] = db.create_supplier_quote({
                        "entity_id": entity_id, "supplier_id": supplier_id,
                        "quote_ref": quote_ref, "currency": currency,
                        "fx_rate_bp": rate,
                        "email_subject": (row.get("Emai Subject") or "").strip()
                        or None,
                        "email_sent_date": (row.get("Email Request Sent") or "")
                        .strip() or None,
                    }, actor_id)["id"]
                else:
                    quotes[key] = -1
                created["quotes"] += 1
            quote_id = quotes[key]

        po_number = (row.get("PO Number") or "").strip()
        po_id = None
        if po_number and supplier_id:
            key = (project_id, supplier_id, po_number)
            if key not in pos:
                if apply:
                    pos[key] = db.create_supplier_po({
                        "entity_id": entity_id, "project_id": project_id,
                        "supplier_id": supplier_id,
                        "supplier_quote_id": quote_id if quote_id != -1 else None,
                        "po_number": po_number,
                        "po_date": (row.get("PO Date") or "").strip() or None,
                        "approved_by": (row.get("Approved By") or "").strip()
                        or None,
                    }, actor_id)["id"]
                else:
                    pos[key] = -1
                created["pos"] += 1
            po_id = pos[key]

        invoice_ref = (row.get("Invoice  Ref") or row.get("Invoice Ref") or "").strip()
        invoice_id = None
        if invoice_ref and supplier_id and invoice_ref.upper() != "A/A":
            key = (supplier_id, invoice_ref)
            if key not in invoices:
                if apply:
                    found, was_new = db.find_or_create_supplier_invoice(
                        entity_id, supplier_id, invoice_ref, actor_id,
                        due_date=(row.get("Date of Payment Due") or "").strip()
                        or None)
                    invoices[key] = found["id"]
                    if was_new:
                        created["invoices"] += 1
                else:
                    invoices[key] = -1
                    created["invoices"] += 1
            invoice_id = invoices[key]

        period_id = None
        eom = (row.get("EOM") or "").strip()
        if eom:
            period_id = db.scalar("SELECT id FROM period WHERE label = ?", (eom,))

        # A date only where the register HAS one. Dating delivery from the
        # PO date would invent a fact; the state is kept verbatim instead,
        # and stops being needed the moment someone records the real date.
        state = (row.get("Delivery Remaining") or "").strip()
        po_date = (row.get("PO Date") or "").strip() or None
        dates = {}
        if "ordered_date" in STATE_DATES.get(state.casefold(), ()) and po_date:
            dates["ordered_date"] = po_date
        # `Date of Payment Due` is a DUE date, not a paid date. Treating it
        # as payment made three `complete` rows read as `paid, pending
        # delivery` -- a real date producing a LESS advanced state than the
        # sheet reported. It belongs on the invoice, where it is a
        # commitment rather than a fact.

        if apply:
            db.create_procurement_line({
                "entity_id": entity_id, "project_id": project_id,
                "supplier_id": supplier_id,
                "supplier_po_id": po_id if po_id != -1 else None,
                "supplier_quote_id": quote_id if quote_id != -1 else None,
                "supplier_invoice_id": invoice_id if invoice_id != -1 else None,
                "period_id": period_id,
                "item": (row.get("Item") or "").strip() or None,
                "description": (row.get("Description") or "").strip() or None,
                "quantity": quantity, "currency": currency,
                "unit_cost_cents": unit or 0, "total_cents": total,
                "note": (row.get("Other Info") or "").strip() or None,
                "stated_state": state.casefold() or None,
                **dates,
            }, actor_id)
        created["lines"] += 1
    return created


#: What the register owns, and may therefore change on a row that already
#: exists. Deliberately short: a DATE somebody recorded in the platform is
#: a fact the sheet does not have, so `delivered_date` and `paid_date` are
#: not here. Syncing them back from a sheet that never held them would
#: erase the thing the platform was built to capture.
SYNCED_FIELDS = ("quantity", "unit_cost_cents", "total_cents", "currency",
                 "period_id", "item", "description", "note", "stated_state")


def natural_key(project_id, supplier_id, item):
    """Project, supplier and item.

    The RESOLVED supplier, not the register's word for it: the sheet says
    `Eve` where the platform holds `EVE Security Services Pty Ltd`, and
    keying on the raw name made every aliased row look new -- twenty-nine
    of them, against six that actually were.

    Not quantity or cost either: both legitimately change on a row that is
    still the same row. Twelve costs moved in the September export and none
    of them was a new purchase.
    """
    return (project_id, supplier_id, (item or "").strip().casefold())


def plan_sync(db, resolved, fx_rate_bp, entity_id):
    """What the register has that the platform does not, and what has
    changed on the rows they share."""
    existing = {}
    for row in db.query(
            """SELECT l.id, l.project_id, l.supplier_id, l.item, l.quantity,
                      l.unit_cost_cents, l.total_cents, l.currency,
                      l.period_id, l.description, l.note, l.stated_state,
                      l.is_estimate, l.delivered_date, l.paid_date,
                      q.fx_rate_bp AS fx_rate_bp
               FROM procurement_line l
               LEFT JOIN supplier_quote q ON q.id = l.supplier_quote_id
               WHERE l.entity_id = ? AND l.is_estimate = 0""",
            (entity_id,)):
        existing[natural_key(row["project_id"], row["supplier_id"],
                             row["item"])] = row

    added, changed, unchanged, held = [], [], 0, []
    for n, row, project_id, supplier_id in resolved:
        found = existing.get(natural_key(project_id, supplier_id,
                                         row.get("Item")))
        if found is None:
            added.append((n, row, project_id, supplier_id))
            continue
        # A USD line is costed at the rate of ITS OWN QUOTE, not at
        # whatever the sheet says today. The sheet's rate cell is live: it
        # re-floats every foreign line the moment somebody opens the file,
        # which moved twelve costs in the September export without a single
        # price changing. The rate is agreed with the supplier and fixed at
        # quote (ADR-40), so the line's own rate is the one that governs.
        rate = None
        if found["currency"] != "AUD":
            rate = found["fx_rate_bp"]
            if not rate and found["unit_cost_cents"] and found["quantity"]:
                # A USD line imported WITHOUT a quote reference has no rate
                # recorded anywhere, so it is recovered from what it was
                # costed at. Backing it out is exact: the total was computed
                # from these two numbers and a rate, once.
                gross = found["unit_cost_cents"] * found["quantity"]
                rate = round(found["total_cents"] * 10_000_000 / gross)
        wanted = line_fields(db, row, rate if rate else fx_rate_bp)
        diffs = {k: (found[k], v) for k, v in wanted.items()
                 if k in SYNCED_FIELDS and found[k] != v}
        # A STATE somebody set in the platform is not the sheet's to undo.
        # Twenty lines were marked `complete` here while the register still
        # said `delivered` -- unchanged since the last export -- and syncing
        # would have walked every one of them backwards. The sheet may tell
        # the platform something it has never been told; it may not
        # overwrite something recorded here. Same rule as the dates, and
        # the same rule as a typed expense figure beating a calculated one.
        if "stated_state" in diffs and db.scalar(
                """SELECT COUNT(*) FROM procurement_line_revision
                   WHERE line_id = ? AND field = 'stated_state'""",
                (found["id"],)):
            held.append((found, row, diffs.pop("stated_state")))
        if diffs:
            changed.append((found, row, diffs))
        else:
            unchanged += 1
    return added, changed, unchanged, held


def line_fields(db, row, fx_rate_bp):
    """The register's own view of a line, in the platform's terms."""
    currency = "USD" if cents(row.get("USD")) else "AUD"
    unit = cents(row.get("USD")) if currency == "USD" else cents(row.get("Cost"))
    try:
        quantity = max(1, int(float((row.get("Qty Req") or "1").strip() or 1)))
    except ValueError:
        quantity = 1
    rate = fx_rate_bp if currency == "USD" else None
    eom = (row.get("EOM") or "").strip()
    return {
        "quantity": quantity,
        "currency": currency,
        "unit_cost_cents": unit or 0,
        "total_cents": Db.extend(unit or 0, quantity, rate),
        "period_id": db.scalar("SELECT id FROM period WHERE label = ?", (eom,))
                     if eom else None,
        "item": (row.get("Item") or "").strip() or None,
        "description": (row.get("Description") or "").strip() or None,
        "note": (row.get("Other Info") or "").strip() or None,
        "stated_state": (row.get("Delivery Remaining") or "").strip().casefold()
                        or None,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--entity", type=int, default=1)
    ap.add_argument("--alias", action="append", default=[],
                    metavar="NAME=SUPPLIER",
                    help="resolve a register name to a supplier, once")
    ap.add_argument("--fx-rate", type=float, metavar="AUD_PER_USD",
                    help="AUD per USD, e.g. 1.388561. Needed when the sheet "
                         "does not carry it above the header.")
    ap.add_argument("--reset", action="store_true",
                    help="clear a partial import and start again")
    ap.add_argument("--sync", action="store_true",
                    help="add what is new and update what changed, instead "
                         "of importing from empty")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    rows, fx_rate_bp = read_register(args.csv)
    db = Db(args.db, MIGRATIONS)
    try:
        actor = db.query_one("SELECT id FROM users ORDER BY id LIMIT 1")
        actor_id = actor["id"] if actor else None

        for pair in args.alias:
            if "=" not in pair:
                raise SystemExit(f"--alias needs NAME=SUPPLIER, got {pair!r}")
            alias, target = (part.strip() for part in pair.split("=", 1))
            supplier = db.query_one(
                """SELECT id, name FROM supplier
                   WHERE entity_id = ? AND name = ? COLLATE NOCASE""",
                (args.entity, target))
            if supplier is None:
                raise SystemExit(f"no supplier named {target!r}")
            db.add_supplier_alias(args.entity, alias, supplier["id"], actor_id,
                                  "procurement register")
            print(f"  {alias!r} now resolves to {supplier['name']}")

        # Every table this writes, not just the lines: an import that
        # failed part way through leaves quotes and orders behind, and a
        # guard that only counts lines would let the next run duplicate
        # them.
        counts_now = {t: db.scalar(f"SELECT COUNT(*) FROM {t}") for t in
                      ("procurement_line", "supplier_quote", "supplier_po",
                       "supplier_invoice")}
        if args.reset and args.apply:
            db.clear_procurement(actor_id)
            print("  cleared "
                  + ", ".join(f"{v} {k}" for k, v in counts_now.items() if v))
            counts_now = {t: 0 for t in counts_now}
        if any(counts_now.values()) and not args.sync:
            print("ABORT: procurement is already imported ("
                  + ", ".join(f"{v} {k}" for k, v in counts_now.items() if v)
                  + "). This importer is one-shot; use --reset --apply to "
                    "clear and start again.", file=sys.stderr)
            return 2

        if args.fx_rate:
            fx_rate_bp = int(round(args.fx_rate * 10_000_000))

        resolved, unmatched, missing = resolve(db, rows, args.entity)
        print()
        print(f"  {len(rows)} register row(s)")
        if fx_rate_bp:
            print(f"  FX rate from the sheet: "
                  f"{fx_rate_bp / 10_000_000:.6f} AUD per USD")
        else:
            print("  no FX rate found above the header; USD rows cannot be "
                  "costed")
        print(f"  {len(resolved)} row(s) can be placed")

        if unmatched:
            print(f"\n  {len(unmatched)} supplier name(s) are not in the "
                  "supplier list. Nothing is imported until each is "
                  "resolved:")
            for name, nearest in sorted(unmatched.items()):
                if nearest:
                    print(f'    --alias "{name}={nearest}"')
                else:
                    print(f"    {name!r} — no near match; add the supplier "
                          "first, or alias it to an existing one")
        if missing:
            print(f"\n  {len(missing)} project(s) in the register are not in "
                  "the platform:")
            for text in missing:
                print(f"    {text}")

        # A USD row without a rate cannot be costed in AUD, and the schema
        # refuses to store it. Caught HERE rather than at the database: the
        # first version validated on write, so it created twenty-two quotes
        # and then failed, leaving a half-import behind.
        usd_rows = [row for _n, row, _p, _s in resolved if cents(row.get("USD"))]
        if usd_rows and not fx_rate_bp:
            print(f"\n  {len(usd_rows)} row(s) are priced in USD and no rate "
                  "was found above the header.")
            print("  ABORT: pass --fx-rate, e.g. --fx-rate 1.388561 "
                  "(AUD per USD).\n")
            return 1

        if unmatched:
            # An alias is a ten-second fix, and a WRONG supplier is worse
            # than a missing row: it puts spend against a company that
            # never sold us anything.
            print("\n  ABORT: resolve the supplier names above and run "
                  "again.\n")
            return 1
        if missing:
            # Not fatal. Creating a project needs a job-code decision
            # (ADR-28), which is not something an importer should make --
            # so those rows are listed and left, and the rest go in.
            print("  Those rows are SKIPPED. Create the project and run "
                  "again for them.")

        if args.sync:
            added, changed, unchanged, held = plan_sync(
                db, resolved, fx_rate_bp, args.entity)
            print(f"\n  {len(added)} new, {len(changed)} changed, "
                  f"{unchanged} unchanged"
                  + (f", {len(held)} state(s) held" if held else ""))
            for _n, row, _p, _s in added:
                print(f"    + {row['Project'][:26]:26s} "
                      f"{(row.get('Supplier') or '')[:20]:20s} "
                      f"{(row.get('Item') or '')[:26]:26s} {row.get('Cost','')}")
            for found, row, diffs in changed:
                print(f"    ~ {row['Project'][:26]:26s} "
                      f"{(row.get('Item') or '')[:26]:26s}")
                for field, (was, now) in sorted(diffs.items()):
                    shown = ((money.format(was), money.format(now))
                             if field.endswith("_cents") else (was, now))
                    print(f"        {field:18s} {shown[0]!r} -> {shown[1]!r}")
            if held:
                print(f"\n  {len(held)} line(s) whose state was set HERE and "
                      "is left alone:")
                for found, row, (was, now) in held:
                    print(f"    {row['Project'][:26]:26s} "
                          f"{(row.get('Item') or '')[:24]:24s} "
                          f"platform {was!r}, sheet {now!r}")
            # A line the platform has and the register no longer does is
            # left ALONE. The sheet is where lines are added, not the only
            # place they may exist, and deleting spend because a row moved
            # is not a trade worth making.
            if not args.apply:
                print("\n  DRY RUN — nothing written. Re-run with --apply.\n")
                return 0
            made = build(db, added, fx_rate_bp, args.entity, actor_id, True)
            for found, _row, diffs in changed:
                db.update_procurement_line(
                    found["id"], {k: v for k, (_w, v) in diffs.items()},
                    actor_id, "register sync")
            print(f"\n  added {made['lines']} line(s), "
                  f"updated {len(changed)}.\n")
            return 0

        counts = build(db, resolved, fx_rate_bp, args.entity, actor_id, False)
        print(f"\n  would create {counts['lines']} line(s), "
              f"{counts['quotes']} quote(s), {counts['pos']} order(s), "
              f"{counts['invoices']} invoice(s)")
        if not args.apply:
            print("\n  DRY RUN — nothing written. Re-run with --apply.\n")
            return 0
        counts = build(db, resolved, fx_rate_bp, args.entity, actor_id, True)
        print(f"\n  imported {counts['lines']} line(s), "
              f"{counts['quotes']} quote(s), {counts['pos']} order(s), "
              f"{counts['invoices']} invoice(s).\n")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
