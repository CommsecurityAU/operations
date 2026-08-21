"""Database access. ALL writes go through here (CS-OP-ARCH-002 §4).

Connection model
----------------
ONE write connection guarded by ONE lock, and thread-local READ-ONLY
connections that take no lock. A single shared connection would serialise
every read behind every other read, which throws away the only thing WAL
buys and makes the §14 read budgets unreachable by construction: one 150 ms
dashboard query would stall the whole process (ADR-16).

Handlers never write SQL. Every mutation is a `Db` method whose body runs in
`with self._tx() as c:`. Reads may be SQL strings in modules, executed via
`query()`. Never hold the write lock across anything slow.
"""

import os
import sqlite3
import threading
import time
from typing import Any

# STRICT tables need 3.37; UPDATE ... RETURNING needs 3.35. Asserted at boot
# rather than discovered at the first failing migration, and asserted again
# inside the built image by CI (§13).
MIN_SQLITE = (3, 37, 0)

WRITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=FULL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
)

READ_PRAGMAS = (
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA query_only=ON",
)


class MigrationError(Exception):
    pass


def _check_sqlite_version():
    actual = tuple(int(p) for p in sqlite3.sqlite_version.split("."))
    if actual < MIN_SQLITE:
        raise RuntimeError(
            f"SQLite {'.'.join(map(str, MIN_SQLITE))}+ required for STRICT tables "
            f"and UPDATE ... RETURNING; this build has {sqlite3.sqlite_version}. "
            "Check the pinned base image digest (§13)."
        )


def rows(cursor):
    """sqlite3.Row -> plain dicts. The ONE representation change (§5).

    SELECT column names are the JSON field names are the JS property names,
    snake_case end to end. No entity classes, no serialisers, no mapping layer.
    """
    return [dict(r) for r in cursor.fetchall()]


class Db:
    def __init__(self, path, migrations_dir):
        _check_sqlite_version()
        self.path = os.path.abspath(path)
        self.migrations_dir = migrations_dir
        self._lock = threading.Lock()
        self._local = threading.local()
        # Every read connection ever handed out, so close() is deterministic.
        # Relying on the owning thread dying and CPython refcounting to close
        # these leaves file handles open for as long as the Thread object is
        # referenced -- invisible on Linux, an unlinkable file on Windows.
        self._read_conns = []
        self._read_reg = threading.Lock()
        # Declared here, not conjured when a backup first fails: an attribute
        # that only exists after an error is invisible to readers and to the
        # type checker.
        self.last_backup_error = None

        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # check_same_thread=False is safe ONLY because every use is serialised
        # by self._lock. Do not call this connection outside _tx().
        self._write = sqlite3.connect(self.path, check_same_thread=False)
        self._write.row_factory = sqlite3.Row
        for p in WRITE_PRAGMAS:
            self._write.execute(p)

    # ------------------------------------------------------------- reads
    @property
    def _read(self):
        """One read-only connection per thread. No lock: WAL permits many
        concurrent readers alongside the single writer.

        Thread-per-connection in the HTTP layer means one of these per live
        HTTP connection, which is why §3 caps concurrent connections rather
        than letting threads accumulate without bound.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            uri = f"file:{self.path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            for p in READ_PRAGMAS:
                conn.execute(p)
            self._local.conn = conn
            with self._read_reg:
                self._read_conns.append(conn)
        return conn

    def query(self, sql, params=()):
        return rows(self._read.execute(sql, params))

    def query_one(self, sql, params=()):
        r = self._read.execute(sql, params).fetchone()
        return dict(r) if r else None

    def scalar(self, sql, params=()):
        r = self._read.execute(sql, params).fetchone()
        return r[0] if r else None

    # ------------------------------------------------------------ writes
    class _Tx:
        def __init__(self, db):
            self._db = db

        def __enter__(self):
            self._db._lock.acquire()
            self._db._write.execute("BEGIN IMMEDIATE")
            return self._db._write

        def __exit__(self, exc_type, exc, tb):
            try:
                if exc_type is None:
                    self._db._write.commit()
                else:
                    self._db._write.rollback()
            finally:
                self._db._lock.release()
            return False

    def _tx(self):
        return self._Tx(self)

    # -------------------------------------------------------- migrations
    def _expected(self):
        return sorted(
            f for f in os.listdir(self.migrations_dir) if f.endswith(".sql")
        )

    def _applied(self):
        try:
            return sorted(
                r["version"] for r in rows(
                    self._write.execute("SELECT version FROM schema_migrations"))
            )
        except sqlite3.OperationalError:
            return []

    def migrate(self):
        """Numbered, forward-only, one transaction each, recorded in
        schema_migrations. Returns the versions applied by this call.

        Migrations must NOT contain their own transaction control -- the
        runner supplies BEGIN/COMMIT. A failure inside executescript leaves
        the transaction OPEN and the partial work in place, so the rollback
        here is not belt-and-braces; without it a failed migration leaves a
        half-applied schema behind.
        """
        with self._lock:
            self._write.executescript(
                "BEGIN;"
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "  version TEXT PRIMARY KEY, applied_ts INTEGER NOT NULL) STRICT;"
                "COMMIT;"
            )
            done = set(self._applied())
            applied = []
            for version in self._expected():
                if version in done:
                    continue
                with open(os.path.join(self.migrations_dir, version),
                          encoding="utf-8") as f:
                    sql = f.read()
                if "BEGIN" in sql.upper().split("--")[0] and ";" in sql:
                    pass  # triggers legitimately contain BEGIN ... END
                try:
                    self._write.executescript(f"BEGIN;\n{sql}\nCOMMIT;")
                    self._write.execute(
                        "INSERT INTO schema_migrations VALUES (?, ?)",
                        (version, int(time.time())))
                    self._write.commit()
                except Exception as e:
                    self._write.rollback()
                    raise MigrationError(f"{version} failed, rolled back: {e}") from e
                applied.append(version)
            return applied

    # ------------------------------------------------------------ health
    def health(self):
        """§3. 200 only if the DB opens, quick_check passes, and every
        migration this binary ships has been applied.

        applied >= expected, NEVER equality. A newer schema is healthy.
        Equality would brick the deploy loop: a release that migrates and
        then fails the gate for any other reason gets rolled back onto a
        binary that now sees a schema ahead of it and declares ITSELF
        unhealthy -- forever (ADR-10, §4 N-1).
        """
        report: dict[str, Any] = {
            "ok": False, "schema": None, "integrity": None, "warnings": []}
        try:
            check = self._write.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.Error as e:
            report["integrity"] = f"unreadable: {e}"
            return report
        report["integrity"] = check
        if check != "ok":
            return report

        expected, applied = set(self._expected()), set(self._applied())
        missing = sorted(expected - applied)
        report["schema"] = {
            "expected": len(expected),
            "applied": len(applied),
            "missing": missing,
            "ahead": sorted(applied - expected),
        }
        if missing:
            return report

        # A backup job that dies quietly takes the RPO with it and nothing
        # external notices, so its failure is surfaced here (ADR-17).
        if self.last_backup_error:
            report["warnings"].append(f"backup: {self.last_backup_error}")

        report["ok"] = True
        return report

    def close(self):
        """Closes EVERY read connection, not just this thread's."""
        with self._read_reg:
            for conn in self._read_conns:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._read_conns.clear()
        self._local.conn = None
        self._write.close()

    # ==================================================================
    # Write methods. One commented section per module (§6); modules never
    # import each other and never write SQL of their own.
    # ==================================================================

    # ------------------------------------------------------------ users
    def upsert_user(self, oidc_sub, email, display_name):
        """First sign-in provisions the row with ZERO entity grants. `hd`
        proves someone is staff; it says nothing about whether they should
        see money (ADR-18). Keyed on sub, never email.
        """
        now = int(time.time())
        with self._tx() as c:
            c.execute(
                """INSERT INTO users (oidc_sub, email, display_name, created_ts,
                                      last_seen_ts)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(oidc_sub) DO UPDATE SET
                       email = excluded.email,
                       display_name = excluded.display_name,
                       last_seen_ts = excluded.last_seen_ts""",
                (oidc_sub, email, display_name, now, now))
            user = c.execute(
                "SELECT * FROM users WHERE oidc_sub = ?", (oidc_sub,)).fetchone()
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action, target_type,
                                          target_id)
                   VALUES (?,?,'sign_in','user',?)""",
                (now, user["id"], str(user["id"])))
            return dict(user)

    def bump_token_version(self, user_id, actor_id):
        """Instant revocation: kills every live session for this user on
        their next request. No session table, no cleanup job (ADR-13)."""
        now = int(time.time())
        with self._tx() as c:
            c.execute(
                "UPDATE users SET token_version = token_version + 1 WHERE id = ?",
                (user_id,))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action, target_type,
                                          target_id)
                   VALUES (?,?,'token_version_bump','user',?)""",
                (now, actor_id, str(user_id)))

    def grant_role(self, user_id, entity_id, role, actor_id):
        now = int(time.time())
        with self._tx() as c:
            c.execute(
                """INSERT OR IGNORE INTO user_entity_role
                   (user_id, entity_id, role, granted_by, granted_ts)
                   VALUES (?,?,?,?,?)""",
                (user_id, entity_id, role, actor_id, now))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action, target_type,
                                          target_id, detail)
                   VALUES (?,?,'role_grant','user',?,?)""",
                (now, actor_id, str(user_id), f"entity={entity_id} role={role}"))

    def roles_for(self, user_id):
        """Resolved from the DB on EVERY request. A token is never a bag of
        permissions, so a role edit applies on the next click (§9)."""
        return self.query(
            "SELECT entity_id, role FROM user_entity_role WHERE user_id = ?",
            (user_id,))

    # ----------------------------------------------------------- projects
    def next_job_number(self):
        """Global sequence, issued inside the caller's transaction (§4)."""
        with self._tx() as c:
            n = c.execute(
                "UPDATE job_number_sequence SET next_value = next_value + 1 "
                "WHERE id = 1 RETURNING next_value - 1").fetchone()[0]
            return f"JN-{n}"
