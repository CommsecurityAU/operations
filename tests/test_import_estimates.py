"""tools/import_estimates.py — the orange-flagged expense forecast.

Early estimates of future procurement: 31 cells, $1,576,928.29, which is
ten times the value of the real orders. Mixing the two would make committed
cost meaningless, so an estimate carries a flag and every view separates
them.
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
import import_estimates as ie  # noqa: E402
import import_register as ir  # noqa: E402
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.join(ROOT, "ops", "migrations")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
ORANGE = os.path.join(FIXTURES, "pe_orange.csv")


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ops.db")
        db = Db(self.path, MIGRATIONS)
        db.migrate()
        db.close()
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA foreign_keys=ON")
        ir.load(conn, ir.validate(ir.read_rows(
            os.path.join(FIXTURES, "project_register_fy27.csv"))))
        conn.commit()
        conn.close()
        self.db = Db(self.path, MIGRATIONS)
        self.user = self.db.upsert_user("s1", "r@x", "R")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_import(self, *extra, csv_path=ORANGE):
        return ie.main(["--db", self.path, "--csv", csv_path, *extra])

    def count(self, where="1=1"):
        return self.db.scalar(
            f"SELECT COUNT(*) FROM procurement_line WHERE {where}")


class TestTheRealFlags(Case):
    def test_a_dry_run_writes_nothing(self):
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.count(), 0)

    def test_it_places_what_it_can(self):
        """Two flags name `PDNSW - 6PSQ L13 Tenancy Access Door`, which is
        not in the register. Those are listed and left; creating a project
        needs a job-code decision (ADR-28)."""
        self.run_import("--apply")
        self.assertEqual(self.count(), 29)

    def test_every_line_is_flagged_as_an_estimate(self):
        self.run_import("--apply")
        self.assertEqual(self.count("is_estimate = 1"), 29)

    def test_the_total_matches_the_matrix(self):
        self.run_import("--apply")
        self.assertEqual(self.db.scalar(
            "SELECT SUM(total_cents) FROM procurement_line"), 156787700)

    def test_an_estimate_has_no_supplier_quote_or_order(self):
        """Nothing has been quoted, approved or committed. A supplier on it
        would say otherwise."""
        self.run_import("--apply")
        self.assertEqual(self.count(
            "is_estimate = 1 AND (supplier_id IS NOT NULL "
            "OR supplier_quote_id IS NOT NULL OR supplier_po_id IS NOT NULL)"),
            0)

    def test_each_lands_in_the_month_the_matrix_gave_it(self):
        self.run_import("--apply")
        self.assertEqual(self.db.scalar(
            """SELECT SUM(l.total_cents) FROM procurement_line l
               JOIN period pe ON pe.id = l.period_id
               WHERE pe.label = 'May-27'"""), 38990000)

    def test_it_reads_as_not_yet_ordered(self):
        self.run_import("--apply")
        self.assertEqual(self.db.scalar(
            "SELECT DISTINCT state FROM v_procurement_line"), "to be ordered")

    def test_running_it_twice_is_refused(self):
        """The estimates it made are meant to be replaced in place, not
        re-imported over."""
        self.run_import("--apply")
        self.assertEqual(self.run_import("--apply"), 2)
        self.assertEqual(self.count(), 29)


class TestEstimatesAreNotCommitments(Case):
    """$1.57m of estimates in the same total as $160k of real orders would
    make committed cost wrong by a factor of ten."""

    def real_line(self, cents):
        project = self.db.scalar("SELECT id FROM project LIMIT 1")
        po = self.db.scalar(
            "SELECT id FROM customer_po WHERE project_id = ? LIMIT 1", (project,))
        self.assertIsNotNone(po)
        return self.db.create_procurement_line(
            {"entity_id": 1, "project_id": project, "quantity": 1,
             "unit_cost_cents": cents, "total_cents": cents}, self.user["id"])

    def test_committed_excludes_estimates(self):
        self.run_import("--apply")
        self.real_line(100000)
        row = self.db.query_one(
            """SELECT SUM(committed_cents) c, SUM(estimated_cents) e,
                      SUM(forecast_cents) f FROM v_project_procurement""")
        self.assertEqual(row["c"], 100000)
        self.assertEqual(row["e"], 156787700)
        self.assertEqual(row["f"], 156887700)

    def test_an_estimate_is_not_outstanding_or_undelivered(self):
        """Nothing is owed on something nobody has ordered."""
        self.run_import("--apply")
        row = self.db.query_one(
            """SELECT SUM(outstanding_cents) o, SUM(undelivered_cents) u
               FROM v_project_procurement""")
        self.assertEqual(row["o"], 0)
        self.assertEqual(row["u"], 0)

    def test_an_estimate_becomes_real_in_place(self):
        """Replaced, not deleted: the month keeps its forecast while
        somebody types the real figure."""
        self.run_import("--apply")
        line = self.db.query_one(
            "SELECT id, period_id, total_cents FROM procurement_line LIMIT 1")
        before = self.db.scalar(
            """SELECT SUM(forecast_cents) FROM v_project_procurement""")
        self.db.update_procurement_line(
            line["id"], {"is_estimate": 0, "unit_cost_cents": line["total_cents"],
                         "total_cents": line["total_cents"]},
            self.user["id"], "quoted")
        after = self.db.query_one(
            """SELECT SUM(committed_cents) c, SUM(forecast_cents) f
               FROM v_project_procurement""")
        self.assertEqual(after["f"], before)
        self.assertEqual(after["c"], line["total_cents"])


class TestRefusals(Case):
    def write(self, rows):
        path = os.path.join(self.dir, "flags.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["Project", "Job Code", "Type", "EOM", "Amount", "Matched"])
            w.writerows(rows)
        return path

    def test_an_unreadable_amount_is_listed_not_guessed(self):
        path = self.write([["194 Pitt St - ICN", "JN-5261", "ICN", "Dec-26",
                            "about twenty thousand", "font"]])
        self.assertEqual(self.run_import("--apply", csv_path=path), 1)
        self.assertEqual(self.count(), 0)

    def test_an_unknown_month_is_listed(self):
        path = self.write([["194 Pitt St - ICN", "JN-5261", "ICN", "Dec-99",
                            "25000", "font"]])
        self.assertEqual(self.run_import("--apply", csv_path=path), 1)

    def test_the_job_code_is_preferred_over_the_name(self):
        """It is what identifies a project to everyone outside this
        system."""
        path = self.write([["Whatever Someone Typed", "JN-5261", "ICN",
                            "Dec-26", "25000", "font"]])
        self.run_import("--apply", csv_path=path)
        self.assertEqual(self.db.scalar(
            """SELECT p.job_code FROM procurement_line l
               JOIN project p ON p.id = l.project_id"""), "JN-5261")

    def test_a_placeholder_job_code_falls_back_to_the_name(self):
        """`TBA` and `#N/A` are not codes."""
        path = self.write([["130 Little Collins - ICN Maintenance", "TBA",
                            "ICN", "Jun-27", "6012.13", "font"]])
        self.run_import("--apply", csv_path=path)
        self.assertEqual(self.count(), 1)

    def test_a_missing_file_names_itself(self):
        with self.assertRaises(SystemExit) as e:
            ie.main(["--db", self.path, "--csv", "/nope/flags.csv"])
        self.assertIn("no such file", str(e.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
