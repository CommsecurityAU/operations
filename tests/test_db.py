"""ops.db -- connection model, migration runner, health check.

The health-check tests are the important ones. `applied >= expected` rather
than equality is what stops an auto-rollback loop, and that failure mode is
invisible until a real deploy goes wrong, so it is pinned here.
"""

import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from ops.db import Db, MigrationError, rows  # noqa: E402

MIGRATIONS = os.path.join(ROOT, "ops", "migrations")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ops.db")
        self.db = Db(self.path, MIGRATIONS)
        self.db.migrate()

    def tearDown(self):
        self.db.close()
        for f in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, f))
        os.rmdir(self.dir)


class TestConnections(Base):
    def test_write_connection_pragmas(self):
        c = self.db._write
        self.assertEqual(c.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(c.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(c.execute("PRAGMA synchronous").fetchone()[0], 2)  # FULL

    def test_read_connection_cannot_write(self):
        """query_only=ON plus mode=ro: a stray write through a read path
        fails loudly instead of quietly succeeding."""
        with self.assertRaises(sqlite3.OperationalError):
            self.db._read.execute("INSERT INTO entity (code, name) VALUES ('X','x')")

    def test_reads_do_not_block_on_the_write_lock(self):
        """WAL's whole point. If reads took the write lock, a slow read would
        stall every other request (ADR-16)."""
        self.db._read.execute("SELECT 1").fetchone()  # warm this thread's conn
        with self.db._tx():
            result = self.db.query("SELECT COUNT(*) AS n FROM entity")
        self.assertEqual(result[0]["n"], 3)

    def test_each_thread_gets_its_own_read_connection(self):
        seen = {}

        def grab(name):
            self.db._read.execute("SELECT 1").fetchone()
            seen[name] = id(self.db._read)

        self.db._read.execute("SELECT 1")
        main = id(self.db._read)
        ts = [threading.Thread(target=grab, args=(f"t{i}",)) for i in range(3)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(set(seen.values())), 3)
        self.assertNotIn(main, seen.values())

    def test_rollback_on_exception(self):
        with self.assertRaises(ValueError):
            with self.db._tx() as c:
                c.execute("INSERT INTO entity (code, name) VALUES ('X','x')")
                raise ValueError("boom")
        self.assertIsNone(
            self.db.query_one("SELECT id FROM entity WHERE code='X'"))

    def test_lock_is_released_after_a_failed_transaction(self):
        """A lock leaked on the error path deadlocks the process on the next
        write -- and only under failure, so nothing would catch it in dev."""
        try:
            with self.db._tx():
                raise ValueError()
        except ValueError:
            pass
        self.assertTrue(self.db._lock.acquire(timeout=2))
        self.db._lock.release()

    def test_read_modify_write_is_serialised(self):
        """The lock's actual job, and the only shape that proves it.

        SQLite's own mutex already makes a SINGLE statement atomic, so a test
        built from single statements passes with the lock removed -- it is
        testing SQLite, not us. A lost update needs a read and a write in the
        same transaction with a gap between them, which is exactly what a
        real Db method does.
        """
        with self.db._tx() as c:
            c.execute("INSERT INTO client (entity_id, name) VALUES (1,'0')")

        errors = []

        def bump():
            for _ in range(10):
                try:
                    with self.db._tx() as c:
                        v = int(c.execute(
                            "SELECT name FROM client WHERE id=1").fetchone()[0])
                        time.sleep(0.001)  # widen the window
                        c.execute("UPDATE client SET name=? WHERE id=1", (str(v + 1),))
                except Exception as e:      # unlocked: nested BEGIN also raises
                    errors.append(e)

        ts = [threading.Thread(target=bump) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [], "transactions collided")
        self.assertEqual(
            int(self.db.scalar("SELECT name FROM client WHERE id=1")), 40,
            "lost update: the write lock is not serialising transactions")


class TestMigrations(Base):
    def test_applied_once_and_recorded(self):
        first = self.db.migrate()
        self.assertEqual(first, [])  # already applied in setUp
        versions = [r["version"] for r in
                    self.db.query("SELECT version FROM schema_migrations")]
        self.assertIn("001_foundation.sql", versions)

    def test_is_idempotent(self):
        before = self.db.scalar("SELECT COUNT(*) FROM period")
        self.db.migrate()
        self.db.migrate()
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM period"), before)

    def test_failure_rolls_back_completely(self):
        """Python does NOT roll back a failed executescript -- it leaves the
        transaction open and the partial work in place. Without the explicit
        rollback in the runner this leaves a half-applied schema."""
        bad = os.path.join(MIGRATIONS, "999_bad.sql")
        with open(bad, "w") as f:
            f.write("CREATE TABLE good_table (x INTEGER) STRICT;\n"
                    "CREATE TABLE oops (x NOT_A_TYPE) STRICT;\n")
        try:
            with self.assertRaises(MigrationError):
                self.db.migrate()
            self.assertIsNone(self.db.query_one(
                "SELECT name FROM sqlite_master WHERE name='good_table'"))
            self.assertNotIn("999_bad.sql", [
                r["version"] for r in
                self.db.query("SELECT version FROM schema_migrations")])
        finally:
            os.unlink(bad)


class TestHealth(Base):
    def test_healthy_after_migrate(self):
        h = self.db.health()
        self.assertTrue(h["ok"])
        self.assertEqual(h["integrity"], "ok")
        self.assertEqual(h["schema"]["missing"], [])

    def test_unhealthy_when_a_migration_is_missing(self):
        with self.db._tx() as c:
            c.execute("DELETE FROM schema_migrations")
        h = self.db.health()
        self.assertFalse(h["ok"])
        self.assertEqual(h["schema"]["missing"], ["001_foundation.sql"])

    def test_HEALTHY_when_schema_is_AHEAD_of_the_binary(self):
        """The rollback-loop guard, and the reason this is >= not ==.

        A release migrates to 002, fails its health gate for some unrelated
        reason, and the agent rolls back to the previous image -- which knows
        only 001 but now sees 002 applied. Under equality that binary reports
        itself unhealthy and the rollback target can never come up: an
        unrecoverable loop (§3, ADR-10).
        """
        with self.db._tx() as c:
            c.execute("INSERT INTO schema_migrations VALUES ('002_future.sql', 0)")
        h = self.db.health()
        self.assertTrue(h["ok"], "a newer schema must be healthy")
        self.assertEqual(h["schema"]["ahead"], ["002_future.sql"])

    def test_surfaces_a_failing_backup(self):
        self.db.last_backup_error = "disk full"
        h = self.db.health()
        self.assertTrue(any("backup" in w for w in h["warnings"]))


class TestWriteMethods(Base):
    def test_first_sign_in_provisions_zero_grants(self):
        u = self.db.upsert_user("sub-1", "r@commsecurity.com.au", "Richard")
        self.assertEqual(u["token_version"], 1)
        self.assertEqual(self.db.roles_for(u["id"]), [])

    def test_user_is_keyed_on_sub_not_email(self):
        """A changed email must update the SAME row. Keying on email hands a
        departed employee's grants to their replacement (ADR-18)."""
        a = self.db.upsert_user("sub-1", "old@commsecurity.com.au", "R")
        b = self.db.upsert_user("sub-1", "new@commsecurity.com.au", "R")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM users"), 1)
        self.assertEqual(
            self.db.scalar("SELECT email FROM users"), "new@commsecurity.com.au")

    def test_role_grant_applies_without_re_login(self):
        u = self.db.upsert_user("sub-1", "r@x", "R")
        self.db.grant_role(u["id"], 1, "viewer", u["id"])
        self.assertEqual(self.db.roles_for(u["id"]),
                         [{"entity_id": 1, "role": "viewer"}])

    def test_token_bump_revokes(self):
        u = self.db.upsert_user("sub-1", "r@x", "R")
        self.db.bump_token_version(u["id"], u["id"])
        self.assertEqual(
            self.db.scalar("SELECT token_version FROM users WHERE id=?", (u["id"],)),
            2)

    def test_sign_in_is_audited(self):
        self.db.upsert_user("sub-1", "r@x", "R")
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM audit_log WHERE action='sign_in'"), 1)

    def test_job_numbers_are_sequential_and_unique_under_concurrency(self):
        issued, lock = [], threading.Lock()

        def take():
            for _ in range(15):
                n = self.db.next_job_number()
                with lock:
                    issued.append(n)

        ts = [threading.Thread(target=take) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(issued), 60)
        self.assertEqual(len(set(issued)), 60, "duplicate job number issued")


if __name__ == "__main__":
    unittest.main(verbosity=2)
