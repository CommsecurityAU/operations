"""tools/drift_check.py -- ADR-27.

The tests that matter are the ones about what it does NOT report. A checker
that flags expected differences gets ignored within a fortnight, and an
ignored checker is worse than none because it looks like coverage.
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
import drift_check  # noqa: E402
import import_register as imp  # noqa: E402
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.join(ROOT, "ops", "migrations")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "project_register_fy27.csv")


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.dir, "ops.db")
        db = Db(self.db_path, MIGRATIONS)
        db.migrate()
        db.close()
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        imp.load(conn, imp.validate(imp.read_rows(FIXTURE)))
        conn.commit()
        conn.close()
        self.db = Db(self.db_path, MIGRATIONS)
        self.csv = os.path.join(self.dir, "register.csv")
        shutil.copy(FIXTURE, self.csv)

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_check(self):
        workbook = drift_check.read_workbook(self.csv)
        platform = drift_check.read_platform(self.db_path)
        findings = drift_check.compare(workbook, platform)
        _text, actionable = drift_check.report(findings, workbook, platform)
        return findings, actionable

    def edit_csv(self, project, column, value):
        with open(self.csv, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
            cols = rows[0].keys()
        for r in rows:
            if r["Project"].strip() == project:
                r[column] = value
        with open(self.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)


class TestNoDriftOnAFreshImport(Case):
    def test_the_workbook_it_was_imported_from_shows_no_drift(self):
        """The baseline. If importing then immediately checking reports
        anything, every later finding is untrustworthy."""
        findings, actionable = self.run_check()
        self.assertEqual(actionable, 0, findings)
        self.assertEqual(findings["field_drift"], [])
        self.assertEqual(findings["missing_in_platform"], [])
        self.assertEqual(findings["missing_in_workbook"], [])


class TestWhatItReports(Case):
    def test_a_changed_contract_value(self):
        self.edit_csv("116 Cremorne St - ICN", "Purchase Order", "$999,999.00")
        findings, actionable = self.run_check()
        self.assertEqual(actionable, 1)
        name, field, left, right = findings["field_drift"][0]
        self.assertEqual(name, "116 Cremorne St - ICN")
        self.assertEqual(field, "Purchase Order")
        self.assertIn("999,999", left)
        self.assertIn("214,000", right)

    def test_a_changed_status(self):
        self.edit_csv("116 Cremorne St - ICN", "Status", "Complete")
        findings, _n = self.run_check()
        self.assertIn("Status", [f[1] for f in findings["field_drift"]])

    def test_a_changed_lead(self):
        self.edit_csv("116 Cremorne St - ICN", "Project Lead", "Someone Else")
        findings, _n = self.run_check()
        self.assertIn("Project Lead", [f[1] for f in findings["field_drift"]])

    def test_a_row_added_to_the_workbook_only(self):
        with open(self.csv, "a", encoding="utf-8") as f:
            f.write("Brand New Site,Hines,JN-9001,,ICN,Active,R,"
                    "$1000.00,$0.00,$1000.00,,\n")
        findings, _n = self.run_check()
        self.assertIn("Brand New Site", findings["missing_in_platform"])

    def test_a_project_created_in_the_platform_only(self):
        """Creating in the platform is now normal, so this is information
        rather than an error -- but it still has to appear, or the workbook
        quietly falls behind."""
        client = self.db.scalar("SELECT id FROM client LIMIT 1")
        type_id = self.db.scalar("SELECT id FROM project_type LIMIT 1")
        # Creation no longer allocates (ADR-28): supply the code, as the UI
        # now does.
        self.db.create_project(
            {"entity_id": 1, "name": "Platform Only", "client_id": client,
             "type_id": type_id, "status": "Active", "project_lead": "R",
             "job_code": "JN-9001",
             "purchase_order_cents": 0, "invoiced_prior_cents": 0}, None)
        findings, _n = self.run_check()
        self.assertIn("Platform Only", findings["missing_in_workbook"])


class TestWhatItDoesNotReport(Case):
    def test_a_platform_issued_job_number_is_not_drift(self):
        """After the worklist turns TBA into JN-6889 the workbook is stale on
        a field the platform owns. Reporting that would bury the real
        findings under expected ones, which is how a check gets ignored."""
        user = self.db.upsert_user("s1", "r@x", "R")
        self.db.reserve_job_number_range(9000, 9999, "test", user["id"])
        issue = self.db.query_one(
            "SELECT id FROM job_code_issue WHERE raw_code = 'TBA' LIMIT 1")
        result = self.db.resolve_issue(issue["id"], "issue", user["id"])
        findings, actionable = self.run_check()
        self.assertEqual(actionable, 0, findings["field_drift"])
        self.assertEqual(len(findings["workbook_stale"]), 1)
        name, field, was, now = findings["workbook_stale"][0]
        self.assertEqual(field, "job_code")
        self.assertEqual(was, "TBA")
        self.assertEqual(now, result["job_code"])

    def test_a_genuinely_changed_job_code_IS_drift(self):
        """The stale-workbook exemption applies only to placeholders. If the
        workbook says JN-676 and the platform says something else, that is a
        real difference someone has to decide about."""
        self.edit_csv("116 Cremorne St - ICN", "Job Code", "JN-1234")
        findings, actionable = self.run_check()
        self.assertEqual(actionable, 1)
        self.assertEqual(findings["field_drift"][0][1], "job_code")

    def test_case_and_spacing_differences_are_not_drift(self):
        """'Active ' and 'active' are the same status. Flagging them trains
        people to skim the report."""
        self.edit_csv("116 Cremorne St - ICN", "Status", "  active ")
        _findings, actionable = self.run_check()
        self.assertEqual(actionable, 0)

    def test_money_formatting_differences_are_not_drift(self):
        """$214,000 and $214,000.00 are the same amount."""
        self.edit_csv("116 Cremorne St - ICN", "Purchase Order", "214000")
        _findings, actionable = self.run_check()
        self.assertEqual(actionable, 0)


class TestItNeverWrites(Case):
    def test_the_database_is_opened_read_only(self):
        """A checker that repairs what it finds is a second, unreviewed
        import path.

        The SQL is inspected through `ast`, not by searching the file text.
        Uppercasing the source to look for "INSERT" matches `sys.path.insert`
        -- the third time in this codebase that a naive string search has
        produced a false failure, after `innerHTML` in a comment and
        `round()` in a docstring. Parse the code; do not read it as prose.
        """
        import ast
        path = os.path.join(ROOT, "tools", "drift_check.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("mode=ro", source)

        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                head = node.value.strip().upper()
                for verb in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER "):
                    if head.startswith(verb):
                        offenders.append(f"{verb.strip()} at line {node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr == "commit":
                offenders.append(f"commit() at line {node.lineno}")
        self.assertEqual(offenders, [])

    def test_running_it_changes_nothing(self):
        before = self.db.scalar("SELECT COUNT(*) FROM project")
        stamp = os.path.getmtime(self.db_path)
        self.run_check()
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM project"), before)
        self.assertEqual(os.path.getmtime(self.db_path), stamp)


class TestExitCode(Case):
    def test_zero_when_clean_and_one_when_not(self):
        self.assertEqual(
            drift_check.main(["--csv", self.csv, "--db", self.db_path]), 0)
        self.edit_csv("116 Cremorne St - ICN", "Status", "Complete")
        self.assertEqual(
            drift_check.main(["--csv", self.csv, "--db", self.db_path]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
