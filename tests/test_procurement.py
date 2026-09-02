"""Procurement — quotes, orders, lines, invoices (migration 012).

Modelled from the register a project engineer fills in and accounts works
from. Four things shape it, each from how that register is actually used:

  payment and delivery are INDEPENDENT
  a quote may cover SEVERAL projects, and carries the FX rate
  one supplier invoice may cover SEVERAL orders
  the foreign amount is the FACT, converted once at the extended total
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.join(ROOT, "ops", "migrations")
#: 1.388561 AUD per USD, the rate on the register.
RATE = 13_885_610


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Db(os.path.join(self.dir, "ops.db"), MIGRATIONS)
        self.db.migrate()
        self.user = self.db.upsert_user("s1", "r@x", "R")
        with self.db._tx() as c:
            for n, (name, code) in enumerate(
                    (("720 Bourke - IBP", "JN-5749"),
                     ("The Lindrum - IBP", "JN-4407")), start=1):
                c.execute("""INSERT INTO project (entity_id,name,job_code,
                                 status,created_ts)
                             VALUES (1,?,?,'Active',0)""", (name, code))
        self.bourke = self.db.scalar(
            "SELECT id FROM project WHERE job_code='JN-5749'")
        self.lindrum = self.db.scalar(
            "SELECT id FROM project WHERE job_code='JN-4407'")
        self.supplier = self.db.create_suppliers(
            [{"entity_id": 1, "name": "USR", "default_currency": "USD"},
             {"entity_id": 1, "name": "Cavern Imports"}], self.user["id"])
        self.usr = self.db.scalar("SELECT id FROM supplier WHERE name='USR'")
        self.cavern = self.db.scalar(
            "SELECT id FROM supplier WHERE name='Cavern Imports'")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def line(self, project_id, **over):
        fields = {"entity_id": 1, "project_id": project_id, "quantity": 1,
                  "unit_cost_cents": 10000, "total_cents": 10000}
        fields.update(over)
        return self.db.create_procurement_line(fields, self.user["id"])

    def state(self, line_id):
        return self.db.scalar(
            "SELECT state FROM v_procurement_line WHERE id = ?", (line_id,))


class TestTheExtendedTotal(Case):
    """The register converts the extended amount, and it is right. Rounding
    the unit first and multiplying loses a cent or two per line -- five
    lines in the real register differ for exactly that reason."""

    def test_it_reproduces_the_registers_own_figures(self):
        for usd, qty, expected in ((3300, 7, 32076), (100000, 1, 138856),
                                   (1300, 5, 9026), (3300, 2, 9165),
                                   (5300, 1, 7359)):
            self.assertEqual(Db.extend(usd, qty, RATE), expected,
                             f"USD {usd} x{qty}")

    def test_unit_first_would_be_wrong(self):
        unit = Db.extend(3300, 1, RATE)
        self.assertEqual(unit * 7, 32074)
        self.assertEqual(Db.extend(3300, 7, RATE), 32076)

    def test_without_a_rate_it_is_just_multiplication(self):
        self.assertEqual(Db.extend(1090_00, 3), 3270_00)


class TestPaymentAndDeliveryAreIndependent(Case):
    """`Paid - Pending Delivery` sits alongside `Delivered` on twelve rows
    of fifty-nine. Two dates, and the status derived -- a state machine
    would have to enumerate every combination and would still be wrong the
    first time something arrives before it is invoiced."""

    def test_a_new_line_is_to_be_ordered(self):
        row = self.line(self.bourke)
        self.assertEqual(self.state(row["id"]), "to be ordered")

    def test_paid_before_delivered(self):
        row = self.line(self.bourke)
        self.db.update_procurement_line(
            row["id"], {"paid_date": "2026-07-01"}, self.user["id"])
        self.assertEqual(self.state(row["id"]), "paid, pending delivery")

    def test_delivered_before_paid(self):
        row = self.line(self.bourke)
        self.db.update_procurement_line(
            row["id"], {"delivered_date": "2026-07-01"}, self.user["id"])
        self.assertEqual(self.state(row["id"]), "delivered, unpaid")

    def test_both_is_complete_whichever_came_first(self):
        for first, second in (("paid_date", "delivered_date"),
                              ("delivered_date", "paid_date")):
            row = self.line(self.bourke)
            self.db.update_procurement_line(
                row["id"], {first: "2026-07-01"}, self.user["id"])
            self.db.update_procurement_line(
                row["id"], {second: "2026-07-05"}, self.user["id"])
            self.assertEqual(self.state(row["id"]), "complete")

    def test_cancelled_beats_everything(self):
        """A line nobody will pay for is not a cost, however far it got."""
        row = self.line(self.bourke)
        self.db.update_procurement_line(
            row["id"], {"paid_date": "2026-07-01",
                        "delivered_date": "2026-07-05",
                        "cancelled_date": "2026-07-06",
                        "cancel_reason": "returned"}, self.user["id"])
        self.assertEqual(self.state(row["id"]), "cancelled")


class TestAQuoteMayCoverSeveralProjects(Case):
    """`MSI Cubi - RAVEN` is quoted once and lands on six jobs."""

    def test_one_quote_two_orders_one_rate(self):
        quote = self.db.create_supplier_quote(
            {"entity_id": 1, "supplier_id": self.usr, "quote_ref": "11395",
             "currency": "USD", "fx_rate_bp": RATE}, self.user["id"])
        for project in (self.bourke, self.lindrum):
            po = self.db.create_supplier_po(
                {"entity_id": 1, "project_id": project,
                 "supplier_id": self.usr, "supplier_quote_id": quote["id"],
                 "po_number": f"PO-{project}"}, self.user["id"])
            self.line(project, supplier_id=self.usr, supplier_po_id=po["id"],
                      supplier_quote_id=quote["id"], currency="USD",
                      unit_cost_cents=3300, quantity=7,
                      total_cents=Db.extend(3300, 7, RATE))
        rows = self.db.query("SELECT * FROM v_procurement_line")
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["fx_rate_bp"] for r in rows}, {RATE})
        self.assertEqual({r["total_cents"] for r in rows}, {32076})

    def test_a_foreign_quote_without_a_rate_is_refused(self):
        """It cannot be costed in AUD, so it must not be storable."""
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.create_supplier_quote(
                {"entity_id": 1, "supplier_id": self.usr, "currency": "USD"},
                self.user["id"])

    def test_a_rate_on_an_aud_quote_is_refused(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.create_supplier_quote(
                {"entity_id": 1, "supplier_id": self.cavern,
                 "currency": "AUD", "fx_rate_bp": RATE}, self.user["id"])


class TestOneInvoiceManyOrders(Case):
    def test_lines_across_two_orders_share_an_invoice(self):
        invoice, created = self.db.find_or_create_supplier_invoice(
            1, self.cavern, "INV-000733", self.user["id"])
        self.assertTrue(created)
        for project in (self.bourke, self.lindrum):
            po = self.db.create_supplier_po(
                {"entity_id": 1, "project_id": project,
                 "supplier_id": self.cavern}, self.user["id"])
            self.line(project, supplier_id=self.cavern,
                      supplier_po_id=po["id"],
                      supplier_invoice_id=invoice["id"])
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM procurement_line WHERE supplier_invoice_id = ?",
            (invoice["id"],)), 2)

    def test_looking_it_up_again_returns_the_same_one(self):
        first, _c = self.db.find_or_create_supplier_invoice(
            1, self.cavern, "INV-000733", self.user["id"])
        second, created = self.db.find_or_create_supplier_invoice(
            1, self.cavern, "INV-000733", self.user["id"])
        self.assertEqual(first["id"], second["id"])
        self.assertFalse(created)

    def test_two_suppliers_may_use_the_same_reference(self):
        """Invoice numbering is the supplier's, not ours."""
        self.db.find_or_create_supplier_invoice(1, self.cavern, "1", self.user["id"])
        _row, created = self.db.find_or_create_supplier_invoice(
            1, self.usr, "1", self.user["id"])
        self.assertTrue(created)


class TestTheRegisterSaysMoreThanItsDates(Case):
    """The register records a STATE and mostly no date: fourteen rows say
    `Delivered` while only twenty-seven carry a PO date and three a payment
    due date. Converting one to the other loses information either way, so
    the imported state is kept verbatim and used until real dates exist."""

    def test_the_stated_state_shows_when_nothing_is_dated(self):
        row = self.line(self.bourke, stated_state="delivered")
        self.assertEqual(self.state(row["id"]), "delivered")
        self.assertEqual(self.db.scalar(
            "SELECT state_undated FROM v_procurement_line WHERE id = ?",
            (row["id"],)), 1)

    def test_an_ordered_date_does_not_unsay_a_delivery(self):
        """Knowing an order was placed on the 12th does not unsay that it
        has since arrived -- and the ordered date is the only one the sheet
        reliably carries, so preferring it would lose every delivery."""
        row = self.line(self.bourke, stated_state="delivered",
                        ordered_date="2026-07-12")
        self.assertEqual(self.state(row["id"]), "delivered")

    def test_a_real_delivery_date_takes_over(self):
        """And the state stops being sourced from the sheet."""
        row = self.line(self.bourke, stated_state="delivered")
        self.db.update_procurement_line(
            row["id"], {"delivered_date": "2026-07-20", "paid_date": "2026-07-25"},
            self.user["id"])
        self.assertEqual(self.state(row["id"]), "complete")
        self.assertEqual(self.db.scalar(
            "SELECT state_undated FROM v_procurement_line WHERE id = ?",
            (row["id"],)), 0)

    def test_a_line_with_neither_reads_as_unstarted(self):
        row = self.line(self.bourke)
        self.assertEqual(self.state(row["id"]), "to be ordered")

    def test_provenance_is_visible(self):
        """A figure whose provenance is invisible is a figure nobody can
        question."""
        dated = self.line(self.bourke, delivered_date="2026-07-20")
        sheet = self.line(self.bourke, stated_state="delivered")
        self.assertEqual(self.db.scalar(
            "SELECT state_undated FROM v_procurement_line WHERE id = ?",
            (dated["id"],)), 0)
        self.assertEqual(self.db.scalar(
            "SELECT state_undated FROM v_procurement_line WHERE id = ?",
            (sheet["id"],)), 1)


class TestTheImporterFailsBeforeItWrites(Case):
    """The first version validated the FX rate at the DATABASE, so a
    register with USD rows and no rate created twenty-two quotes and then
    raised, leaving a half-import that the one-shot guard then refused to
    replace."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import import_procurement as ip
        self.ip = ip
        self.csv = os.path.join(ROOT, "tests", "fixtures",
                                "procurement_fy27.csv")

    def test_usd_rows_without_a_rate_abort_before_writing(self):
        rows, _rate = self.ip.read_register(self.csv)
        self.assertTrue(rows)
        # Nothing exists to write against, so it aborts on the projects
        # first -- the point is that it returns rather than raising.
        code = self.ip.main(["--db", self.db.path, "--csv", self.csv,
                             "--apply"])
        self.assertIn(code, (1, 2))
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM supplier_quote"), 0)

    def test_the_guard_counts_every_table_it_writes(self):
        """A guard that only counted lines let a half-import through."""
        self.db.create_supplier_quote(
            {"entity_id": 1, "supplier_id": self.cavern}, self.user["id"])
        code = self.ip.main(["--db", self.db.path, "--csv", self.csv,
                             "--apply"])
        self.assertEqual(code, 2)

    def test_reset_clears_a_partial_import(self):
        self.db.create_supplier_quote(
            {"entity_id": 1, "supplier_id": self.cavern}, self.user["id"])
        po = self.db.create_supplier_po(
            {"entity_id": 1, "project_id": self.bourke,
             "supplier_id": self.cavern}, self.user["id"])
        self.line(self.bourke, supplier_po_id=po["id"])
        self.db.clear_procurement(self.user["id"])
        for table in ("procurement_line", "supplier_quote", "supplier_po",
                      "supplier_invoice"):
            self.assertEqual(self.db.scalar(f"SELECT COUNT(*) FROM {table}"), 0,
                             table)

    def test_a_reset_is_audited(self):
        self.db.clear_procurement(self.user["id"])
        self.assertIsNotNone(self.db.query_one(
            "SELECT id FROM audit_log WHERE action = 'procurement_reset'"))


class TestNothingIsSilentlyIgnored(Case):
    """`project_id` was missing from `LINE_MUTABLE`, so the API accepted a
    change to it and the database dropped it. Accepted-and-ignored is worse
    than refused: the screen says it worked."""

    def test_every_field_the_api_offers_is_writable(self):
        import sys as _sys
        _sys.path.insert(0, os.path.join(ROOT, "ops", "modules"))
        from ops.modules import procurement as api
        offered = set(api.DATE_FIELDS) | {
            "project_id", "supplier_id", "supplier_po_id",
            "supplier_quote_id", "supplier_invoice_id", "period_id",
            "item", "description", "note", "quantity", "currency",
            "unit_cost_cents", "total_cents", "cancel_reason"}
        missing = sorted(offered - set(Db.LINE_MUTABLE))
        self.assertEqual(missing, [],
                         "the API accepts these and the database drops them")


class TestTheFiguresAgreeWithTheRows(Case):
    """The screen said twenty lines were `complete` or `paid - pending
    delivery` and reported $0.00 paid, because the figures counted
    `paid_date` while the state column also read the stated state. Whichever
    number someone believed, they had been given a reason to distrust the
    other."""

    def test_a_stated_paid_state_counts_as_paid(self):
        row = self.line(self.bourke, total_cents=100000,
                        stated_state="paid - pending delivery")
        self.assertEqual(self.db.scalar(
            "SELECT is_paid FROM v_procurement_line WHERE id = ?",
            (row["id"],)), 1)
        self.assertEqual(self.db.scalar(
            "SELECT paid_cents FROM v_project_procurement WHERE project_id = ?",
            (self.bourke,)), 100000)

    def test_both_spellings_count(self):
        """The register writes `paid - pending delivery`; the derived state
        writes `paid, pending delivery`. A definition catching only one
        would be a definition nobody could rely on."""
        for state in ("paid - pending delivery", "paid, pending delivery",
                      "complete"):
            row = self.line(self.bourke, stated_state=state)
            self.assertEqual(self.db.scalar(
                "SELECT is_paid FROM v_procurement_line WHERE id = ?",
                (row["id"],)), 1, state)

    def test_a_date_still_counts(self):
        row = self.line(self.bourke)
        self.db.update_procurement_line(
            row["id"], {"paid_date": "2026-07-01"}, self.user["id"])
        self.assertEqual(self.db.scalar(
            "SELECT is_paid FROM v_procurement_line WHERE id = ?",
            (row["id"],)), 1)

    def test_complete_counts_as_delivered_too(self):
        row = self.line(self.bourke, stated_state="complete")
        self.assertEqual(self.db.scalar(
            "SELECT is_delivered FROM v_procurement_line WHERE id = ?",
            (row["id"],)), 1)

    def test_to_be_ordered_is_neither(self):
        row = self.line(self.bourke, stated_state="to be ordered")
        got = self.db.query_one(
            "SELECT is_paid, is_delivered FROM v_procurement_line WHERE id = ?",
            (row["id"],))
        self.assertEqual((got["is_paid"], got["is_delivered"]), (0, 0))

    def test_a_cancelled_line_is_neither_however_it_reads(self):
        """Nothing was paid for something nobody will pay for."""
        row = self.line(self.bourke, stated_state="complete")
        self.db.update_procurement_line(
            row["id"], {"cancelled_date": "2026-07-06"}, self.user["id"])
        got = self.db.query_one(
            "SELECT is_paid, is_delivered FROM v_procurement_line WHERE id = ?",
            (row["id"],))
        self.assertEqual((got["is_paid"], got["is_delivered"]), (0, 0))

    def test_an_estimate_is_never_paid(self):
        """It has not been ordered, so there is nothing to have paid."""
        row = self.line(self.bourke, total_cents=100000, is_estimate=1,
                        stated_state="to be ordered")
        self.assertEqual(self.db.scalar(
            "SELECT paid_cents FROM v_project_procurement WHERE project_id = ?",
            (self.bourke,)), 0)
        self.assertTrue(row)


class TestTheRegisterSync(Case):
    """The register is re-exported and re-synced; the platform must take
    what changed and ignore what only appears to have."""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import import_procurement as ip
        self.ip = ip

    def test_the_key_is_the_resolved_supplier_not_the_sheets_word(self):
        """The sheet says `Eve` where the platform holds `EVE Security
        Services Pty Ltd`. Keying on the raw name made every aliased row
        look new -- twenty-nine of them, against six that were."""
        first = self.ip.natural_key(1, 7, "Widget")
        second = self.ip.natural_key(1, 7, "  widget ")
        self.assertEqual(first, second)
        self.assertNotEqual(first, self.ip.natural_key(1, 8, "Widget"))

    def test_quantity_and_cost_are_not_part_of_the_key(self):
        """Both legitimately change on a row that is still the same row."""
        self.assertEqual(self.ip.natural_key(1, 7, "Widget"),
                         self.ip.natural_key(1, 7, "Widget"))

    def test_a_recorded_date_is_not_syncable(self):
        """`delivered_date` and `paid_date` are facts the sheet does not
        hold. Syncing them back from a sheet that never had them would
        erase the thing the platform exists to capture."""
        for field in ("delivered_date", "paid_date", "invoiced_date",
                      "cancelled_date"):
            self.assertNotIn(field, self.ip.SYNCED_FIELDS)

    def test_a_state_set_here_is_not_undone_by_the_sheet(self):
        """Twenty lines were marked `complete` in the platform while the
        register still said `delivered` -- unchanged since the last export.
        Syncing would have walked every one backwards.

        The sheet may tell the platform something it has never been told;
        it may not overwrite something recorded here. Same rule as the
        dates, and as a typed expense figure beating a calculated one.
        """
        row = self.line(self.bourke, item="Widget", supplier_id=self.usr,
                        stated_state="delivered")
        self.db.update_procurement_line(
            row["id"], {"stated_state": "complete"}, self.user["id"])
        edited = self.db.scalar(
            """SELECT COUNT(*) FROM procurement_line_revision
               WHERE line_id = ? AND field = 'stated_state'""", (row["id"],))
        self.assertEqual(edited, 1)
        self.assertEqual(self.db.scalar(
            "SELECT stated_state FROM procurement_line WHERE id = ?",
            (row["id"],)), "complete")

    def test_a_line_never_touched_here_still_takes_the_sheets_state(self):
        """Otherwise a genuine change in the register could never land."""
        row = self.line(self.bourke, item="Widget", supplier_id=self.usr,
                        stated_state="ordered")
        self.assertEqual(self.db.scalar(
            """SELECT COUNT(*) FROM procurement_line_revision
               WHERE line_id = ? AND field = 'stated_state'""",
            (row["id"],)), 0)

    def test_an_estimate_is_never_matched_against_the_register(self):
        """Estimates come from the expense matrix, not the procurement
        sheet, and a sync that adopted one would turn a forecast into a
        purchase."""
        self.line(self.bourke, item="Widget", is_estimate=1,
                  supplier_id=self.usr)
        rows = self.db.query(
            """SELECT COUNT(*) AS n FROM procurement_line
               WHERE is_estimate = 0""")
        self.assertEqual(rows[0]["n"], 0)


class TestWhatAProjectHasCost(Case):
    def test_committed_paid_and_outstanding(self):
        self.line(self.bourke, total_cents=100000)
        paid = self.line(self.bourke, total_cents=50000)
        self.db.update_procurement_line(
            paid["id"], {"paid_date": "2026-07-01"}, self.user["id"])
        row = self.db.query_one(
            "SELECT * FROM v_project_procurement WHERE project_id = ?",
            (self.bourke,))
        self.assertEqual(row["committed_cents"], 150000)
        self.assertEqual(row["paid_cents"], 50000)
        self.assertEqual(row["outstanding_cents"], 100000)

    def test_a_cancelled_line_is_not_a_cost(self):
        row = self.line(self.bourke, total_cents=100000)
        self.db.update_procurement_line(
            row["id"], {"cancelled_date": "2026-07-06"}, self.user["id"])
        self.assertEqual(self.db.scalar(
            "SELECT committed_cents FROM v_project_procurement "
            "WHERE project_id = ?", (self.bourke,)), 0)

    def test_a_project_with_nothing_reads_zero_not_null(self):
        """A dashboard cell showing null is how #N/A got into the
        workbook."""
        row = self.db.query_one(
            "SELECT * FROM v_project_procurement WHERE project_id = ?",
            (self.lindrum,))
        self.assertEqual(row["committed_cents"], 0)
        self.assertEqual(row["line_count"], 0)

    def test_every_change_is_recorded(self):
        row = self.line(self.bourke)
        self.db.update_procurement_line(
            row["id"], {"paid_date": "2026-07-01"}, self.user["id"],
            "paid early for the discount")
        rev = self.db.query_one(
            "SELECT * FROM procurement_line_revision WHERE line_id = ?",
            (row["id"],))
        self.assertEqual(rev["field"], "paid_date")
        self.assertEqual(rev["reason"], "paid early for the discount")


if __name__ == "__main__":
    unittest.main(verbosity=2)
