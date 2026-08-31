"""Migration 001 + register importer.

Fresh temp DB through the REAL migration file every time -- never a hand-built
schema, or the tests stop testing the thing that ships.

Financial assertions are pinned to the validated FY27 register (20 Aug 2026).
A change to these numbers is either a real regression or a deliberate source
correction, and either way it should require editing this file on purpose.
"""

import csv
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, ROOT)
import import_register as imp  # noqa: E402
from ops.db import Db, MigrationError  # noqa: E402

MIGRATIONS = os.path.join(ROOT, "ops", "migrations")
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "project_register_fy27.csv")

# --- pinned known-good values -------------------------------------------
PROJECTS = 65
PO_CENTS = 723394200          # $7,233,942.00
PRIOR_CENTS = 367040527       # $3,670,405.27
ORDERS_IN_HAND_CENTS = 356353673  # $3,563,536.73  <- FY27 opening position
OPENING_ROWS = 25
PERIOD_ROWS = 144             # FY24..FY35


def migrate(db_path):
    """Uses the REAL runner from ops.db -- not a copy of its logic, or the
    tests stop testing the thing that ships."""
    db = Db(db_path, MIGRATIONS)
    db.migrate()
    return db


class Base(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db)
        self.dbo = migrate(self.db)
        self.conn = self.dbo._write

    def tearDown(self):
        self.dbo.close()
        if os.path.exists(self.db):
            os.unlink(self.db)


class TestMigration(Base):
    def test_every_table_is_strict(self):
        rows = self.conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'").fetchall()
        self.assertTrue(rows)
        for name, sql in rows:
            self.assertRegex(sql.replace("\n", " "), r"\)\s*STRICT\s*$",
                             f"{name} is not STRICT")

    def test_periods_seeded_july_first(self):
        n, = self.conn.execute("SELECT COUNT(*) FROM period").fetchone()
        self.assertEqual(n, PERIOD_ROWS)
        # FY label is the year the FY ENDS in: FY27 starts 1 Jul 2026.
        start, label = self.conn.execute(
            "SELECT month_start, label FROM period WHERE fy=2027 AND month_no=1"
        ).fetchone()
        self.assertEqual(start, "2026-07-01")
        self.assertEqual(label, "Jul-26")
        end, = self.conn.execute(
            "SELECT month_end FROM period WHERE fy=2027 AND month_no=12").fetchone()
        self.assertEqual(end, "2027-06-30")

    def test_audit_log_is_append_only(self):
        self.conn.execute(
            "INSERT INTO audit_log (ts, action, target_type) VALUES (1,'x','y')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE audit_log SET action='z'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM audit_log")

    def test_job_code_alias_allows_one_to_many(self):
        """JN-4335 and JN-4407 each cover two projects. This must not raise."""
        self.conn.execute("INSERT INTO client (entity_id, name) VALUES (1,'c')")
        for n in ("a", "b"):
            self.conn.execute(
                """INSERT INTO project (entity_id, name, job_code, status, created_ts)
                   VALUES (1, ?, 'JN-4335', 'Active', 0)""", (n,))
        ids = [r[0] for r in self.conn.execute("SELECT id FROM project")]
        for pid in ids:
            self.conn.execute(
                """INSERT INTO job_code_alias (legacy_code, project_id, created_ts)
                   VALUES ('JN-4335', ?, 0)""", (pid,))
        n, = self.conn.execute(
            "SELECT COUNT(*) FROM job_code_alias WHERE legacy_code='JN-4335'").fetchone()
        self.assertEqual(n, 2)

    def test_project_cannot_be_over_invoiced(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO project (entity_id, name, job_code, status,
                       purchase_order_cents, invoiced_prior_cents, created_ts)
                   VALUES (1,'x','JN-1','Active', 100, 101, 0)""")

    def test_roles_are_enumerated(self):
        self.conn.execute(
            """INSERT INTO users (oidc_sub,email,display_name,created_ts)
               VALUES ('s','e@x','n',0)""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO user_entity_role (user_id, entity_id, role, granted_ts)
                   VALUES (1,1,'superuser',0)""")


class TestMoneyParsing(unittest.TestCase):
    def test_cents_are_exact(self):
        for raw, want in [("$1,234.56", 123456), ("$0.00", 0), ("", 0), ("-", 0),
                          ("$700,000", 70000000), ("$3,563,536.73", 356353673),
                          ("$45,361", 4536100), ("($550.00)", -55000)]:
            self.assertEqual(imp.cents(raw), want, raw)

    def test_no_float_drift_on_the_pinned_total(self):
        """Summing the fixture in cents must hit the pinned total exactly."""
        with open(FIXTURE, newline="", encoding="utf-8-sig") as f:
            rows = [r for r in csv.DictReader(f) if r["Project"].strip()]
        self.assertEqual(sum(imp.cents(r["Purchase Order"]) for r in rows), PO_CENTS)

    def test_rejects_garbage_rather_than_guessing(self):
        with self.assertRaises(imp.ImportError_):
            imp.cents("about $500")


class TestClassify(unittest.TestCase):
    def test_canonical_codes_pass_through(self):
        for c in ("JN-6631", "JN-4335"):
            self.assertEqual(imp.classify(c), (None, c, None))

    def test_known_good_non_jn_codes_are_left_alone(self):
        """P-3655, P-3707 and JN-CommS are valid. Cleverness corrupts them."""
        for c in ("P-3655", "P-3707", "JN-CommS"):
            self.assertEqual(imp.classify(c), (None, c, None))

    def test_format_variant_is_canonicalised_and_aliased(self):
        self.assertEqual(imp.classify("JN 5108"), ("A", "JN-5108", "JN 5108"))

    def test_placeholders_become_class_b(self):
        for c in ("TBA", "na", "Various", ""):
            cls, _, _ = imp.classify(c)
            self.assertEqual(cls, "B", c)

    def test_unknown_shape_is_flagged_not_guessed(self):
        cls, _, _ = imp.classify("job 12 maybe")
        self.assertEqual(cls, "B")


class TestThePreviousReleaseCanStillWrite(Base):
    """Caught by the N-1 gate, which is what it is for.

    Migration `007` made `contract_value_cents` authoritative and backfilled
    it from the `customer_po` rows migration `003` had created. That
    backfill ran once. The PREVIOUS release's importer writes
    `purchase_order_cents` and creates no such rows, so a project it
    inserted against this schema had a contract of ZERO and every derived
    figure read zero.

    Not only a test artefact: roll back, create a project, roll forward, and
    that project has no contract.
    """

    def test_a_project_written_the_old_way_still_has_a_contract(self):
        self.conn.execute(
            """INSERT INTO project (entity_id, name, job_code, status,
                   created_ts, purchase_order_cents, invoiced_prior_cents)
               VALUES (1,'Old Code','JN-1,1','Active',0,29500000,8850000)""")
        self.assertEqual(self.conn.execute(
            "SELECT contract_value_cents FROM project WHERE name='Old Code'"
        ).fetchone()[0], 29500000)

    def test_it_shows_in_orders_in_hand(self):
        self.conn.execute(
            """INSERT INTO project (entity_id, name, job_code, status,
                   created_ts, purchase_order_cents)
               VALUES (1,'Old Code','JN-1,1','Active',0,29500000)""")
        self.assertEqual(self.conn.execute(
            """SELECT orders_in_hand_cents FROM v_project_orders_in_hand
               WHERE project_name = 'Old Code'""").fetchone()[0], 29500000)

    def test_a_contract_set_the_new_way_is_not_touched(self):
        self.conn.execute(
            """INSERT INTO project (entity_id, name, job_code, status,
                   created_ts, contract_value_cents)
               VALUES (1,'New','JN-2,2','Active',0,19861000)""")
        self.conn.execute(
            "UPDATE project SET purchase_order_cents = 1 WHERE name='New'")
        self.assertEqual(self.conn.execute(
            "SELECT contract_value_cents FROM project WHERE name='New'"
        ).fetchone()[0], 19861000)

    def test_invoiced_falls_back_to_the_project_column(self):
        """Migration `003` turned `invoiced_prior_cents` into opening-balance
        claims and `007`'s view reads those. Both ran once. The previous
        release writes the column and creates no claims, so its projects
        read as never invoiced and orders in hand came out as the WHOLE
        contract -- overstating what is left to bill, which is the worse
        direction for that figure to be wrong in."""
        self.conn.execute(
            """INSERT INTO project (entity_id, name, job_code, status,
                   created_ts, purchase_order_cents, invoiced_prior_cents)
               VALUES (1,'Old Code','JN-1,1','Active',0,29500000,8850000)""")
        row = self.conn.execute(
            """SELECT invoiced_prior_cents, orders_in_hand_cents
               FROM v_project_orders_in_hand
               WHERE project_name = 'Old Code'""").fetchone()
        self.assertEqual(row[0], 8850000)
        self.assertEqual(row[1], 29500000 - 8850000)

    def test_one_claim_supersedes_the_column(self):
        """The claim rows are the record; the column is what preceded them.
        `EXISTS` rather than a sum, so a project whose claims total zero
        reads zero instead of falling back to a figure it has superseded."""
        self.conn.execute(
            """INSERT INTO project (entity_id, name, job_code, status,
                   created_ts, purchase_order_cents, invoiced_prior_cents)
               VALUES (1,'Both','JN-4,4','Active',0,29500000,8850000)""")
        project_id = self.conn.execute(
            "SELECT id FROM project WHERE name='Both'").fetchone()[0]
        self.conn.execute(
            """INSERT INTO claim_line (entity_id, project_id, status,
                   amount_cents, is_opening_balance, claim_date,
                   invoiced_date, created_ts)
               VALUES (1,?, 'invoiced', 1000000, 1, '2026-06-30',
                       '2026-06-30', 0)""", (project_id,))
        self.assertEqual(self.conn.execute(
            """SELECT invoiced_prior_cents FROM v_project_orders_in_hand
               WHERE project_name = 'Both'""").fetchone()[0], 1000000)

    def test_a_zero_purchase_order_changes_nothing(self):
        """The trigger fires only where there is nothing to lose."""
        self.conn.execute(
            """INSERT INTO project (entity_id, name, job_code, status,
                   created_ts) VALUES (1,'Empty','JN-3,3','Active',0)""")
        self.assertEqual(self.conn.execute(
            "SELECT contract_value_cents FROM project WHERE name='Empty'"
        ).fetchone()[0], 0)


class TestImport(Base):
    def setUp(self):
        super().setUp()
        rows = imp.read_rows(FIXTURE)
        self.parsed = imp.validate(rows)
        imp.load(self.conn, self.parsed)

    def q(self, sql):
        return self.conn.execute(sql).fetchone()[0]

    def test_pinned_financials(self):
        self.assertEqual(self.q("SELECT COUNT(*) FROM project"), PROJECTS)
        self.assertEqual(self.q("SELECT SUM(purchase_order_cents) FROM project"), PO_CENTS)
        self.assertEqual(self.q("SELECT SUM(invoiced_prior_cents) FROM project"), PRIOR_CENTS)
        self.assertEqual(
            self.q("SELECT SUM(orders_in_hand_cents) FROM v_project_orders_in_hand"),
            ORDERS_IN_HAND_CENTS)
        self.assertEqual(
            self.q("SELECT COUNT(*) FROM project WHERE invoiced_prior_cents > 0"),
            OPENING_ROWS)

    def test_register_balances_to_zero(self):
        self.assertEqual(PO_CENTS - PRIOR_CENTS - ORDERS_IN_HAND_CENTS, 0)

    def test_every_project_belongs_to_an_entity(self):
        self.assertEqual(self.q("SELECT COUNT(*) FROM project WHERE entity_id IS NULL"), 0)
        self.assertEqual(self.q(
            "SELECT COUNT(DISTINCT entity_id) FROM project"), 1)

    def test_shared_codes_are_flagged_not_merged(self):
        shared = [tuple(r) for r in self.conn.execute(
            """SELECT job_code, COUNT(*) FROM project WHERE job_code LIKE 'JN-%'
               GROUP BY job_code HAVING COUNT(*) > 1 ORDER BY job_code""").fetchall()]
        self.assertEqual(shared, [("JN-4335", 2), ("JN-4407", 2)])
        # both projects keep their own history via a one-to-many alias
        for code in ("JN-4335", "JN-4407"):
            self.assertEqual(self.conn.execute(
                "SELECT COUNT(*) FROM job_code_alias WHERE legacy_code=?",
                (code,)).fetchone()[0], 2)

    def test_no_genuine_collisions_remain(self):
        """JN-676 and JN-5416 were resolved at source; each maps to one project."""
        for code in ("JN-676", "JN-5416"):
            n = self.q(f"SELECT COUNT(*) FROM project WHERE job_code='{code}'")
            self.assertLessEqual(n, 1, f"{code} is a merged-history collision")

    def test_worklist_is_flagged_and_open(self):
        self.assertEqual(self.q(
            "SELECT COUNT(*) FROM job_code_issue WHERE status='open'"),
            self.q("SELECT COUNT(*) FROM job_code_issue"))
        # class B is the placeholders; nothing blocked the import
        self.assertEqual(self.q("SELECT COUNT(*) FROM job_code_issue WHERE class='B'"), 9)
        self.assertEqual(self.q(
            "SELECT COUNT(*) FROM project WHERE needs_resolution=1"), 13)

    def test_job_numbers_resume_above_the_legacy_high_water_mark(self):
        nxt = self.q("SELECT next_value FROM job_number_sequence")
        high = self.q("""SELECT MAX(CAST(substr(job_code,4) AS INTEGER)) FROM project
                         WHERE job_code GLOB 'JN-[0-9]*'""")
        self.assertEqual(nxt, high + 1)

    def test_import_is_one_shot(self):
        with self.assertRaises(sqlite3.IntegrityError):
            imp.load(self.conn, self.parsed)

    def test_import_is_audited(self):
        self.assertEqual(self.q(
            "SELECT COUNT(*) FROM audit_log WHERE action='register_import'"), 1)


class TestValidationRefusesBadData(unittest.TestCase):
    def rows(self, **over):
        base = {"Project": "P", "Client": "C", "Job Code": "JN-1", "Project No": "",
                "Type": "ICN", "Status": "Active", "Project Lead": "",
                "Purchase Order": "$100.00", "Invoiced Prior": "$40.00",
                "Contract Value FY27": "$60.00", "Notes": "", "Check": ""}
        base.update(over)
        return [base]

    def test_accepts_a_balanced_row(self):
        self.assertEqual(len(imp.validate(self.rows())), 1)

    def test_rejects_a_row_that_does_not_balance(self):
        with self.assertRaises(imp.ImportError_) as e:
            imp.validate(self.rows(**{"Contract Value FY27": "$59.00"}))
        self.assertIn("out by", str(e.exception))

    def test_rejects_duplicate_project_names(self):
        r = self.rows() * 2
        with self.assertRaises(imp.ImportError_):
            imp.validate(r)

    def test_missing_invoiced_prior_column_is_named_explicitly(self):
        import io
        bad = io.StringIO("Project,Job Code,Purchase Order,Invoiced FY26,"
                          "Contract Value FY27,Status\nP,JN-1,$1,$0,$1,Active\n")
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write(bad.getvalue())
            path = f.name
        try:
            with self.assertRaises(imp.ImportError_) as e:
                imp.read_rows(path)
            self.assertIn("Invoiced Prior", str(e.exception))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
