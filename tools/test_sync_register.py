"""tools/sync_register.py — applying a corrected register (ADR-27).

`drift_check.py` finds differences and never writes; this is the other half.
The tests that matter are about the opening balances: they are immutable by
design, and this is the one path allowed to change them.
"""

import csv
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import import_register as ir  # noqa: E402
import sync_register as sr  # noqa: E402
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.join(ROOT, "ops", "migrations")
CORRECTED = os.path.join(ROOT, "tests", "fixtures", "project_register_fy27.csv")
WRONG = {"PDNSW - RGB Renovation Works": "$22,000.00",
         "PDNSW - Nowra CCTV & Break Glass Monitoring": "$12,400.00",
         "PDNSW - 6PSQ L14 Tenancy Access Door": "$6,600.00",
         "PDNSW - 6PSQ 5.N.8 FCR1 Card Reader Fault": "$460.00"}


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ops.db")
        stale = os.path.join(self.dir, "stale.csv")
        with open(CORRECTED, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        cols = list(rows[0].keys())
        for r in rows:
            name = (r["Project"] or "").strip()
            if name in WRONG:
                r["Invoiced Prior"] = WRONG[name]
                r["Contract Value FY27"] = "$0.00"
            r["Project Lead"] = ""
        with open(stale, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        db = Db(self.path, MIGRATIONS)
        db.migrate()
        db.close()
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys=ON")
        ir.load(conn, ir.validate(ir.read_rows(stale)))
        conn.commit()
        conn.close()
        self.db = Db(self.path, MIGRATIONS)
        self.db.upsert_user("s1", "r@x", "R")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_sync(self, *extra):
        return sr.main(["--csv", CORRECTED, "--db", self.path, *extra])

    def opening_for(self, name):
        return self.db.scalar(
            """SELECT COALESCE(SUM(cl.amount_cents), 0) FROM claim_line cl
               JOIN project p ON p.id = cl.project_id
               WHERE p.name = ? AND cl.is_opening_balance = 1""", (name,))


class TestDryRun(Case):
    def test_it_writes_nothing_without_apply(self):
        before = self.opening_for("PDNSW - RGB Renovation Works")
        self.assertEqual(self.run_sync(), 0)
        self.assertEqual(self.opening_for("PDNSW - RGB Renovation Works"), before)

    def test_it_finds_the_four_wrong_opening_balances(self):
        register = sr.read_register(CORRECTED)
        _text, opening, _missing, _ret, _d = sr.plan(self.db, register)
        self.assertEqual({name for _id, name, _o, _n in opening}, set(WRONG))
        self.assertTrue(all(new == 0 for _id, _n, _o, new in opening))

    def test_it_reports_missing_projects_but_does_not_create_them(self):
        """A project needs a job-code decision (ADR-28), so creating one
        silently would be the platform inventing a number."""
        # A project with no opening balance, or the immutability trigger
        # refuses the tidy-up -- which is itself the guarantee working.
        victim = self.db.query_one(
            """SELECT p.id, p.name FROM project p
               WHERE NOT EXISTS (SELECT 1 FROM claim_line cl
                                 WHERE cl.project_id = p.id
                                   AND cl.is_opening_balance = 1)
               LIMIT 1""")
        with self.db._tx() as c:
            for table in ("job_code_issue", "job_code_alias", "claim_line",
                          "customer_po"):
                c.execute(f"DELETE FROM {table} WHERE project_id = ?",
                          (victim["id"],))
            c.execute("DELETE FROM project WHERE id = ?", (victim["id"],))
        register = sr.read_register(CORRECTED)
        _t, _o, missing, _ret, _d = sr.plan(self.db, register)
        self.assertIn(victim["name"], missing)
        self.run_sync("--apply", "--reason", "x")
        self.assertIsNone(self.db.query_one(
            "SELECT id FROM project WHERE name = ?", (victim["name"],)))


class TestCorrectingOpeningBalances(Case):
    def test_a_reason_is_mandatory(self):
        """A migration artifact is being overwritten; the next reader needs
        to know why."""
        self.assertEqual(self.run_sync("--apply"), 2)
        self.assertEqual(self.opening_for("PDNSW - RGB Renovation Works"), 2200000)

    def test_with_a_reason_the_correction_lands(self):
        self.assertEqual(
            self.run_sync("--apply", "--reason", "corrected at source"), 0)
        for name in WRONG:
            self.assertEqual(self.opening_for(name), 0, name)

    def test_the_legacy_column_moves_too(self):
        """The previous release still reads it until the contraction
        migration (§4)."""
        self.run_sync("--apply", "--reason", "corrected at source")
        self.assertEqual(self.db.scalar(
            "SELECT invoiced_prior_cents FROM project WHERE name = ?",
            ("PDNSW - RGB Renovation Works",)), 0)

    def test_the_immutability_triggers_are_restored(self):
        """They are stood down for exactly as long as the correction takes.
        Leaving them off would quietly remove the guarantee."""
        self.run_sync("--apply", "--reason", "corrected at source")
        for name in Db.OPENING_TRIGGERS:
            self.assertEqual(self.db.scalar(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = ?", (name,)), 1)
        row = self.db.query_one(
            "SELECT id FROM claim_line WHERE is_opening_balance = 1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db._write.execute(
                "UPDATE claim_line SET amount_cents = 1 WHERE id = ?",
                (row["id"],))

    def test_the_triggers_come_back_even_if_a_correction_fails(self):
        """Otherwise one exception leaves the guarantee switched off."""
        register = sr.read_register(CORRECTED)
        _t, opening, _m, _ret, _d = sr.plan(self.db, register)
        broken = [(999999, "no such project", 100, 200)] + opening
        try:
            sr.apply_opening(self.db, broken, "x", None)
        except Exception:
            pass
        for name in Db.OPENING_TRIGGERS:
            self.assertEqual(self.db.scalar(
                "SELECT COUNT(*) FROM sqlite_master WHERE name = ?", (name,)), 1)

    def test_every_correction_is_audited_with_its_reason(self):
        self.run_sync("--apply", "--reason", "Jul-26 invoicing is FY27")
        rows = self.db.query(
            "SELECT detail FROM audit_log WHERE action='opening_balance_correct'")
        self.assertEqual(len(rows), 4)
        self.assertTrue(all("Jul-26 invoicing is FY27" in r["detail"] for r in rows))

    def test_running_it_twice_finds_nothing_the_second_time(self):
        self.run_sync("--apply", "--reason", "x")
        register = sr.read_register(CORRECTED)
        _t, opening, _m, _ret, _d = sr.plan(self.db, register)
        self.assertEqual(opening, [])


class TestRetentionTerms(Case):
    """The register carries one number -- the cap. The rest is the standard
    agreement: 10% per claim, half released at PC and half at DLP end."""

    def test_the_percentage_becomes_basis_points(self):
        self.assertEqual(sr.retention_cap_bp("5.00%"), 500)
        self.assertEqual(sr.retention_cap_bp("2.50%"), 250)
        self.assertEqual(sr.retention_cap_bp("10%"), 1000)
        self.assertEqual(sr.retention_cap_bp("0.00%"), 0)
        self.assertEqual(sr.retention_cap_bp(""), 0)
        self.assertEqual(sr.retention_cap_bp(None), 0)

    def test_retention_on_prior_invoicing_is_counted(self):
        """Those invoices were issued and the customer held retention
        against them. On three of the seven the cap was reached before the
        platform's window opened -- leaving it at zero would report $82,240
        of held money as not held."""
        self.run_sync("--apply", "--reason", "x")
        held = self.db.scalar(
            """SELECT SUM(cl.retention_cents) FROM claim_line cl
               WHERE cl.is_opening_balance = 1""")
        self.assertEqual(held, 8224036)          # $82,240.36

    def test_it_is_capped_where_the_project_was_fully_invoiced(self):
        self.run_sync("--apply", "--reason", "x")
        row = self.db.query_one(
            """SELECT r.* FROM v_po_retention_position r
               JOIN project p ON p.id = r.project_id
               WHERE p.name = 'Brennan Pl - ICN'""")
        self.assertEqual(row["withheld_cents"], row["cap_cents"])
        self.assertEqual(row["remaining_to_withhold_cents"], 0)

    def test_the_derivation_is_audited_as_a_derivation(self):
        """The workbook never recorded what was actually withheld, so this
        is an inference and has to read as one."""
        self.run_sync("--apply", "--reason", "x")
        row = self.db.query_one(
            "SELECT detail FROM audit_log WHERE action='retention_on_opening'")
        self.assertIn("derived", row["detail"])
        self.assertIn("not recorded in the workbook", row["detail"])

    def test_dates_are_read_in_either_order(self):
        self.assertEqual(sr.normalise_date("31/03/2027"), "2027-03-31")
        self.assertEqual(sr.normalise_date("2027-03-31"), "2027-03-31")
        self.assertEqual(sr.normalise_date("1/7/2027"), "2027-07-01")
        self.assertIsNone(sr.normalise_date("next March"))
        self.assertIsNone(sr.normalise_date(""))

    def test_it_finds_the_projects_carrying_retention(self):
        register = sr.read_register(CORRECTED)
        _t, _o, _m, retention, _d = sr.plan(self.db, register)
        names = {name for _id, name, _o2, new in retention if new}
        self.assertIn("Brennan Pl - ICN", names)
        self.assertTrue(all(new == 500 for _i, _n, _o2, new in retention if new))

    def test_applying_sets_cap_rate_and_split(self):
        self.run_sync("--apply", "--reason", "x")
        row = self.db.query_one(
            """SELECT po.* FROM customer_po po JOIN project p ON p.id = po.project_id
               WHERE p.name = 'Brennan Pl - ICN'""")
        self.assertEqual(row["retention_applies"], 1)
        self.assertEqual(row["retention_cap_bp"], 500)
        self.assertEqual(row["retention_rate_bp"], 1000)
        self.assertEqual(row["release_policy"], "split")
        self.assertEqual(row["release_split_bp"], 5000)

    def test_projects_without_retention_are_left_at_zero(self):
        self.run_sync("--apply", "--reason", "x")
        self.assertEqual(self.db.scalar(
            """SELECT COUNT(*) FROM customer_po po JOIN project p ON p.id = po.project_id
               WHERE p.name = '116 Cremorne St - ICN' AND po.retention_applies = 1"""), 0)

    def test_running_it_twice_finds_nothing_the_second_time(self):
        self.run_sync("--apply", "--reason", "x")
        register = sr.read_register(CORRECTED)
        _t, _o, _m, retention, _d = sr.plan(self.db, register)
        self.assertEqual(retention, [])


class TestTextFields(Case):
    def test_project_leads_are_filled_in(self):
        self.run_sync("--apply", "--reason", "x")
        self.assertEqual(self.db.scalar(
            "SELECT project_lead FROM project WHERE name = ?",
            ("50 Queens Rd - ICN",)), "Justin Anders")

    def test_it_does_not_blank_a_value_the_register_leaves_empty(self):
        """The workbook having nothing in a cell is not an instruction to
        erase what the platform knows."""
        target = self.db.query_one("SELECT name FROM project LIMIT 1")["name"]
        with self.db._tx() as c:
            c.execute("UPDATE project SET project_lead = 'Someone' WHERE name = ?",
                      (target,))
        stale = os.path.join(self.dir, "blank.csv")
        with open(CORRECTED, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        cols = list(rows[0].keys())
        for r in rows:
            if r["Project"].strip() == target:
                r["Project Lead"] = ""
        with open(stale, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        sr.main(["--csv", stale, "--db", self.path, "--apply", "--reason", "x"])
        self.assertEqual(self.db.scalar(
            "SELECT project_lead FROM project WHERE name = ?", (target,)),
            "Someone")


if __name__ == "__main__":
    unittest.main(verbosity=2)
