"""tools/restore.py -- pre-flight verification and restore ordering.

The negative tests carry the weight. A restore tool that only works on good
input is a tool that discovers a corrupt backup after deleting the live
database.
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
import restore as restore_mod  # noqa: E402
from ops import backup  # noqa: E402
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.join(ROOT, "ops", "migrations")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "project_register_fy27.csv")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.data = os.path.join(self.dir, "data")
        os.makedirs(self.data)
        self.db = Db(os.path.join(self.data, "ops.db"), MIGRATIONS)
        self.db.migrate()

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def with_register(self):
        self.db.close()
        conn = sqlite3.connect(os.path.join(self.data, "ops.db"))
        conn.execute("PRAGMA foreign_keys=ON")
        imp.load(conn, imp.validate(imp.read_rows(FIXTURE)))
        conn.commit()
        conn.close()
        self.db = Db(os.path.join(self.data, "ops.db"), MIGRATIONS)

    def snap(self):
        return backup.snapshot(self.db, os.path.join(self.dir, "offbox"))


class TestCheck(Base):
    def test_accepts_a_good_snapshot(self):
        f = restore_mod.check(self.snap())
        self.assertEqual(f["integrity"], "ok")
        self.assertEqual(f["counts"]["period"], 144)
        self.assertIn("001_foundation.sql", f["migrations"])

    def test_reports_the_register_and_that_it_balances(self):
        self.with_register()
        f = restore_mod.check(self.snap())
        self.assertEqual(f["register"]["projects"], 59)
        self.assertEqual(f["register"]["oih_cents"], restore_mod.REGISTER_OIH_CENTS)
        self.assertTrue(f["register"]["balances"])

    def test_missing_file(self):
        with self.assertRaises(restore_mod.RestoreError):
            restore_mod.check(os.path.join(self.dir, "nope.db"))

    def test_empty_file(self):
        p = os.path.join(self.dir, "empty.db")
        open(p, "wb").close()
        with self.assertRaises(restore_mod.RestoreError):
            restore_mod.check(p)

    def test_not_a_database(self):
        p = os.path.join(self.dir, "junk.db")
        with open(p, "wb") as f:
            f.write(b"this is not a sqlite file at all, not even close")
        with self.assertRaises(Exception):
            restore_mod.check(p)

    def test_truncated_snapshot_is_caught(self):
        """Half a file can still open; the table check is what catches it."""
        good = self.snap()
        bad = os.path.join(self.dir, "trunc.db")
        with open(good, "rb") as src, open(bad, "wb") as dst:
            dst.write(src.read(4096))
        with self.assertRaises(Exception):
            restore_mod.check(bad)

    def test_database_with_no_migrations_is_refused(self):
        p = os.path.join(self.dir, "bare.db")
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE x (a INTEGER) STRICT")
        c.commit()
        c.close()
        with self.assertRaises(restore_mod.RestoreError) as e:
            restore_mod.check(p)
        self.assertIn("missing tables", str(e.exception))

    def test_register_identity_is_actually_checked(self):
        """PO = Prior + OIH is arithmetic over the same two columns, so it
        cannot be broken by editing them -- OIH is derived. What the check
        DOES catch is a snapshot whose totals have moved from the validated
        figures, which is the realistic corruption: rows lost."""
        self.with_register()
        snap = self.snap()
        conn = sqlite3.connect(snap)
        conn.execute("DELETE FROM job_code_issue")
        conn.execute("DELETE FROM job_code_alias")
        conn.execute("DELETE FROM project WHERE id > 10")   # lose 49 rows
        conn.commit()
        conn.close()
        f = restore_mod.check(snap)
        self.assertEqual(f["register"]["projects"], 10)
        self.assertNotEqual(f["register"]["oih_cents"],
                            restore_mod.REGISTER_OIH_CENTS)
        # It still "balances" -- which is exactly why the report also prints
        # whether the figures match the validated set, and why a human reads
        # the pre-flight output before typing --force.
        self.assertTrue(f["register"]["balances"])
        self.assertNotIn("matches the 21 Aug 2026",
                         restore_mod.report(f))


class TestRestore(Base):
    def test_restores_into_an_empty_data_dir(self):
        self.with_register()
        snap = self.snap()
        target = os.path.join(self.dir, "restored")
        f, elapsed = restore_mod.restore(snap, target)
        self.assertTrue(os.path.exists(os.path.join(target, "ops.db")))
        self.assertEqual(f["register"]["oih_cents"], restore_mod.REGISTER_OIH_CENTS)
        self.assertLess(elapsed, 60)

    def test_refuses_to_overwrite_a_live_database_without_force(self):
        """Restoring DISCARDS every write since the snapshot. That has to be
        a decision, not an accident."""
        snap = self.snap()
        with self.assertRaises(restore_mod.RestoreError) as e:
            restore_mod.restore(snap, self.data)
        self.assertIn("DISCARDS", str(e.exception))

    def test_force_overwrites(self):
        snap = self.snap()
        self.db.close()
        f, _ = restore_mod.restore(snap, self.data, force=True)
        self.assertEqual(f["integrity"], "ok")
        self.db = Db(os.path.join(self.data, "ops.db"), MIGRATIONS)

    def test_verifies_before_destroying_anything(self):
        """Pre-flight runs first, so a bad snapshot leaves the live database
        untouched rather than deleted."""
        bad = os.path.join(self.dir, "bad.db")
        with open(bad, "wb") as f:
            f.write(b"not a database")
        before = self.db.scalar("SELECT COUNT(*) FROM period")
        with self.assertRaises(Exception):
            restore_mod.restore(bad, self.data, force=True)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM period"), before)

    def test_stale_wal_is_removed(self):
        """A -wal left from the OLD database would be replayed over the
        restored one, quietly corrupting it."""
        snap = self.snap()
        target = os.path.join(self.dir, "restored")
        os.makedirs(target)
        with open(os.path.join(target, "ops.db-wal"), "wb") as f:
            f.write(b"stale wal from a previous database")
        restore_mod.restore(snap, target)
        self.assertFalse(os.path.exists(os.path.join(target, "ops.db-wal")))

    def test_documents_are_restored_before_the_database(self):
        """A row pointing at a missing blob is a visible 404; a blob with no
        row is invisible and harmless. So blobs land first."""
        snap = self.snap()
        docs = os.path.join(self.dir, "offbox_docs", "ab")
        os.makedirs(docs)
        with open(os.path.join(docs, "deadbeef"), "wb") as f:
            f.write(b"a pdf")
        target = os.path.join(self.dir, "restored")
        restore_mod.restore(snap, target,
                            documents_src=os.path.join(self.dir, "offbox_docs"))
        self.assertTrue(os.path.exists(
            os.path.join(target, "documents", "ab", "deadbeef")))

    def test_restore_is_well_inside_the_60s_budget(self):
        self.with_register()
        snap = self.snap()
        _, elapsed = restore_mod.restore(snap, os.path.join(self.dir, "r2"))
        self.assertLess(elapsed, 10, "restore should be near-instant at this size")


class TestOffboxScriptExcludesTheLiveDatabase(unittest.TestCase):
    def test_script_never_copies_ops_db(self):
        """§12: a WAL database copied mid-transaction yields a .db and a -wal
        that disagree, and the copy fails only at restore."""
        path = os.path.join(ROOT, "tools", "offbox_sync.sh")
        with open(path, encoding="utf-8") as f:
            body = f.read()
        code = "\n".join(l for l in body.splitlines()
                         if not l.strip().startswith("#"))
        self.assertNotIn("$SRC/ops.db", code)
        self.assertIn('"$SRC/backups/"', code)

    def test_script_checks_snapshot_age(self):
        """A sync that succeeds while the app has stopped snapshotting looks
        healthy and is not."""
        with open(os.path.join(ROOT, "tools", "offbox_sync.sh"),
                  encoding="utf-8") as f:
            self.assertIn("age_h", f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
