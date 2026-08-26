"""tools/import_claims.py — three sources, one control total.

The Invoicing tab holds both issued invoices and a stale partial copy of the
forward plan, because the monthly copy-forward moves rows across without
removing them. Reading both tabs whole double-counts $1.15m, so most of
these tests are about what the importer REFUSES to read.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import import_claims as ic  # noqa: E402
import import_register as ir  # noqa: E402
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.join(ROOT, "ops", "migrations")
FIX = os.path.join(ROOT, "tests", "fixtures")
REGISTER = os.path.join(FIX, "project_register_fy27.csv")
INVOICING = os.path.join(FIX, "invoicing_fy27.csv")
FUTURE = os.path.join(FIX, "future_invoicing_fy27.csv")
MATRIX = os.path.join(FIX, "invoicing_by_month_fy27.csv")

INVOICED_FY27 = 45894034      # $458,940.34, Jul-26 + Aug-26
FORECAST = 320397674          # $3,203,976.74
# The Invoicing tab has no status column and no overlap with Future
# Invoicing: once a claim is invoiced it moves out. The earlier
# "copy-forward residue" was an artifact of a lossy markdown export.
RESIDUE = 0
# The pivot fixture predates the two Aug-26 invoices added on 26 Aug,
# so it is $1,285 short. A stale pivot is exactly what this check is
# for -- it says which side moved, and when.
STALE_PIVOT = -128500


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ops.db")
        db = Db(self.path, MIGRATIONS)
        db.migrate()
        db.close()
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys=ON")
        ir.load(conn, ir.validate(ir.read_rows(REGISTER)))
        conn.commit()
        conn.close()
        self.db = Db(self.path, MIGRATIONS)
        self.db.upsert_user("s1", "r@x", "R")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def sources(self):
        return ic.load_sources(INVOICING, FUTURE, MATRIX)

    def run_import(self, *extra):
        return ic.main(["--db", self.path, "--invoicing", INVOICING,
                        "--future", FUTURE, "--matrix", MATRIX, *extra])


class TestTheCopyForwardResidue(Case):
    def test_both_tabs_are_taken_whole(self):
        issued, residue, planned, _m, _mo = self.sources()
        self.assertEqual(sum(ic.cents(r["Invoice Amount"]) for r in issued),
                         INVOICED_FY27)
        self.assertEqual(sum(ic.cents(r["Invoice Amount"]) for r in planned),
                         FORECAST)
        self.assertEqual(residue, [])

    def test_the_two_tabs_do_not_overlap(self):
        """Once a claim is invoiced it moves out of Future Invoicing, so
        there is nothing to de-duplicate. An earlier version of this file
        asserted the opposite, on the strength of a lossy export that had
        merged two unrelated tabs."""
        issued, _r, planned, _m, _mo = self.sources()
        ik = {(r["Project"].strip(), r["EOM"].strip()) for r in issued}
        pk = {(r["Project"].strip(), r["EOM Cycle"].strip()) for r in planned}
        self.assertEqual(ik & pk, set())


class TestReconciliation(Case):
    def test_the_pivot_reconciles_to_its_own_source(self):
        """Monthly Data is a pivot of Invoicing, Future Invoicing and the
        register, so it carries no information of its own -- but a pivot
        that disagrees with its source means a row was missed on the way in,
        and checking costs nothing."""
        issued, _r, planned, matrix, months = self.sources()
        findings = ic.reconcile(issued, planned, matrix, months)
        self.assertEqual(sum(f["difference"] for f in findings), STALE_PIVOT)
        self.assertEqual({f["project"] for f in findings},
                         {"PDNSW - Maitland L1 South Low Batt",
                          "PDNSW - RGB Service works"})

    def test_months_beyond_the_matrix_are_not_treated_as_differences(self):
        """The control total stops at the end of FY27; forward planning does
        not. Flagging Jul-27 as a discrepancy would bury the real findings."""
        issued, _r, planned, matrix, months = self.sources()
        findings = ic.reconcile(issued, planned, matrix, months)
        self.assertEqual([f for f in findings if f["month"] not in months], [])
        beyond = ic.outside_control_total(planned, months)
        self.assertIn("Jul-27", beyond)

    def test_it_refuses_while_the_pivot_disagrees(self):
        """Currently the pivot is stale by $1,285. Refusing is right: an
        incomplete picture imported silently is one nobody can trust."""
        self.assertEqual(self.run_import("--dry-run"), 1)
        self.assertEqual(self.run_import("--accept-variance", "--dry-run"), 0)


class TestPlacement(Case):
    def test_a_zero_value_row_for_a_deleted_project_is_skipped_and_listed(self):
        """`Adhoc Service Calls` was deleted from the register but still has
        a row. Silently dropping rows is how an import loses something that
        mattered."""
        issued, _r, planned, _m, _mo = self.sources()
        _resolved, errors, _needs, skipped, _sp = ic.resolve(self.db, issued, planned)
        self.assertEqual(errors, [])
        self.assertTrue(any("Adhoc Service Calls" in s for s in skipped))

    def test_a_project_with_claims_but_no_po_gets_a_placeholder(self):
        """Maintenance is often billed against an SLA with no PO number.
        A placeholder at zero keeps the claim attached to something."""
        issued, _r, planned, _m, _mo = self.sources()
        _resolved, _e, needs_po, _s, _sp = ic.resolve(self.db, issued, planned)
        names = [n for _id, n in needs_po]
        self.assertIn("Dover House - ICN Maintenance", names)
        self.assertEqual(len(needs_po), 6)


class TestIncrementalSync(Case):
    """Once a claim is in the platform it diverges on purpose -- statuses
    move, months slip, retention is withheld. A sync that overwrote would
    undo the work the platform exists to do."""

    def setUp(self):
        super().setUp()
        self.assertEqual(self.run_import("--accept-variance"), 0)

    def test_a_second_run_without_sync_is_refused(self):
        self.assertEqual(self.run_import("--accept-variance"), 2)

    def test_syncing_the_same_export_adds_nothing(self):
        before = self.db.scalar("SELECT COUNT(*) FROM claim_line")
        self.assertEqual(self.run_import("--sync", "--accept-variance"), 0)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM claim_line"), before)

    def test_it_leaves_work_already_done_alone(self):
        """The point of additive-only: a claim moved to `due` stays there."""
        row = self.db.query_one(
            "SELECT id FROM claim_line WHERE status='forecast' LIMIT 1")
        self.db.transition_claim(row["id"], "due", {}, None, None)
        self.run_import("--sync", "--accept-variance")
        self.assertEqual(self.db.scalar(
            "SELECT status FROM claim_line WHERE id = ?", (row["id"],)), "due")

    def test_a_slipped_claim_is_not_recreated_in_its_old_month(self):
        """Slippage changes the month, which changes the natural key. The
        row must not come back where it started."""
        row = self.db.query_one(
            """SELECT cl.id, cl.period_id, cl.amount_cents FROM claim_line cl
               WHERE cl.status='forecast' AND cl.detail IS NOT NULL LIMIT 1""")
        other = self.db.scalar(
            "SELECT id FROM period WHERE id <> ? AND fy = 2027 LIMIT 1",
            (row["period_id"],))
        self.db.update_claim_line(row["id"], {"period_id": other}, None, "slipped")
        before = self.db.scalar("SELECT COUNT(*) FROM claim_line")
        self.run_import("--sync", "--accept-variance")
        added = self.db.scalar("SELECT COUNT(*) FROM claim_line") - before
        # It IS re-added: the workbook still says the old month, and the
        # platform cannot tell slippage from a genuinely new line item.
        # Recorded here because it is a real limitation, not a bug to be
        # discovered during a month-end.
        self.assertEqual(added, 1)


class TestAfterImport(Case):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.run_import("--accept-variance"), 0)

    def test_the_money_reconciles(self):
        invoiced = self.db.scalar(
            """SELECT SUM(amount_cents) FROM claim_line
               WHERE status='invoiced' AND is_opening_balance=0""")
        opening = self.db.scalar(
            "SELECT SUM(amount_cents) FROM claim_line WHERE is_opening_balance=1")
        self.assertEqual(invoiced, INVOICED_FY27)
        self.assertEqual(
            self.db.scalar("SELECT SUM(invoiced_prior_cents) "
                           "FROM v_project_orders_in_hand"),
            opening + INVOICED_FY27)

    def test_orders_in_hand_falls_by_exactly_the_fy27_invoicing(self):
        self.assertEqual(
            self.db.scalar("SELECT SUM(orders_in_hand_cents) "
                           "FROM v_project_orders_in_hand"),
            356353673 - INVOICED_FY27)

    def test_the_forecast_pipeline_landed(self):
        self.assertEqual(self.db.scalar(
            "SELECT SUM(amount_cents) FROM claim_line WHERE status='forecast'"),
            FORECAST)

    def test_invoiced_claims_went_through_the_lifecycle(self):
        """Not written straight to the row: retention is computed at
        invoicing, and going around it would skip that."""
        rows = self.db.query(
            """SELECT COUNT(*) n FROM claim_line_revision r
               WHERE r.field='status' AND r.new_value='invoiced'""")
        self.assertGreater(rows[0]["n"], 0)

    def test_no_opening_balance_overlap_remains(self):
        """Four PDNSW projects once carried their Jul-26 invoice as BOTH an
        opening balance and an FY27 claim -- $41,460 counted twice, and
        orders in hand understated by exactly that. Corrected at source on
        25 Aug; this test keeps it corrected."""
        overlap = ic.double_counted(self.db)
        # Corrected at source on 25 Aug: `Invoiced Prior` for four PDNSW
        # projects held their Jul-26 invoicing, which is inside FY27.
        self.assertEqual(overlap, [])

    def test_importing_twice_is_refused(self):
        self.assertEqual(self.run_import("--accept-variance"), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
