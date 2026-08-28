"""tools/import_suppliers.py — the iTrade supplier list.

Re-runnable and never destructive: matching is on the iTrade `ID#`, so a
second export updates what changed and adds what is new. A name missing
from a later file has been retired in iTrade, not erased from history, and
purchase orders may still reference it.
"""

import csv
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import import_suppliers as isup  # noqa: E402
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.join(ROOT, "ops", "migrations")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "suppliers.csv")


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ops.db")
        self.db = Db(self.path, MIGRATIONS)
        self.db.migrate()
        self.user = self.db.upsert_user("s1", "r@x", "R")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_import(self, *extra, csv_path=FIXTURE):
        return isup.main(["--db", self.path, "--csv", csv_path, *extra])

    def count(self, where="1=1", params=()):
        return self.db.scalar(f"SELECT COUNT(*) FROM supplier WHERE {where}",
                              params)

    def write_csv(self, rows):
        path = os.path.join(self.dir, "suppliers.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ID#", "Name", "Phone", "Contact", "Mobile", "Email"])
            w.writerows(rows)
        return path


class TestTheRealList(Case):
    def test_a_dry_run_writes_nothing(self):
        self.assertEqual(self.run_import(), 0)
        self.assertEqual(self.count(), 0)

    def test_it_imports_every_supplier(self):
        self.run_import("--apply")
        self.assertEqual(self.count(), 92)

    def test_the_itrade_reference_is_kept(self):
        """It means something outside this system, which is why it is kept
        rather than replaced by the platform's own id."""
        self.run_import("--apply")
        self.assertEqual(self.db.scalar(
            "SELECT itrade_ref FROM supplier WHERE name = 'Kodelabs'"), "29")

    def test_the_two_usd_suppliers_default_to_usd(self):
        self.run_import("--apply")
        names = sorted(r["name"] for r in self.db.query(
            "SELECT name FROM supplier WHERE default_currency = 'USD'"))
        self.assertEqual(names, ["Jinan USR IOT Technology Limited",
                                 "Kodelabs"])

    def test_everything_else_defaults_to_aud(self):
        self.run_import("--apply")
        self.assertEqual(self.count("default_currency = 'AUD'"), 90)

    def test_placeholder_emails_import_as_nothing(self):
        """39 rows carry `mailto:`. An empty column is honest; a string that
        looks like an address is not."""
        self.run_import("--apply")
        self.assertEqual(self.count("email IS NOT NULL"), 53)
        self.assertEqual(self.count("email = 'mailto:'"), 0)

    def test_both_phone_columns_are_kept_as_they_stand(self):
        """Landlines sit in `Mobile` on nine rows. Guessing which is which
        would replace a number that is merely mislabelled with one that is
        wrong."""
        self.run_import("--apply")
        row = self.db.query_one(
            "SELECT phone FROM supplier WHERE name = 'AAA Door Closers Pty Ltd'")
        self.assertEqual(row["phone"], "03-9708 2337")

    def test_nothing_arrives_with_an_abn_yet(self):
        """It is being gathered. The column exists now so that withholding
        is not retrofitted when somebody notices it."""
        self.run_import("--apply")
        self.assertEqual(self.count("abn IS NULL"), 92)


class TestRunningItAgain(Case):
    def test_a_second_run_changes_nothing(self):
        self.run_import("--apply")
        self.run_import("--apply")
        self.assertEqual(self.count(), 92)

    def test_a_changed_contact_is_updated_and_recorded(self):
        self.run_import("--apply")
        path = self.write_csv([[29, "Kodelabs", "", "Sam", "", "sam@kodelabs.com"]])
        self.run_import("--apply", csv_path=path)
        row = self.db.query_one("SELECT * FROM supplier WHERE name = 'Kodelabs'")
        self.assertEqual(row["contact_name"], "Sam")
        self.assertEqual(self.db.scalar(
            """SELECT COUNT(*) FROM supplier_revision
               WHERE supplier_id = ? AND field = 'contact_name'""",
            (row["id"],)), 1)

    def test_a_supplier_missing_from_a_later_file_is_kept(self):
        """Retired in iTrade, not erased from history — and a purchase
        order may still reference it."""
        self.run_import("--apply")
        path = self.write_csv([[29, "Kodelabs", "", "", "", ""]])
        self.run_import("--apply", csv_path=path)
        self.assertEqual(self.count(), 92)

    def test_a_new_supplier_is_added(self):
        self.run_import("--apply")
        path = self.write_csv([[999, "Brand New Pty Ltd", "", "", "", "a@b.com"]])
        self.run_import("--apply", csv_path=path)
        self.assertEqual(self.count(), 93)

    def test_a_hand_set_currency_is_not_reverted(self):
        """`USD_SUPPLIERS` says which the form offers first, not what the
        supplier is. Switching one over by hand must survive the next
        import."""
        self.run_import("--apply")
        row = self.db.query_one("SELECT id FROM supplier WHERE name = 'Anixter'")
        self.db.update_suppliers([(row["id"], {"default_currency": "USD"})],
                                 self.user["id"])
        self.run_import("--apply")
        self.assertEqual(self.db.scalar(
            "SELECT default_currency FROM supplier WHERE id = ?", (row["id"],)),
            "USD")

    def test_a_renamed_supplier_matches_on_its_itrade_id(self):
        self.run_import("--apply")
        path = self.write_csv([[29, "Kodelabs Inc", "", "", "", ""]])
        self.run_import("--apply", csv_path=path)
        self.assertEqual(self.count(), 92)
        self.assertEqual(self.db.scalar(
            "SELECT name FROM supplier WHERE itrade_ref = '29'"), "Kodelabs Inc")


class TestRefusals(Case):
    def test_a_row_with_no_name_is_skipped_and_listed(self):
        path = self.write_csv([[1, "", "", "", "", ""],
                               [2, "Real Supplier", "", "", "", ""]])
        self.run_import("--apply", csv_path=path)
        self.assertEqual(self.count(), 1)

    def test_two_rows_for_one_name_cannot_both_land(self):
        """Two rows for one company is how spend gets split in half without
        anyone noticing."""
        import sqlite3
        path = self.write_csv([[1, "Same Name", "", "", "", ""],
                               [2, "same name", "", "", "", ""]])
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_import("--apply", csv_path=path)

    def test_a_missing_file_names_itself(self):
        with self.assertRaises(SystemExit) as e:
            isup.main(["--db", self.path, "--csv", "/nope/suppliers.csv"])
        self.assertIn("no such file", str(e.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
