"""Migration 003 — customer POs, claim lines, and the new orders-in-hand.

The migration moves the source of truth for a figure that people have
already signed off. The tests that matter are the ones proving the number
did not move while its derivation did.
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
import import_register as imp  # noqa: E402
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.join(ROOT, "ops", "migrations")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "project_register_fy27.csv")

PO_CENTS = 723394200
PRIOR_CENTS = 367040527
ORDERS_IN_HAND_CENTS = 356353673
OPENING_ROWS = 25


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ops.db")
        db = Db(self.path, MIGRATIONS)
        db.migrate()
        db.close()
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys=ON")
        imp.load(conn, imp.validate(imp.read_rows(FIXTURE)))
        conn.commit()
        conn.close()
        self.db = Db(self.path, MIGRATIONS)
        self.user = self.db.upsert_user("s1", "r@x", "R")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class TestTheFigureDidNotMove(Case):
    def test_orders_in_hand_still_reconciles(self):
        """The whole point. The derivation changed from two columns on
        `project` to a difference of two fact tables; the answer must not."""
        self.assertEqual(self.db.scalar(
            "SELECT SUM(orders_in_hand_cents) FROM v_project_orders_in_hand"),
            ORDERS_IN_HAND_CENTS)

    def test_the_components_reconcile_too(self):
        po = self.db.scalar(
            "SELECT SUM(purchase_order_cents) FROM v_project_orders_in_hand")
        inv = self.db.scalar(
            "SELECT SUM(invoiced_prior_cents) FROM v_project_orders_in_hand")
        self.assertEqual(po, PO_CENTS)
        self.assertEqual(inv, PRIOR_CENTS)
        self.assertEqual(po - inv, ORDERS_IN_HAND_CENTS)

    def test_the_view_now_reads_the_fact_tables_not_the_columns(self):
        """Prove the source actually changed. Zero the legacy columns: the
        view must be unmoved, or 003 achieved nothing."""
        with self.db._tx() as c:
            c.execute("UPDATE project SET purchase_order_cents = 0, "
                      "invoiced_prior_cents = 0")
        self.assertEqual(self.db.scalar(
            "SELECT SUM(orders_in_hand_cents) FROM v_project_orders_in_hand"),
            ORDERS_IN_HAND_CENTS)

    def test_no_financial_year_appears_in_the_view(self):
        """`contract - claims up to X` answers FY27, FY28 and today with one
        definition. A hardcoded date is the workbook's July ritual in SQL."""
        sql = self.db.scalar(
            "SELECT sql FROM sqlite_master WHERE name='v_project_orders_in_hand'")
        for year in ("2026", "2027", "FY27", "07-01"):
            self.assertNotIn(year, sql)


class TestContractVersusOrdered(Case):
    """The register's `Purchase Order` column was the CONTRACT VALUE.
    Migration 003 turned it into a customer_po row, so adding the real
    orders alongside it double-counted -- `200 Victoria - IBP` read
    $422,833 against a $295,000 contract."""

    def test_the_contract_is_the_projects_own_figure(self):
        self.assertEqual(self.db.scalar(
            "SELECT SUM(contract_value_cents) FROM v_project_orders_in_hand"),
            PO_CENTS)

    def test_adding_an_order_does_not_change_the_contract(self):
        pid = self.db.scalar(
            "SELECT id FROM project WHERE name = '200 Victoria - IBP'")
        before = self.db.scalar(
            "SELECT contract_value_cents FROM v_project_orders_in_hand "
            "WHERE project_id = ?", (pid,))
        with self.db._tx() as c:
            c.execute("""INSERT INTO customer_po (entity_id, project_id,
                             po_number, amount_cents, created_ts)
                         VALUES (1,?, 'PO06932420_255549', 1191660, 0)""", (pid,))
        row = self.db.query_one(
            "SELECT * FROM v_project_orders_in_hand WHERE project_id = ?", (pid,))
        self.assertEqual(row["contract_value_cents"], before)
        self.assertEqual(row["ordered_cents"], 1191660)

    def test_orders_in_hand_still_means_contract_less_invoiced(self):
        """What the register has always meant, and what every pinned figure
        reconciles to. The PO-sum version quietly redefined it."""
        row = self.db.query_one(
            """SELECT SUM(contract_value_cents) c, SUM(invoiced_prior_cents) i,
                      SUM(orders_in_hand_cents) o FROM v_project_orders_in_hand""")
        self.assertEqual(row["o"], row["c"] - row["i"])

    def test_the_migrated_rows_are_placeholders_not_orders(self):
        self.assertEqual(self.db.scalar(
            "SELECT SUM(ordered_cents) FROM v_project_orders_in_hand"), 0)
        self.assertGreater(self.db.scalar(
            "SELECT COUNT(*) FROM customer_po WHERE is_placeholder = 1"), 0)

    def test_a_placeholder_can_still_carry_claims(self):
        """Which is why they were flagged rather than deleted: 204 claims
        reference them and the retention terms sit on them."""
        po = self.db.query_one(
            "SELECT id, project_id, entity_id FROM customer_po "
            "WHERE is_placeholder = 1 LIMIT 1")
        period = self.db.scalar("SELECT id FROM period LIMIT 1")
        with self.db._tx() as c:
            c.execute(
                """INSERT INTO claim_line (entity_id, project_id,
                       customer_po_id, period_id, status, amount_cents, created_ts)
                   VALUES (?,?,?,?, 'forecast', 5000, 0)""",
                (po["entity_id"], po["project_id"], po["id"], period))
        self.assertEqual(self.db.scalar(
            """SELECT COUNT(*) FROM claim_line cl
               JOIN customer_po p ON p.id = cl.customer_po_id
               WHERE p.is_placeholder = 1"""), 1)
        # And it still does not count as ordered.
        self.assertEqual(self.db.scalar(
            "SELECT ordered_cents FROM v_project_orders_in_hand "
            "WHERE project_id = ?", (po["project_id"],)), 0)


class TestOpeningBalances(Case):
    def test_twenty_nine_rows_totalling_the_prior_invoicing(self):
        rows = self.db.query(
            "SELECT amount_cents FROM claim_line WHERE is_opening_balance = 1")
        self.assertEqual(len(rows), OPENING_ROWS)
        self.assertEqual(sum(r["amount_cents"] for r in rows), PRIOR_CENTS)

    def test_they_carry_no_po_and_no_invoice_number(self):
        """They are the boundary of what this platform knows, not claims
        anyone made. Attaching them to a PO would be a guess."""
        self.assertEqual(self.db.scalar(
            """SELECT COUNT(*) FROM claim_line
               WHERE is_opening_balance = 1
                 AND (customer_po_id IS NOT NULL OR invoice_number IS NOT NULL)"""),
            0)

    def test_they_are_immutable(self):
        row = self.db.query_one(
            "SELECT id FROM claim_line WHERE is_opening_balance = 1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db._write.execute(
                "UPDATE claim_line SET amount_cents = 1 WHERE id = ?", (row["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db._write.execute(
                "DELETE FROM claim_line WHERE id = ?", (row["id"],))

    def test_only_an_opening_balance_may_float_free_of_a_po(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db._write.execute(
                """INSERT INTO claim_line (entity_id, project_id, status,
                       amount_cents, is_opening_balance, created_ts)
                   VALUES (1, 1, 'forecast', 100, 0, 0)""")


class TestClaimLifecycle(Case):
    def po_for(self, name="116 Cremorne St - ICN"):
        return self.db.query_one(
            """SELECT po.id, po.project_id, po.entity_id FROM customer_po po
               JOIN project p ON p.id = po.project_id WHERE p.name = ?""",
            (name,))

    def add(self, status, cents):
        po = self.po_for()
        with self.db._tx() as c:
            c.execute(
                """INSERT INTO claim_line (entity_id, project_id,
                       customer_po_id, status, amount_cents, created_ts)
                   VALUES (?,?,?,?,?,0)""",
                (po["entity_id"], po["project_id"], po["id"], status, cents))
        return po["project_id"]

    def test_a_forecast_does_not_reduce_orders_in_hand(self):
        """Forecast is intent, not history. Counting it would report work as
        billed before anyone agreed to bill it."""
        before = self.db.scalar(
            "SELECT SUM(orders_in_hand_cents) FROM v_project_orders_in_hand")
        self.add("forecast", 500000)
        self.assertEqual(self.db.scalar(
            "SELECT SUM(orders_in_hand_cents) FROM v_project_orders_in_hand"),
            before)

    def test_invoicing_reduces_orders_in_hand(self):
        before = self.db.scalar(
            "SELECT SUM(orders_in_hand_cents) FROM v_project_orders_in_hand")
        self.add("invoiced", 500000)
        self.assertEqual(self.db.scalar(
            "SELECT SUM(orders_in_hand_cents) FROM v_project_orders_in_hand"),
            before - 500000)

    def test_paid_counts_the_same_as_invoiced(self):
        """Orders in hand is about what is left to bill, so payment does not
        change it -- a paid claim was already billed."""
        before = self.db.scalar(
            "SELECT SUM(orders_in_hand_cents) FROM v_project_orders_in_hand")
        self.add("paid", 500000)
        self.assertEqual(self.db.scalar(
            "SELECT SUM(orders_in_hand_cents) FROM v_project_orders_in_hand"),
            before - 500000)

    def test_cancelled_does_not_count(self):
        before = self.db.scalar(
            "SELECT SUM(orders_in_hand_cents) FROM v_project_orders_in_hand")
        self.add("cancelled", 500000)
        self.assertEqual(self.db.scalar(
            "SELECT SUM(orders_in_hand_cents) FROM v_project_orders_in_hand"),
            before)

    def test_an_unknown_status_is_refused(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.add("nearly-done", 100)

    def test_the_pipeline_view_separates_the_stages(self):
        """Deltas, not absolutes: this project already carries an opening
        balance, which correctly counts as invoiced. Asserting the absolute
        would be asserting that the opening row does not exist."""
        pid = self.po_for()["project_id"]
        before = self.db.query_one(
            "SELECT * FROM v_project_pipeline WHERE project_id = ?", (pid,))
        self.add("forecast", 100000)
        self.add("due", 200000)
        self.add("invoiced", 300000)
        after = self.db.query_one(
            "SELECT * FROM v_project_pipeline WHERE project_id = ?", (pid,))
        self.assertEqual(after["forecast_cents"] - before["forecast_cents"], 100000)
        self.assertEqual(after["due_cents"] - before["due_cents"], 200000)
        self.assertEqual(after["invoiced_cents"] - before["invoiced_cents"], 300000)
        # And the opening balance is in there, where it belongs.
        self.assertGreater(before["invoiced_cents"], 0)


class TestDualWriteDuringTheExpandWindow(Case):
    """003 is expand-only: the previous release still reads the columns on
    `project`, so both have to stay true until the contraction migration."""

    def test_the_importer_wrote_both(self):
        self.assertEqual(
            self.db.scalar("SELECT SUM(purchase_order_cents) FROM project"),
            self.db.scalar("SELECT SUM(amount_cents) FROM customer_po"))
        self.assertEqual(
            self.db.scalar("SELECT SUM(invoiced_prior_cents) FROM project"),
            self.db.scalar(
                "SELECT SUM(amount_cents) FROM claim_line "
                "WHERE is_opening_balance = 1"))

    def test_creating_a_project_writes_both(self):
        client = self.db.scalar("SELECT id FROM client LIMIT 1")
        type_id = self.db.scalar("SELECT id FROM project_type LIMIT 1")
        p = self.db.create_project(
            {"entity_id": 1, "name": "Dual Write", "client_id": client,
             "type_id": type_id, "status": "Active", "project_lead": "R",
             "job_code": "JN-9500", "purchase_order_cents": 250000,
             "invoiced_prior_cents": 50000}, self.user["id"])
        self.assertEqual(self.db.scalar(
            "SELECT amount_cents FROM customer_po WHERE project_id = ?",
            (p["id"],)), 250000)
        self.assertEqual(self.db.scalar(
            """SELECT amount_cents FROM claim_line
               WHERE project_id = ? AND is_opening_balance = 1""",
            (p["id"],)), 50000)
        row = self.db.query_one(
            "SELECT * FROM v_project_orders_in_hand WHERE project_id = ?",
            (p["id"],))
        self.assertEqual(row["orders_in_hand_cents"], 200000)

    def test_editing_the_contract_value_moves_the_po_too(self):
        """Otherwise the register and the view disagree about one project,
        which is the divergence this whole platform exists to remove."""
        client = self.db.scalar("SELECT id FROM client LIMIT 1")
        type_id = self.db.scalar("SELECT id FROM project_type LIMIT 1")
        p = self.db.create_project(
            {"entity_id": 1, "name": "Editable", "client_id": client,
             "type_id": type_id, "status": "Active", "project_lead": "R",
             "job_code": "JN-9501", "purchase_order_cents": 100000}, self.user["id"])
        self.db.update_project(
            p["id"], {"purchase_order_cents": 175000}, self.user["id"])
        row = self.db.query_one(
            "SELECT * FROM v_project_orders_in_hand WHERE project_id = ?",
            (p["id"],))
        self.assertEqual(row["purchase_order_cents"], 175000)
        self.assertEqual(row["orders_in_hand_cents"], 175000)

    def test_the_po_edit_is_recorded_as_a_revision(self):
        """A PO adjusted in place would retrospectively move every
        orders-in-hand figure ever derived from it."""
        client = self.db.scalar("SELECT id FROM client LIMIT 1")
        type_id = self.db.scalar("SELECT id FROM project_type LIMIT 1")
        p = self.db.create_project(
            {"entity_id": 1, "name": "Revised", "client_id": client,
             "type_id": type_id, "status": "Active", "project_lead": "R",
             "job_code": "JN-9502", "purchase_order_cents": 100000}, self.user["id"])
        self.db.update_project(
            p["id"], {"purchase_order_cents": 175000}, self.user["id"])
        rev = self.db.query_one(
            """SELECT * FROM customer_po_revision r
               JOIN customer_po po ON po.id = r.customer_po_id
               WHERE po.project_id = ?""", (p["id"],))
        self.assertEqual(rev["old_value"], "100000")
        self.assertEqual(rev["new_value"], "175000")


class TestMigrationIsExpandOnly(unittest.TestCase):
    def test_003_drops_nothing_the_previous_release_reads(self):
        """Rollback is automatic and nothing rolls the schema back, so the
        previous release must still find its columns."""
        with open(os.path.join(MIGRATIONS, "003_invoicing.sql"),
                  encoding="utf-8") as f:
            body = f.read().upper()
        self.assertNotIn("DROP TABLE", body)
        self.assertNotIn("ALTER TABLE PROJECT", body)
        # Dropping a VIEW is fine: it is recreated in the same transaction.
        self.assertIn("CREATE VIEW V_PROJECT_ORDERS_IN_HAND", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
