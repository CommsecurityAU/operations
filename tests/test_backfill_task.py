"""tools/backfill_task.py — filling in a column the importer never mapped.

`Task` is the workbook's LINE ITEM. It was folded into `detail` and the
column left empty, so the claim plan had nothing to group on. Fixing the
importer only helps a fresh import; this fills in what is already there.
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
import backfill_task as bt  # noqa: E402
import import_register as ir  # noqa: E402
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.join(ROOT, "ops", "migrations")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")


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

    def claim(self, project, month, cents, detail=None, task=None):
        pid = self.db.scalar("SELECT id FROM project WHERE name = ?", (project,))
        po = self.db.scalar(
            "SELECT id FROM customer_po WHERE project_id = ? LIMIT 1", (pid,))
        period = self.db.scalar("SELECT id FROM period WHERE label = ?", (month,))
        with self.db._tx() as c:
            c.execute(
                """INSERT INTO claim_line (entity_id, project_id, customer_po_id,
                       period_id, status, amount_cents, detail, task, created_ts)
                   VALUES (1,?,?,?, 'forecast', ?,?,?,0)""",
                (pid, po, period, cents, detail, task))

    def run_backfill(self, *extra):
        return bt.main(["--db", self.path,
                        "--invoicing", os.path.join(FIXTURES, "invoicing_fy27.csv"),
                        "--future", os.path.join(FIXTURES, "future_invoicing_fy27.csv"),
                        *extra])

    def tasks(self):
        return self.db.scalar(
            "SELECT COUNT(*) FROM claim_line WHERE task IS NOT NULL")


class TestBackfill(Case):
    def test_a_dry_run_writes_nothing(self):
        self.claim("200 Victoria - IBP", "Sep-26", 1770000,
                   detail="Client Training")
        self.assertEqual(self.run_backfill(), 0)
        self.assertEqual(self.tasks(), 0)

    def test_it_matches_on_the_detail_the_importer_wrote(self):
        """Where the task was folded into `detail`, the pairing is certain
        rather than positional."""
        self.claim("200 Victoria - IBP", "Sep-26", 1770000,
                   detail="Client Training")
        self.run_backfill("--apply")
        self.assertEqual(self.db.scalar(
            "SELECT task FROM claim_line WHERE detail = 'Client Training'"),
            "Client Training")

    def test_it_leaves_a_claim_that_already_has_one(self):
        self.claim("200 Victoria - IBP", "Sep-26", 1770000,
                   detail="Client Training", task="Something else")
        self.run_backfill("--apply")
        self.assertEqual(self.db.scalar(
            "SELECT task FROM claim_line WHERE detail = 'Client Training'"),
            "Something else")

    def test_a_claim_with_no_workbook_row_keeps_an_empty_task(self):
        """Better an empty column than a task borrowed from another row."""
        self.claim("200 Victoria - IBP", "Sep-26", 999999, detail="Invented")
        self.run_backfill("--apply")
        self.assertIsNone(self.db.scalar(
            "SELECT task FROM claim_line WHERE detail = 'Invented'"))

    def test_running_it_twice_changes_nothing(self):
        self.claim("200 Victoria - IBP", "Sep-26", 1770000,
                   detail="Client Training")
        self.run_backfill("--apply")
        before = self.tasks()
        self.run_backfill("--apply")
        self.assertEqual(self.tasks(), before)

    def test_positional_matches_are_reported_not_silent(self):
        """Several claims sharing a project, month and amount can only be
        paired in file order, which is an assumption."""
        for _ in range(2):
            self.claim("200 Victoria - IBP", "Sep-26", 1770000)
        rows = bt.read_rows(os.path.join(FIXTURES, "invoicing_fy27.csv"), "EOM")
        rows += bt.read_rows(
            os.path.join(FIXTURES, "future_invoicing_fy27.csv"), "EOM Cycle")
        _updates, guessed, _unmatched = bt.plan(self.db, rows)
        self.assertEqual(len(guessed), 2)

    def test_a_missing_file_names_itself(self):
        with self.assertRaises(SystemExit) as e:
            bt.main(["--db", self.path, "--invoicing", "/nope/a.csv",
                     "--future", "/nope/b.csv"])
        self.assertIn("no such file", str(e.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
