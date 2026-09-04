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

import contextlib
import hashlib
import os
import re
import shutil
import sqlite3
import threading
import time
from typing import Any

from ops import money

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


class JobNumberError(Exception):
    """Allocation refused. Carries the reason, which is always actionable."""


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
        self._seed_from_template()
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

    #: Where a pre-migrated database is cached. `OPS_SCHEMA_CACHE`, or off.
    #:
    #: Applying twenty-four migrations costs about 65 ms and copying the
    #: result costs 3 ms. Across a thousand tests that is a minute of every
    #: run on Linux and over two on Windows — enough that people stop
    #: running the suite before committing, which is the real cost. It is
    #: SQL execution rather than fsync, so no pragma helps: an in-memory
    #: database saves 5 ms of the 65.
    #:
    #: AN ENVIRONMENT VARIABLE, not a hook. The obvious place was
    #: `tests/__init__.py`, and it never ran: `unittest discover` puts the
    #: test directory on `sys.path` and imports the modules top-level, so
    #: the package init is never imported. The cache looked enabled, the
    #: suite was unchanged, and only counting the migrations showed why.
    #:
    #: Production does not set it and its path is unchanged.
    _template_dir = os.environ.get("OPS_SCHEMA_CACHE") or None

    @classmethod
    def use_template_cache(cls, directory):
        """Cache the migrated schema in `directory`."""
        cls._template_dir = directory

    def _template_path(self):
        """Keyed on the CONTENT of every migration, so editing one — or
        adding one — invalidates the cache. Keyed on the name alone, a
        changed migration would be silently skipped and every test would
        run against yesterday's schema."""
        if not self._template_dir:
            return None
        digest = hashlib.sha256()
        for name in sorted(os.listdir(self.migrations_dir)):
            if not name.endswith(".sql"):
                continue
            digest.update(name.encode())
            with open(os.path.join(self.migrations_dir, name), "rb") as f:
                digest.update(f.read())
        return os.path.join(self._template_dir,
                            f"schema-{digest.hexdigest()[:16]}.db")

    def _seed_from_template(self):
        """Copy the cached schema in, if there is one and this database does
        not exist yet. A database with anything already in it is never
        touched."""
        template = self._template_path()
        if not template or not os.path.exists(template):
            return
        if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
            return
        shutil.copyfile(template, self.path)

    def _save_template(self):
        """VACUUM INTO, not a file copy: the write connection is open and a
        copy of a live WAL database is a copy that disagrees with itself."""
        template = self._template_path()
        if not template or os.path.exists(template):
            return
        os.makedirs(os.path.dirname(template), exist_ok=True)
        scratch = f"{template}.{os.getpid()}"
        try:
            self._write.execute("VACUUM INTO ?", (scratch,))
            os.replace(scratch, template)      # atomic; parallel runs are fine
        except Exception:
            # A cache that cannot be written is a slow suite, not a broken
            # one.
            if os.path.exists(scratch):
                os.unlink(scratch)

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
                    if version.endswith(".nofk.sql"):
                        self._rebuild_migration(version, sql)
                    else:
                        self._write.executescript(f"BEGIN;\n{sql}\nCOMMIT;")
                    self._write.execute(
                        "INSERT INTO schema_migrations VALUES (?, ?)",
                        (version, int(time.time())))
                    self._write.commit()
                except Exception as e:
                    self._write.rollback()
                    raise MigrationError(f"{version} failed, rolled back: {e}") from e
                applied.append(version)
            if applied:
                # Only after a run that actually did work, and only into a
                # cache somebody switched on.
                self._save_template()
            return applied

    def _rebuild_migration(self, version, sql):
        """A migration that REBUILDS A REFERENCED TABLE.

        Widening a CHECK means recreating the table, and with foreign keys
        on, dropping one that eight other tables reference simply fails.
        `PRAGMA foreign_keys` is a no-op inside a transaction, so such a
        migration cannot run the way the others do.

        This is SQLite's own documented procedure, and the important part is
        the END of it: `foreign_key_check` runs before the commit, so a
        rebuild that orphans a row is refused rather than discovered later.
        Turning the keys off is the dangerous bit; verifying before letting
        go is what makes it safe.

        Named by the file: `NNN_thing.nofk.sql`. Explicit, because a
        migration that silently disabled referential integrity would be a
        migration nobody could review.
        """
        # Every view is dropped and put back around the rebuild.
        #
        # SQLite validates every view when a table is dropped, so a rebuild
        # fails on the first view that mentions the table -- and there are
        # eight. Copying their definitions into the migration would
        # duplicate two hundred lines that then have to stay in step with
        # the originals forever, and the views do not change: only the
        # table beneath them does.
        #
        # A view the migration recreates ITSELF is left alone, so a
        # migration that legitimately redefines one still can.
        views = [(r["name"], r["sql"]) for r in self.query(
            "SELECT name, sql FROM sqlite_master WHERE type = 'view' "
            "AND sql IS NOT NULL ORDER BY name")]
        self._write.execute("PRAGMA foreign_keys=OFF")
        try:
            drop = "".join(f"DROP VIEW IF EXISTS {name};\n"
                           for name, _sql in views)
            self._write.executescript(f"BEGIN;\n{drop}{sql}\nCOMMIT;")
            back = [(name, definition) for name, definition in views
                    if not self._write.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='view' "
                        "AND name = ?", (name,)).fetchone()]
            if back:
                self._write.executescript(
                    "BEGIN;\n"
                    + ";\n".join(definition for _name, definition in back)
                    + ";\nCOMMIT;")
            broken = self._write.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                # Roll the whole thing back: a schema whose references do
                # not resolve is worse than the CHECK we were widening.
                self._write.executescript(
                    "BEGIN;" + "".join(
                        f"-- {row}\n" for row in broken[:5]) + "ROLLBACK;")
                raise MigrationError(
                    f"{version} would orphan {len(broken)} row(s): "
                    f"{broken[:3]}")
        finally:
            self._write.execute("PRAGMA foreign_keys=ON")

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

    def active_admin_exists(self):
        """Is there ANY active admin on ANY entity? The bootstrap only
        runs while the answer is no, so a system that already has an
        administrator can never be re-seeded from an environment variable."""
        return bool(self.scalar(
            """SELECT COUNT(*) FROM user_entity_role r
               JOIN users u ON u.id = r.user_id
               WHERE r.role = 'admin' AND u.is_active = 1"""))

    def bootstrap_admin(self, user_id, roles):
        """Every role on every active entity, self-granted, ONE audit line
        per grant plus one naming the bootstrap. This is the seed an empty
        system needs before the admin UI can be used at all; after it, all
        further grants go through that UI and its audit trail."""
        now = int(time.time())
        entities = [r["id"] for r in self.query(
            "SELECT id FROM entity WHERE is_active = 1 ORDER BY id")]
        with self._tx() as c:
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action, target_type,
                                          target_id, detail)
                   VALUES (?,?,'bootstrap_admin','user',?,?)""",
                (now, user_id, str(user_id),
                 f"entities={entities} roles={list(roles)}"))
            for entity_id in entities:
                for role in roles:
                    c.execute(
                        """INSERT OR IGNORE INTO user_entity_role
                           (user_id, entity_id, role, granted_by, granted_ts)
                           VALUES (?,?,?,?,?)""",
                        (user_id, entity_id, role, user_id, now))
                    c.execute(
                        """INSERT INTO audit_log (ts, actor_user_id, action,
                                                  target_type, target_id, detail)
                           VALUES (?,?,'role_grant','user',?,?)""",
                        (now, user_id, str(user_id),
                         f"entity={entity_id} role={role} via=bootstrap"))
        return entities

    # ---------------------------------------------------------- suppliers
    SUPPLIER_FIELDS = ("name", "itrade_ref", "xero_ref", "abn",
                       "default_currency", "payment_terms_days",
                       "contact_name", "phone", "email", "address", "note",
                       "is_active")

    def create_suppliers(self, rows, actor_id):
        now = int(time.time())
        if not rows:
            return 0
        with self._tx() as c:
            for row in rows:
                c.execute(
                    """INSERT INTO supplier
                       (entity_id, name, itrade_ref, xero_ref, abn,
                        default_currency, payment_terms_days, contact_name,
                        phone, email, address, note, created_by, created_ts)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (row.get("entity_id", 1), row["name"],
                     row.get("itrade_ref"), row.get("xero_ref"),
                     row.get("abn"), row.get("default_currency", "AUD"),
                     row.get("payment_terms_days"), row.get("contact_name"),
                     row.get("phone"), row.get("email"), row.get("address"),
                     row.get("note"), actor_id, now))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'supplier_import','supplier',?,?)""",
                (now, actor_id, "", f"{len(rows)} supplier(s) added"))
        return len(rows)

    def update_suppliers(self, changes, actor_id):
        """`changes` is [(supplier_id, {field: value})]. Each field change is
        kept: a currency or an ABN that turns out to have been wrong changes
        what was withheld and what was paid."""
        now = int(time.time())
        if not changes:
            return 0
        with self._tx() as c:
            for supplier_id, fields in changes:
                before = c.execute("SELECT * FROM supplier WHERE id = ?",
                                   (supplier_id,)).fetchone()
                if before is None:
                    continue
                for key, value in fields.items():
                    if key not in self.SUPPLIER_FIELDS or before[key] == value:
                        continue
                    c.execute(f"UPDATE supplier SET {key} = ? WHERE id = ?",
                              (value, supplier_id))
                    c.execute(
                        """INSERT INTO supplier_revision (supplier_id, field,
                               old_value, new_value, reason, changed_by,
                               changed_ts)
                           VALUES (?,?,?,?,?,?,?)""",
                        (supplier_id, key,
                         None if before[key] is None else str(before[key]),
                         None if value is None else str(value),
                         "iTrade import", actor_id, now))
        return len(changes)

    # -------------------------------------------------------- procurement
    #: `project_id` is here because a line entered against the wrong job is
    #: the commonest slip, and it moves the cost onto another project's
    #: margin. Leaving it out meant the update was accepted and ignored,
    #: which is worse than refusing it.
    LINE_MUTABLE = ("project_id", "supplier_id", "supplier_po_id",
                    "supplier_quote_id", "currency", "stated_state",
                    "is_estimate",
                    "supplier_invoice_id", "period_id", "item", "description",
                    "quantity", "currency", "unit_cost_cents", "total_cents",
                    "requested_date", "ordered_date", "invoiced_date",
                    "delivered_date", "paid_date", "cancelled_date",
                    "cancel_reason", "note")

    @staticmethod
    def extend(unit_cents, quantity, fx_rate_bp=None):
        """A line's total, converted ONCE at the extended amount.

        The register does it this way and it is right: `$33.00 x 7` at
        1.388561 is $320.76, while rounding the unit to $45.82 first and
        multiplying gives $320.74. Five lines in the register differ by a
        cent or two for exactly that reason, and converting last removes the
        difference rather than reconciling it.
        """
        gross = unit_cents * max(1, int(quantity))
        if not fx_rate_bp:
            return gross
        return money.divide(gross * fx_rate_bp, 10_000_000)

    def create_procurement_line(self, fields, actor_id):
        now = int(time.time())
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO procurement_line
                   (entity_id, project_id, supplier_id, supplier_po_id,
                    supplier_quote_id, supplier_invoice_id, period_id,
                    item, description, quantity, currency, unit_cost_cents,
                    total_cents, requested_date, ordered_date, invoiced_date,
                    delivered_date, paid_date, stated_state, is_estimate,
                    note, created_by, created_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fields.get("entity_id", 1), fields["project_id"],
                 fields.get("supplier_id"), fields.get("supplier_po_id"),
                 fields.get("supplier_quote_id"),
                 fields.get("supplier_invoice_id"), fields.get("period_id"),
                 fields.get("item"), fields.get("description"),
                 fields.get("quantity", 1), fields.get("currency", "AUD"),
                 fields.get("unit_cost_cents", 0),
                 fields.get("total_cents", 0), fields.get("requested_date"),
                 fields.get("ordered_date"), fields.get("invoiced_date"),
                 fields.get("delivered_date"), fields.get("paid_date"),
                 fields.get("stated_state"), fields.get("is_estimate", 0),
                 fields.get("note"), actor_id, now))
            line_id = cur.lastrowid
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'procurement_create','procurement_line',?,?)""",
                (now, actor_id, str(line_id),
                 f"{fields.get('item') or '(no item)'} "
                 f"x{fields.get('quantity', 1)} "
                 f"{money.format(fields.get('total_cents', 0))}"))
            return dict(c.execute(
                "SELECT * FROM procurement_line WHERE id = ?",
                (line_id,)).fetchone())

    def update_procurement_line(self, line_id, changes, actor_id, reason=None):
        now = int(time.time())
        with self._tx() as c:
            before = c.execute("SELECT * FROM procurement_line WHERE id = ?",
                               (line_id,)).fetchone()
            if before is None:
                return None
            applied = []
            for key in self.LINE_MUTABLE:
                if key not in changes or before[key] == changes[key]:
                    continue
                c.execute(f"UPDATE procurement_line SET {key} = ? WHERE id = ?",
                          (changes[key], line_id))
                applied.append(key)
                c.execute(
                    """INSERT INTO procurement_line_revision (line_id, field,
                           old_value, new_value, reason, changed_by, changed_ts)
                       VALUES (?,?,?,?,?,?,?)""",
                    (line_id, key,
                     None if before[key] is None else str(before[key]),
                     None if changes[key] is None else str(changes[key]),
                     reason, actor_id, now))
            return {"line": dict(c.execute(
                "SELECT * FROM v_procurement_line WHERE id = ?",
                (line_id,)).fetchone()), "changed": sorted(applied)}

    def resolve_supplier(self, entity_id, name):
        """Exact, then case-insensitive, then a recorded alias. NEVER fuzzy:
        it would get `Colterlec` right and `USR` wrong, and a wrong supplier
        on an order puts spend against a company that never sold us
        anything."""
        text = (name or "").strip()
        if not text:
            return None
        row = self.query_one(
            """SELECT id FROM supplier
               WHERE entity_id = ? AND name = ? COLLATE NOCASE""",
            (entity_id, text))
        if row:
            return row["id"]
        row = self.query_one(
            """SELECT supplier_id AS id FROM supplier_alias
               WHERE entity_id = ? AND alias = ? COLLATE NOCASE""",
            (entity_id, text))
        return row["id"] if row else None

    def add_supplier_alias(self, entity_id, alias, supplier_id, actor_id,
                           note=None):
        now = int(time.time())
        with self._tx() as c:
            c.execute(
                """INSERT OR IGNORE INTO supplier_alias
                   (entity_id, alias, supplier_id, note, created_by, created_ts)
                   VALUES (?,?,?,?,?,?)""",
                (entity_id, alias.strip(), supplier_id, note, actor_id, now))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'supplier_alias','supplier',?,?)""",
                (now, actor_id, str(supplier_id), f"{alias!r} resolves here"))
        return True

    def clear_procurement(self, actor_id):
        """Undo an import. Deliberate and total: a partial import leaves
        quotes and orders with no lines, which reads as real procurement
        that nobody ordered."""
        now = int(time.time())
        with self._tx() as c:
            for table in ("procurement_line_revision", "procurement_line",
                          "supplier_po", "supplier_invoice", "supplier_quote"):
                c.execute(f"DELETE FROM {table}")
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'procurement_reset','procurement_line',?,?)""",
                (now, actor_id, "", "every procurement row cleared"))
        return True

    # --------------------------------------------------- office expenses
    def import_expense_matrix(self, entity_id, lines, months, nsw, kinds,
                              salary_steps, actor_id):
        """The whole matrix in one transaction.

        One transaction because a half-imported expense sheet reads as a
        business that costs less to run than it does.
        """
        now = int(time.time())
        periods = {}
        for label in months:
            found = self.scalar("SELECT id FROM period WHERE label = ?",
                                (label,))
            if found:
                periods[label] = found
        made = {"categories": 0, "lines": 0, "amounts": 0, "salaries": 0}
        with self._tx() as c:
            categories = {}
            for order, line in enumerate(lines):
                name = line["category"]
                # Work Cover and Payroll Tax are each two obligations under
                # two schemes to two insurers, so they are two categories.
                # Grouping them together gives a header that is the sum of
                # a VIC charge and an NSW one, which is a number nobody
                # asks for.
                if kinds.get(name.casefold()) == "statutory":
                    where = "NSW" if "nsw" in line["name"].lower() else "VIC"
                    name = f"{name} ({where})"
                if name not in categories:
                    kind = kinds.get(line["category"].casefold(), "expense")
                    cur = c.execute(
                        """INSERT INTO expense_category
                           (entity_id, name, kind, sequence, created_by,
                            created_ts)
                           VALUES (?,?,?,?,?,?)""",
                        (entity_id, name, kind, len(categories), actor_id, now))
                    categories[name] = (cur.lastrowid, kind)
                    made["categories"] += 1
                category_id, kind = categories[name]

                rate_bp = None
                # The rate is written in the line's own name, which is where
                # it is maintained: `Work Cover 1.785%`.
                import re as _re
                found = _re.search(r"(\d+(?:\.\d+)?)\s*%?\s*$", line["name"])
                if kind == "statutory" and found:
                    rate_bp = int(round(float(found.group(1)) * 100))

                is_forecast = 1 if "forecast" in line["name"].lower() else 0
                state = None
                if kind in ("wages", "super"):
                    state = "NSW" if line["name"].casefold() in nsw else "VIC"
                elif "nsw" in line["name"].lower():
                    state = "NSW"
                elif kind == "statutory":
                    state = "VIC"

                # How this line is worked out, where it is worked out at
                # all. Super follows the person's own wages; the statutory
                # lines follow wages plus super for their state; NSW
                # payroll tax takes $47,000 a year off first.
                formula = threshold = None
                if kind == "super":
                    formula = "percent_of_line"
                    rate_bp = self.rate(12)
                elif kind == "statutory":
                    lower = line["name"].lower()
                    if "nsw" in lower and "payroll" in lower:
                        formula = "percent_less_annual"
                        threshold = 4_700_000          # $47,000 a year
                        rate_bp = self.rate(5.45)
                    elif "nsw" in lower:
                        # The sheet computes 0.405% while the line is
                        # called 0.39%. Confirmed with the Ops Manager: the
                        # LABEL is right and the sheet's rate is wrong, so
                        # $81.27 a month becomes $78.26. The name was the
                        # fact here, which is the opposite of the usual
                        # direction and worth having asked.
                        formula = "percent_of_state"
                        rate_bp = self.rate(0.39)
                    elif "payroll" in lower:
                        formula = "percent_of_state"
                        rate_bp = self.rate(4.85)
                    else:
                        formula = "percent_of_state"
                        rate_bp = self.rate(1.785)
                cur = c.execute(
                    """INSERT INTO expense_line
                       (entity_id, category_id, name, state, is_forecast,
                        rate_bp, formula, threshold_annual_cents, sequence,
                        created_by, created_ts)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (entity_id, category_id, line["name"], state, is_forecast,
                     rate_bp, formula, threshold, order, actor_id, now))
                line_id = cur.lastrowid
                if kind == "super":
                    # A person's super follows THEIR wages, matched on the
                    # name the sheet uses for both.
                    c.execute(
                        """UPDATE expense_line SET basis_line_id = (
                               SELECT w.id FROM expense_line w
                               JOIN expense_category wc ON wc.id = w.category_id
                               WHERE wc.kind = 'wages' AND w.entity_id = ?
                                 AND w.name = ? COLLATE NOCASE)
                           WHERE id = ?""",
                        (entity_id, line["name"], line_id))
                made["lines"] += 1

                steps = salary_steps(line["amounts"], months) \
                    if kind == "wages" else None
                if steps:
                    for label, annual in steps:
                        if label not in periods:
                            continue
                        c.execute(
                            """INSERT INTO salary_revision
                               (expense_line_id, from_period_id, annual_cents,
                                created_by, created_ts)
                               VALUES (?,?,?,?,?)""",
                            (line_id, periods[label], annual, actor_id, now))
                        made["salaries"] += 1

                for label, cents in line["amounts"].items():
                    if label not in periods:
                        continue
                    # A figure the platform can work out is marked as
                    # such, so recomputing OWNS it. Marking everything
                    # `entered` meant the first recompute changed nothing
                    # and the sheet's stale payroll tax survived the
                    # import.
                    source = ("salary" if steps
                              else "rate" if formula
                              else "entered")
                    c.execute(
                        """INSERT INTO expense_amount
                           (expense_line_id, period_id, amount_cents, source,
                            created_by, created_ts)
                           VALUES (?,?,?,?,?,?)""",
                        (line_id, periods[label], cents, source, actor_id, now))
                    made["amounts"] += 1
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'expense_import','expense_line',?,?)""",
                (now, actor_id, "",
                 f"{made['lines']} line(s), {made['amounts']} monthly "
                 f"figure(s)"))
        return made

    #: Everything the API will accept. A field missing here is a field the
    #: API takes and the database drops -- accepted-and-ignored, which is
    #: worse than refused because the screen says it worked. It happened
    #: with `project_id` on a procurement line and again with
    #: `threshold_annual_cents` here, so `test_gates` now checks both.
    EXPENSE_LINE_MUTABLE = ("category_id", "name", "state", "is_forecast",
                            "rate_bp", "formula", "basis_line_id",
                            "threshold_annual_cents", "note", "is_active",
                            "sequence")

    def record_salary_view(self, line_id, actor_id):
        """Who looked at whose pay, and when.

        A control that leaves no trace is a control nobody can check was
        working.
        """
        now = int(time.time())
        name = self.scalar("SELECT name FROM expense_line WHERE id = ?",
                           (line_id,))
        with self._tx() as c:
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'salary_view','expense_line',?,?)""",
                (now, actor_id, str(line_id), name or ""))
        return True

    def create_expense_category(self, entity_id, name, kind, actor_id):
        now = int(time.time())
        with self._tx() as c:
            nxt = c.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM expense_category "
                "WHERE entity_id = ?", (entity_id,)).fetchone()[0]
            cur = c.execute(
                """INSERT INTO expense_category
                   (entity_id, name, kind, sequence, created_by, created_ts)
                   VALUES (?,?,?,?,?,?)""",
                (entity_id, name, kind, nxt, actor_id, now))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'expense_category_create','expense_category',?,?)""",
                (now, actor_id, str(cur.lastrowid), f"{name} ({kind})"))
            return dict(c.execute("SELECT * FROM expense_category WHERE id = ?",
                                  (cur.lastrowid,)).fetchone())

    def create_expense_line(self, fields, actor_id):
        now = int(time.time())
        with self._tx() as c:
            nxt = c.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM expense_line "
                "WHERE category_id = ?", (fields["category_id"],)).fetchone()[0]
            cur = c.execute(
                """INSERT INTO expense_line
                   (entity_id, category_id, name, state, is_forecast, rate_bp,
                    sequence, note, created_by, created_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (fields["entity_id"], fields["category_id"], fields["name"],
                 fields.get("state"), fields.get("is_forecast", 0),
                 fields.get("rate_bp"), nxt, fields.get("note"), actor_id, now))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'expense_line_create','expense_line',?,?)""",
                (now, actor_id, str(cur.lastrowid), fields["name"]))
            return dict(c.execute("SELECT * FROM v_expense_line WHERE line_id = ?",
                                  (cur.lastrowid,)).fetchone())

    def update_expense_line(self, line_id, changes, actor_id):
        now = int(time.time())
        with self._tx() as c:
            before = c.execute("SELECT * FROM expense_line WHERE id = ?",
                               (line_id,)).fetchone()
            if before is None:
                return None
            applied = []
            for key in self.EXPENSE_LINE_MUTABLE:
                if key not in changes or before[key] == changes[key]:
                    continue
                c.execute(f"UPDATE expense_line SET {key} = ? WHERE id = ?",
                          (changes[key], line_id))
                applied.append(key)
                c.execute(
                    """INSERT INTO audit_log (ts, actor_user_id, action,
                           target_type, target_id, detail)
                       VALUES (?,?,'expense_line_update','expense_line',?,?)""",
                    (now, actor_id, str(line_id),
                     f"{key}: {before[key]!r} -> {changes[key]!r}"))
            return {"line": dict(c.execute(
                "SELECT * FROM v_expense_line WHERE line_id = ?",
                (line_id,)).fetchone()), "changed": sorted(applied)}

    def set_expense_amount(self, line_id, period_id, cents, actor_id,
                           reason=None):
        """Setting it to nothing removes it: a month a line does not run in
        should be absent, not zero."""
        now = int(time.time())
        with self._tx() as c:
            found = c.execute(
                """SELECT id, amount_cents FROM expense_amount
                   WHERE expense_line_id = ? AND period_id = ?""",
                (line_id, period_id)).fetchone()
            if not cents:
                if found:
                    c.execute(
                        """INSERT INTO expense_amount_revision
                           (expense_line_id, period_id, old_cents, new_cents,
                            reason, changed_by, changed_ts)
                           VALUES (?,?,?,NULL,?,?,?)""",
                        (line_id, period_id, found["amount_cents"], reason,
                         actor_id, now))
                    c.execute("DELETE FROM expense_amount WHERE id = ?",
                              (found["id"],))
                return {"removed": bool(found)}
            if found:
                c.execute(
                    """INSERT INTO expense_amount_revision
                       (expense_line_id, period_id, old_cents, new_cents,
                        reason, changed_by, changed_ts)
                       VALUES (?,?,?,?,?,?,?)""",
                    (line_id, period_id, found["amount_cents"], cents, reason,
                     actor_id, now))
                c.execute(
                    "UPDATE expense_amount SET amount_cents = ?, source = "
                    "'entered' WHERE id = ?", (cents, found["id"]))
                return {"id": found["id"], "amount_cents": cents}
            cur = c.execute(
                """INSERT INTO expense_amount
                   (expense_line_id, period_id, amount_cents, source,
                    created_by, created_ts)
                   VALUES (?,?,?, 'entered', ?,?)""",
                (line_id, period_id, cents, actor_id, now))
            return {"id": cur.lastrowid, "amount_cents": cents}

    def set_salary(self, line_id, from_period_id, annual_cents, actor_id,
                   note=None):
        """A salary is the fact and the months are its consequence, so every
        month from this one onward is recomputed. Months carrying an ENTERED
        figure are left alone: somebody typed those on purpose."""
        now = int(time.time())
        start = self.scalar("SELECT month_start FROM period WHERE id = ?",
                            (from_period_id,))
        with self._tx() as c:
            c.execute(
                """INSERT INTO salary_revision
                   (expense_line_id, from_period_id, annual_cents, note,
                    created_by, created_ts)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT (expense_line_id, from_period_id)
                   DO UPDATE SET annual_cents = excluded.annual_cents,
                                 note = excluded.note""",
                (line_id, from_period_id, annual_cents, note, actor_id, now))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'salary_set','expense_line',?,?)""",
                (now, actor_id, str(line_id),
                 f"{money.format(annual_cents)} a year from period "
                 f"{from_period_id}"))
        # Which revision governs each month: the latest one at or before it.
        touched = 0
        for period in self.query(
                """SELECT id, month_start FROM period WHERE month_start >= ?
                   ORDER BY month_start""", (start,)):
            annual = self.scalar(
                """SELECT r.annual_cents FROM salary_revision r
                   JOIN period p ON p.id = r.from_period_id
                   WHERE r.expense_line_id = ? AND p.month_start <= ?
                   ORDER BY p.month_start DESC LIMIT 1""",
                (line_id, period["month_start"]))
            if annual is None:
                continue
            monthly = money.divide(annual, 12)
            with self._tx() as c:
                existing = c.execute(
                    """SELECT id, source FROM expense_amount
                       WHERE expense_line_id = ? AND period_id = ?""",
                    (line_id, period["id"])).fetchone()
                if existing and existing["source"] != "salary":
                    continue
                if existing:
                    c.execute(
                        "UPDATE expense_amount SET amount_cents = ? WHERE id = ?",
                        (monthly, existing["id"]))
                else:
                    c.execute(
                        """INSERT INTO expense_amount
                           (expense_line_id, period_id, amount_cents, source,
                            created_by, created_ts)
                           VALUES (?,?,?, 'salary', ?,?)""",
                        (line_id, period["id"], monthly, actor_id, now))
            touched += 1
        return {"annual_cents": annual_cents, "months_updated": touched}

    @staticmethod
    def rate(percent):
        """A percentage as hundredths of a basis point: 1.785% is 17850.

        Written as a function because writing it by hand is how `0.405%`
        became `405_00` and a $81.27 Work Cover charge came out at $812.70.
        Underscores group digits; they do not check them.
        """
        return int(round(float(percent) * 10_000))

    @staticmethod
    def rate_amount(cents, rate_bp):
        """A rate in hundredths of a basis point, applied once."""
        return money.divide(cents * rate_bp, 1_000_000)

    def set_fy_settings(self, entity_id, fy, rate_bp, further_cents, actor_id,
                        note=None):
        now = int(time.time())
        with self._tx() as c:
            before = c.execute(
                "SELECT * FROM fy_settings WHERE entity_id = ? AND fy = ?",
                (entity_id, fy)).fetchone()
            rate = rate_bp if rate_bp is not None else (
                before["tax_rate_bp"] if before else 250000)
            further = further_cents if further_cents is not None else (
                before["further_sales_cents"] if before else 0)
            c.execute(
                """INSERT INTO fy_settings (entity_id, fy, tax_rate_bp,
                       further_sales_cents, note, updated_by, updated_ts)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT (entity_id, fy) DO UPDATE SET
                       tax_rate_bp = excluded.tax_rate_bp,
                       further_sales_cents = excluded.further_sales_cents,
                       note = excluded.note,
                       updated_by = excluded.updated_by,
                       updated_ts = excluded.updated_ts""",
                (entity_id, fy, rate, further, note, actor_id, now))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'fy_settings','fy_settings',?,?)""",
                (now, actor_id, str(fy),
                 f"tax {rate / 10000}%, further sales "
                 f"{money.format(further)}"))
            return dict(c.execute(
                "SELECT * FROM fy_settings WHERE entity_id = ? AND fy = ?",
                (entity_id, fy)).fetchone())

    def recompute_derived(self, entity_id, actor_id, from_period_id=None):
        """Work out every derived figure, in dependency order.

        Wages come from salaries, super follows wages, and the statutory
        lines follow wages plus super. Doing it in one pass in that order is
        why the order is written down here rather than inferred: a rate
        applied before super was recomputed would be a rate on last month's
        payroll.

        The sheet this replaces had VIC payroll tax frozen at $4,255.07
        while wages rose in Oct-26 -- $792.16 a month, $16,635.36 across the
        two years it covers. A figure that has to be dragged across a row by
        hand is a figure that eventually is not.
        """
        now = int(time.time())
        start = self.scalar("SELECT month_start FROM period WHERE id = ?",
                            (from_period_id,)) if from_period_id else None
        periods = self.query(
            """SELECT id, month_start FROM period
               WHERE (? IS NULL OR month_start >= ?) ORDER BY month_start""",
            (start, start))
        lines = self.query(
            """SELECT * FROM v_expense_line
               WHERE entity_id = ? AND is_active = 1""", (entity_id,))
        by_formula = {"percent_of_line": [], "percent_of_state": [],
                      "percent_less_annual": []}
        for line in lines:
            if line["formula"] in by_formula:
                by_formula[line["formula"]].append(line)
        touched = 0
        for period in periods:
            # 1. Super, from each person's own wages.
            for line in by_formula["percent_of_line"]:
                if not line["basis_line_id"] or not line["rate_bp"]:
                    continue
                base = self.scalar(
                    """SELECT amount_cents FROM expense_amount
                       WHERE expense_line_id = ? AND period_id = ?""",
                    (line["basis_line_id"], period["id"])) or 0
                touched += self._derive(line["line_id"], period["id"],
                                        money.divide(base * line["rate_bp"],
                                                     1_000_000),
                                        actor_id, now)
            # 2. The statutory lines, on wages plus super for their state.
            for kind in ("percent_of_state", "percent_less_annual"):
                for line in by_formula[kind]:
                    if not line["rate_bp"]:
                        continue
                    base = self.scalar(
                        """SELECT base_cents FROM v_wage_base
                           WHERE entity_id = ? AND period_id = ? AND state = ?""",
                        (entity_id, period["id"],
                         line["state"] or "VIC")) or 0
                    if kind == "percent_of_state":
                        value = money.divide(base * line["rate_bp"], 1_000_000)
                    else:
                        annual = base * 12 - (line["threshold_annual_cents"] or 0)
                        value = money.divide(
                            money.divide(max(0, annual) * line["rate_bp"],
                                         1_000_000), 12)
                    touched += self._derive(line["line_id"], period["id"],
                                            value, actor_id, now)
        if touched:
            with self._tx() as c:
                c.execute(
                    """INSERT INTO audit_log (ts, actor_user_id, action,
                           target_type, target_id, detail)
                       VALUES (?,?,'expense_recompute','expense_line',?,?)""",
                    (now, actor_id, "", f"{touched} derived figure(s)"))
        return touched

    def _derive(self, line_id, period_id, cents, actor_id, now):
        """Write a derived figure, leaving anything typed alone."""
        with self._tx() as c:
            found = c.execute(
                """SELECT id, amount_cents, source FROM expense_amount
                   WHERE expense_line_id = ? AND period_id = ?""",
                (line_id, period_id)).fetchone()
            if found and found["source"] == "entered":
                # Somebody typed that on purpose.
                return 0
            if not cents:
                if found:
                    c.execute("DELETE FROM expense_amount WHERE id = ?",
                              (found["id"],))
                    return 1
                return 0
            if found:
                if found["amount_cents"] == cents:
                    return 0
                c.execute(
                    "UPDATE expense_amount SET amount_cents = ?, source = 'rate' "
                    "WHERE id = ?", (cents, found["id"]))
            else:
                c.execute(
                    """INSERT INTO expense_amount
                       (expense_line_id, period_id, amount_cents, source,
                        created_by, created_ts)
                       VALUES (?,?,?, 'rate', ?,?)""",
                    (line_id, period_id, cents, actor_id, now))
            return 1

    def procurement_line_is_deletable(self, line_id):
        """Why this line cannot be deleted, or None.

        DELETE means the row should never have existed -- a duplicate, a
        mistyped entry. CANCEL means it was real and is not any more, and
        that leaves a trace on purpose.

        So a line that has been invoiced, paid or delivered is refused:
        those are facts about money that moved, and money that moved is
        cancelled rather than erased.
        """
        row = self.query_one(
            """SELECT invoiced_date, paid_date, delivered_date,
                      supplier_invoice_id, is_paid, is_delivered
               FROM v_procurement_line WHERE id = ?""", (line_id,))
        if row is None:
            return "no such line"
        for field, what in (("invoiced_date", "invoiced"),
                            ("paid_date", "paid"),
                            ("delivered_date", "delivered")):
            if row[field]:
                return (f"this line was {what} on {row[field]}; cancel it "
                        "rather than deleting it")
        if row["supplier_invoice_id"]:
            return ("this line is attached to a supplier invoice; cancel it "
                    "rather than deleting it")
        if row["is_paid"] or row["is_delivered"]:
            return ("this line reads as paid or delivered; cancel it rather "
                    "than deleting it")
        return None

    def delete_procurement_line(self, line_id, reason, actor_id):
        """Remove a row that should not exist. Reason mandatory.

        The whole row is written to the audit log first, because a deletion
        nobody can reconstruct is a deletion nobody can question.
        """
        if not (reason or "").strip():
            raise ValueError("a reason is required")
        now = int(time.time())
        row = self.query_one(
            "SELECT * FROM v_procurement_line WHERE id = ?", (line_id,))
        if row is None:
            return False
        detail = "; ".join(
            f"{k}={row[k]!r}" for k in
            ("project_name", "supplier_name", "item", "description",
             "quantity", "currency", "total_cents", "period_label",
             "is_estimate", "stated_state") if row[k] is not None)
        with self._tx() as c:
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'procurement_delete','procurement_line',?,?)""",
                (now, actor_id, str(line_id),
                 f"{detail} :: {reason.strip()}"))
            c.execute("DELETE FROM procurement_line_revision WHERE line_id = ?",
                      (line_id,))
            c.execute("DELETE FROM procurement_line WHERE id = ?", (line_id,))
        return True

    def create_supplier_quote(self, fields, actor_id):
        now = int(time.time())
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO supplier_quote
                   (entity_id, supplier_id, quote_ref, quote_date, currency,
                    fx_rate_bp, email_subject, email_sent_date, note,
                    created_by, created_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (fields.get("entity_id", 1), fields["supplier_id"],
                 fields.get("quote_ref"), fields.get("quote_date"),
                 fields.get("currency", "AUD"), fields.get("fx_rate_bp"),
                 fields.get("email_subject"), fields.get("email_sent_date"),
                 fields.get("note"), actor_id, now))
            return dict(c.execute("SELECT * FROM supplier_quote WHERE id = ?",
                                  (cur.lastrowid,)).fetchone())

    def create_supplier_po(self, fields, actor_id):
        now = int(time.time())
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO supplier_po
                   (entity_id, project_id, supplier_id, supplier_quote_id,
                    po_number, po_date, approved_by, approved_date, note,
                    created_by, created_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (fields.get("entity_id", 1), fields["project_id"],
                 fields["supplier_id"], fields.get("supplier_quote_id"),
                 fields.get("po_number"), fields.get("po_date"),
                 fields.get("approved_by"), fields.get("approved_date"),
                 fields.get("note"), actor_id, now))
            po_id = cur.lastrowid
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'supplier_po_create','supplier_po',?,?)""",
                (now, actor_id, str(po_id),
                 f"{fields.get('po_number') or '(no number)'}"))
            return dict(c.execute("SELECT * FROM supplier_po WHERE id = ?",
                                  (po_id,)).fetchone())

    def find_or_create_supplier_invoice(self, entity_id, supplier_id,
                                        invoice_ref, actor_id, **fields):
        """One invoice regularly covers several orders, so it is looked up
        by reference rather than created per line."""
        now = int(time.time())
        found = self.query_one(
            """SELECT * FROM supplier_invoice
               WHERE entity_id = ? AND supplier_id = ? AND invoice_ref = ?""",
            (entity_id, supplier_id, invoice_ref))
        if found:
            return dict(found), False
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO supplier_invoice
                   (entity_id, supplier_id, invoice_ref, invoice_date,
                    due_date, note, created_by, created_ts)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (entity_id, supplier_id, invoice_ref,
                 fields.get("invoice_date"), fields.get("due_date"),
                 fields.get("note"), actor_id, now))
            return dict(c.execute(
                "SELECT * FROM supplier_invoice WHERE id = ?",
                (cur.lastrowid,)).fetchone()), True

    def revoke_role(self, user_id, entity_id, role, actor_id):
        now = int(time.time())
        with self._tx() as c:
            cur = c.execute(
                """DELETE FROM user_entity_role
                   WHERE user_id = ? AND entity_id = ? AND role = ?""",
                (user_id, entity_id, role))
            if not cur.rowcount:
                return False
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'role_revoke','users',?,?)""",
                (now, actor_id, str(user_id), f"{role} on entity {entity_id}"))
            return True

    def set_user_active(self, user_id, active, actor_id):
        """Switching someone off must take effect NOW, not when their
        session happens to expire -- so the token version moves with it and
        every cookie they hold stops validating on the next request."""
        now = int(time.time())
        with self._tx() as c:
            row = c.execute("SELECT is_active, display_name FROM users "
                            "WHERE id = ?", (user_id,)).fetchone()
            if row is None or row["is_active"] == (1 if active else 0):
                return False
            c.execute(
                "UPDATE users SET is_active = ?, token_version = token_version + 1 "
                "WHERE id = ?", (1 if active else 0, user_id))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,?,'users',?,?)""",
                (now, actor_id,
                 "user_activate" if active else "user_deactivate",
                 str(user_id),
                 f"{row['display_name']}"
                 + ("" if active else "; every session invalidated")))
            return True

    def users_with_roles(self, entity_ids=None):
        rows = self.query(
            """SELECT u.id, u.email, u.display_name, u.is_active,
                      u.created_ts, u.last_seen_ts
               FROM users u ORDER BY u.display_name, u.email""")
        grants = self.query(
            """SELECT r.user_id, r.entity_id, r.role, r.granted_ts,
                      e.name AS entity_name, g.display_name AS granted_by
               FROM user_entity_role r
               JOIN entity e ON e.id = r.entity_id
               LEFT JOIN users g ON g.id = r.granted_by
               ORDER BY r.entity_id, r.role""")
        by_user = {}
        for grant in grants:
            by_user.setdefault(grant["user_id"], []).append(dict(grant))
        return [dict(row, roles=by_user.get(row["id"], [])) for row in rows]

    def admins_on(self, entity_id):
        return [r["user_id"] for r in self.query(
            """SELECT r.user_id FROM user_entity_role r
               JOIN users u ON u.id = r.user_id
               WHERE r.entity_id = ? AND r.role = 'admin' AND u.is_active = 1""",
            (entity_id,))]

    def roles_for(self, user_id):
        """Resolved from the DB on EVERY request. A token is never a bag of
        permissions, so a role edit applies on the next click (§9)."""
        return self.query(
            "SELECT entity_id, role FROM user_entity_role WHERE user_id = ?",
            (user_id,))

    # ----------------------------------------------------------- projects
    @staticmethod
    def _issue_job_number(c):
        """Allocate the next number from the platform's RESERVED RANGE.

        Allocated inside the caller's transaction, at commit time -- never on
        opening a form, or every abandoned form leaves a gap.

        Refuses unless a range has been configured. iTrade still issues from
        the general series, so drawing from it here would eventually hand out
        a number iTrade also hands out, and the collision would surface only
        when both reached Xero (ADR-29). No agreed range, no issuing: the
        safe state is the default state, not something to remember.
        """
        row = c.execute(
            "SELECT next_value, range_start, range_end "
            "FROM job_number_sequence WHERE id = 1").fetchone()
        if row["range_start"] is None or row["range_end"] is None:
            raise JobNumberError(
                "no job-number range is reserved for this platform, so "
                "allocating one could collide with iTrade. Record the code "
                "iTrade gave you instead, or reserve a range first "
                "(tools/job_number_range.py).")
        n = row["next_value"]
        if n < row["range_start"] or n > row["range_end"]:
            raise JobNumberError(
                f"the reserved range JN-{row['range_start']}..JN-{row['range_end']} "
                "is exhausted; reserve another block before issuing again")
        c.execute("UPDATE job_number_sequence SET next_value = ? WHERE id = 1",
                  (n + 1,))
        return f"JN-{n}"

    def reserve_job_number_range(self, start, end, note, actor_id):
        """Agree a block this platform owns exclusively.

        Refuses to overlap anything already in use, because a range that
        contains an existing code is not reserved -- it is a collision
        waiting for someone to allocate into it.
        """
        if start > end:
            raise ValueError("start must not be after end")
        now = int(time.time())
        with self._tx() as c:
            used = c.execute(
                """SELECT job_code, name FROM project
                   WHERE job_code GLOB 'JN-[0-9]*'
                     AND CAST(substr(job_code, 4) AS INTEGER) BETWEEN ? AND ?
                   ORDER BY job_code LIMIT 1""", (start, end)).fetchone()
            if used is not None:
                raise ValueError(
                    f"{used['job_code']} ({used['name']}) already sits inside "
                    f"JN-{start}..JN-{end}; pick a block nothing uses")
            c.execute(
                """UPDATE job_number_sequence
                   SET range_start = ?, range_end = ?, range_note = ?,
                       next_value = ?
                   WHERE id = 1""", (start, end, note, start))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'job_range_reserve','job_number_sequence','1',?)""",
                (now, actor_id, f"JN-{start}..JN-{end}: {note}"))
            return {"range_start": start, "range_end": end, "note": note,
                    "next_value": start}

    def job_number_range(self):
        return self.query_one(
            "SELECT next_value, range_start, range_end, range_note "
            "FROM job_number_sequence WHERE id = 1")

    def job_code_in_use(self, code, exclude_project_id=None):
        """The other project holding this code, or None.

        Placeholders are exempt: several projects legitimately sit on TBA at
        once, which is what it means.
        """
        if (code or "").upper() in self.PLACEHOLDER_CODES:
            return None
        return self.query_one(
            "SELECT id, name FROM project WHERE job_code = ? AND id IS NOT ?",
            (code, exclude_project_id))

    def next_job_number(self):
        with self._tx() as c:
            return self._issue_job_number(c)

    @staticmethod
    def client_key(name):
        """Normalised match key: lowercase, alphanumerics only.

        Free-text entry is how a register acquires 'MSquared', 'M Squared'
        and 'M-Squared' as three clients that are one client -- which then
        splits the by-client rollup and is painful to unpick once invoices
        reference all three. Collapsing punctuation and case is aggressive,
        and deliberately so: two genuinely different clients distinguished
        only by a hyphen is a far rarer problem than the one it prevents.
        """
        return "".join(ch for ch in (name or "").lower() if ch.isalnum())

    def resolve_client(self, entity_id, name, actor_id):
        """Find or create, matching on the normalised key.

        Returns (client_id, created, matched_name). `matched_name` is the
        EXISTING spelling when a near-miss was reused, so the caller can say
        so rather than silently correcting what the user typed.
        """
        name = (name or "").strip()
        if not name:
            return None, False, None
        key = self.client_key(name)
        for row in self.query(
                "SELECT id, name FROM client WHERE entity_id = ?", (entity_id,)):
            if self.client_key(row["name"]) == key:
                return row["id"], False, row["name"]
        now = int(time.time())
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO client (entity_id, name) VALUES (?, ?)",
                (entity_id, name))
            cid = cur.lastrowid
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'client_create','client',?,?)""",
                (now, actor_id, str(cid), name))
        return cid, True, name

    def create_project(self, fields, actor_id):
        """Returns the created row. The job number is allocated here, in the
        same transaction, so a failed insert cannot burn one."""
        now = int(time.time())
        with self._tx() as c:
            job_code = fields.get("job_code") or self._issue_job_number(c)
            cur = c.execute(
                """INSERT INTO project
                   (entity_id, name, job_code, project_no, client_id, type_id,
                    status, project_lead, purchase_order_cents,
                    invoiced_prior_cents, needs_resolution, notes, created_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)""",
                (fields["entity_id"], fields["name"], job_code,
                 fields.get("project_no"), fields["client_id"],
                 fields["type_id"], fields["status"], fields["project_lead"],
                 fields.get("purchase_order_cents", 0),
                 fields.get("invoiced_prior_cents", 0),
                 fields.get("notes"), now))
            # lastrowid lives on the CURSOR, not the connection that _tx()
            # yields.
            pid = cur.lastrowid
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'project_create','project',?,?)""",
                (now, actor_id, str(pid), f"{job_code} {fields['name']}"))
            # The contract value is the project's own figure now (migration
            # 007), not the sum of its orders: on a job where POs arrive
            # progressively the contract exists long before the orders do.
            c.execute(
                "UPDATE project SET contract_value_cents = ? WHERE id = ?",
                (fields.get("purchase_order_cents", 0), pid))

            # EXPAND-WINDOW DUAL WRITE (§4, ADR-25).
            #
            # project.purchase_order_cents is still read by the previous
            # release, so it keeps being written until the contraction
            # migration removes it. But v_project_orders_in_hand now reads
            # customer_po, so the PO row is what actually counts. Writing
            # only one of the two is how a project ends up worth nothing.
            #
            # This is temporary and ugly on purpose. It disappears at
            # contraction, one release after 003 has been stable.
            # A placeholder, not an order: it exists so claims and
            # retention have something to hang from until the customer
            # actually raises a PO.
            po_cents = fields.get("purchase_order_cents", 0)
            if po_cents:
                c.execute(
                    """INSERT INTO customer_po
                       (entity_id, project_id, po_number, amount_cents,
                        note, is_placeholder, created_by, created_ts)
                       VALUES (?,?,?,?,?,1,?,?)""",
                    (fields["entity_id"], pid, fields.get("po_number"),
                     po_cents, fields.get("po_note") or "contract value, no order yet",
                     actor_id, now))
            prior_cents = fields.get("invoiced_prior_cents", 0)
            if prior_cents:
                c.execute(
                    """INSERT INTO claim_line
                       (entity_id, project_id, customer_po_id, status,
                        amount_cents, detail, claim_date, invoiced_date,
                        is_opening_balance, created_by, created_ts)
                       VALUES (?,?,NULL,'invoiced',?,?,?,?,1,?,?)""",
                    (fields["entity_id"], pid, prior_cents,
                     "opening balance: invoiced before FY27",
                     "2026-06-30", "2026-06-30", actor_id, now))

            # A project created WITHOUT a number is not an error -- it is a
            # decision deferred. Put it on the worklist so the deferral is
            # visible instead of being a blank cell nobody revisits.
            if job_code.upper() in self.PLACEHOLDER_CODES:
                c.execute(
                    """INSERT INTO job_code_issue
                       (raw_code, class, project_id, created_ts)
                       VALUES (?,'B',?,?)""", (job_code, pid, now))
                c.execute(
                    "UPDATE project SET needs_resolution = 1 WHERE id = ?",
                    (pid,))
            row = c.execute("SELECT * FROM project WHERE id = ?", (pid,)).fetchone()
            return dict(row)

    # Fields a user may change. job_code, entity_id and created_ts are not
    # here: reassigning a job number or moving a project between legal
    # entities is a migration, not an edit.
    MUTABLE = ("name", "project_no", "client_id", "type_id", "status",
               "project_lead", "purchase_order_cents", "invoiced_prior_cents",
               "notes")

    def update_project(self, project_id, changes, actor_id):
        """Field-level patch: only the keys supplied are touched.

        Two people editing different fields of one project therefore do not
        collide at all, which is the common case. Same-field edits are
        last-write-wins, and every change lands in audit_log with its old and
        new value, so a lost update is detectable after the fact rather than
        silent. (Proper optimistic locking needs a version column and a
        migration; not worth one at 10 users until it actually bites.)
        """
        now = int(time.time())
        with self._tx() as c:
            before = c.execute(
                "SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
            if before is None:
                return None
            applied = {}
            for key in self.MUTABLE:
                if key not in changes:
                    continue
                old_value = before[key]
                new_value = changes[key]
                if old_value == new_value:
                    continue
                c.execute(f"UPDATE project SET {key} = ? WHERE id = ?",
                          (new_value, project_id))
                applied[key] = (old_value, new_value)
                c.execute(
                    """INSERT INTO audit_log (ts, actor_user_id, action,
                           target_type, target_id, detail)
                       VALUES (?,?,'project_update','project',?,?)""",
                    (now, actor_id, str(project_id),
                     f"{key}: {old_value!r} -> {new_value!r}"))
            # The contract value lives on the project. Its placeholder PO
            # tracks it so retention (a percentage OF the contract) and any
            # claims still attached keep working.
            if "purchase_order_cents" in applied:
                _o, new_contract = applied["purchase_order_cents"]
                c.execute(
                    "UPDATE project SET contract_value_cents = ? WHERE id = ?",
                    (new_contract, project_id))
            if "purchase_order_cents" in applied:
                _old_v, new_v = applied["purchase_order_cents"]
                po = c.execute(
                    """SELECT id, amount_cents FROM customer_po
                       WHERE project_id = ? AND is_placeholder = 1
                       ORDER BY id LIMIT 1""", (project_id,)).fetchone()
                if po is None:
                    c.execute(
                        """INSERT INTO customer_po
                           (entity_id, project_id, amount_cents, note,
                            created_by, created_ts)
                           SELECT entity_id, id, ?, 'contract value, no order yet',
                                  ?, ?
                           FROM project WHERE id = ?""",
                        (new_v, actor_id, now, project_id))
                else:
                    c.execute(
                        "UPDATE customer_po SET amount_cents = ? WHERE id = ?",
                        (new_v, po["id"]))
                    c.execute(
                        """INSERT INTO customer_po_revision
                           (customer_po_id, field, old_value, new_value,
                            reason, changed_by, changed_ts)
                           VALUES (?,'amount_cents',?,?,?,?,?)""",
                        (po["id"], str(po["amount_cents"]), str(new_v),
                         "contract value edited on the project", actor_id, now))
            row = c.execute(
                "SELECT * FROM project WHERE id = ?", (project_id,)).fetchone()
            return {"project": dict(row), "changed": sorted(applied)}

    # ---------------------------------------------------------- customer POs
    PO_KINDS = ("variation", "correction")

    def create_customer_po(self, fields, actor_id):
        """A new order. Not a change to an existing one: separate scope, its
        own number, and its own retention terms or none."""
        now = int(time.time())
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO customer_po
                   (entity_id, project_id, po_number, amount_cents,
                    issued_date, note, retention_applies, retention_rate_bp,
                    retention_cap_bp, release_policy, release_split_bp,
                    created_by, created_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fields["entity_id"], fields["project_id"],
                 fields.get("po_number"), fields["amount_cents"],
                 fields.get("issued_date"), fields.get("note"),
                 fields.get("retention_applies", 0),
                 fields.get("retention_rate_bp"),
                 fields.get("retention_cap_bp"),
                 fields.get("release_policy"),
                 fields.get("release_split_bp"), actor_id, now))
            po_id = cur.lastrowid
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'po_create','customer_po',?,?)""",
                (now, actor_id, str(po_id),
                 f"{fields.get('po_number') or '(no number)'} "
                 f"{money.format(fields['amount_cents'])}"))
            return dict(c.execute("SELECT * FROM customer_po WHERE id = ?",
                                  (po_id,)).fetchone())

    def revise_customer_po(self, po_id, new_amount, kind, reason,
                           effective_date, actor_id):
        """Change a PO's value, saying WHY.

        `variation` — the contract became bigger (or smaller) on a date. The
        figures before that date were right.
        `correction` — the recorded value was wrong. The figures before were
        wrong too, and correcting it changes what they should have said.

        The distinction cannot be recovered later from the numbers, which is
        why it is required rather than inferred.
        """
        if kind not in self.PO_KINDS:
            raise ValueError(f"kind must be one of {', '.join(self.PO_KINDS)}")
        if not (reason or "").strip():
            raise ValueError("a reason is required")
        if kind == "variation" and not (effective_date or "").strip():
            raise ValueError("a variation needs the date the contract changed")
        now = int(time.time())
        with self._tx() as c:
            before = c.execute("SELECT * FROM customer_po WHERE id = ?",
                               (po_id,)).fetchone()
            if before is None:
                return None
            if before["amount_cents"] == new_amount:
                return {"changed": False, "amount_cents": new_amount}
            c.execute("UPDATE customer_po SET amount_cents = ? WHERE id = ?",
                      (new_amount, po_id))
            c.execute(
                """INSERT INTO customer_po_revision
                   (customer_po_id, field, old_value, new_value, reason,
                    kind, effective_date, changed_by, changed_ts)
                   VALUES (?,'amount_cents',?,?,?,?,?,?,?)""",
                (po_id, str(before["amount_cents"]), str(new_amount), reason,
                 kind, (effective_date or None), actor_id, now))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'po_revise','customer_po',?,?)""",
                (now, actor_id, str(po_id),
                 f"{kind}: {money.format(before['amount_cents'])} -> "
                 f"{money.format(new_amount)} ({reason})"))
            return {"changed": True, "kind": kind,
                    "from": before["amount_cents"], "amount_cents": new_amount}

    def update_customer_po(self, po_id, changes, actor_id):
        """Everything except the amount, which needs `revise` and a reason."""
        editable = ("po_number", "issued_date", "note", "retention_applies",
                    "retention_rate_bp", "retention_cap_bp", "release_policy",
                    "release_split_bp")
        now = int(time.time())
        with self._tx() as c:
            before = c.execute("SELECT * FROM customer_po WHERE id = ?",
                               (po_id,)).fetchone()
            if before is None:
                return None
            applied = []
            for key in editable:
                if key not in changes or before[key] == changes[key]:
                    continue
                c.execute(f"UPDATE customer_po SET {key} = ? WHERE id = ?",
                          (changes[key], po_id))
                applied.append(key)
                c.execute(
                    """INSERT INTO audit_log (ts, actor_user_id, action,
                           target_type, target_id, detail)
                       VALUES (?,?,'po_update','customer_po',?,?)""",
                    (now, actor_id, str(po_id),
                     f"{key}: {before[key]!r} -> {changes[key]!r}"))
            return {"po": dict(c.execute(
                "SELECT * FROM customer_po WHERE id = ?", (po_id,)).fetchone()),
                "changed": sorted(applied)}

    def customer_po_is_movable(self, po_id):
        """A PO with claims cannot move on its own.

        A claim carries both `project_id` and `customer_po_id`. Moving the
        order without its claims would leave the two disagreeing about which
        project the work belongs to -- and moving the claims as well is a
        different, larger operation that should be asked for explicitly
        rather than happening as a side effect.
        """
        row = self.query_one(
            "SELECT claim_count FROM v_customer_po_history WHERE customer_po_id = ?",
            (po_id,))
        if row is None:
            return "no such PO"
        if row["claim_count"]:
            return (f"{row['claim_count']} claim(s) are billed against this "
                    "order; move or re-point them first")
        return None

    def move_customer_po(self, po_id, project_id, actor_id):
        now = int(time.time())
        with self._tx() as c:
            po = c.execute(
                """SELECT po.po_number, po.amount_cents, po.project_id,
                          po.entity_id, p.name AS from_name
                   FROM customer_po po JOIN project p ON p.id = po.project_id
                   WHERE po.id = ?""", (po_id,)).fetchone()
            target = c.execute(
                "SELECT id, name, entity_id FROM project WHERE id = ?",
                (project_id,)).fetchone()
            if po is None or target is None:
                return None
            if target["entity_id"] != po["entity_id"]:
                # Entities are separate legal companies; an order does not
                # cross between them by being dragged.
                raise ValueError("that project belongs to a different entity")
            c.execute("UPDATE customer_po SET project_id = ? WHERE id = ?",
                      (project_id, po_id))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'po_move','customer_po',?,?)""",
                (now, actor_id, str(po_id),
                 f"{po['po_number'] or '(no number)'} "
                 f"{money.format(po['amount_cents'])}: "
                 f"{po['from_name']} -> {target['name']}"))
            return {"from": po["from_name"], "to": target["name"],
                    "po_number": po["po_number"],
                    "amount_cents": po["amount_cents"]}

    def customer_po_is_deletable(self, po_id):
        """A PO with claims against it is history. Returns the reason it may
        not be removed, or None."""
        row = self.query_one(
            "SELECT claim_count FROM v_customer_po_history WHERE customer_po_id = ?",
            (po_id,))
        if row is None:
            return "no such PO"
        if row["claim_count"]:
            return (f"{row['claim_count']} claim(s) are billed against this "
                    "order; it cannot be removed")
        return None

    def delete_customer_po(self, po_id, actor_id):
        now = int(time.time())
        with self._tx() as c:
            row = c.execute(
                "SELECT po_number, amount_cents FROM customer_po WHERE id = ?",
                (po_id,)).fetchone()
            if row is None:
                return False
            c.execute("DELETE FROM customer_po_revision WHERE customer_po_id = ?",
                      (po_id,))
            c.execute("DELETE FROM customer_po WHERE id = ?", (po_id,))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'po_delete','customer_po',?,?)""",
                (now, actor_id, str(po_id),
                 f"{row['po_number'] or '(no number)'} "
                 f"{money.format(row['amount_cents'])}"))
            return True

    # ------------------------------------------------------- claim lines
    def create_claim_line(self, fields, actor_id):
        now = int(time.time())
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO claim_line
                   (entity_id, project_id, customer_po_id, period_id, status,
                    amount_cents, percent_bp, phase, task, detail, reference,
                    claim_date, retention_cents, is_retention_release,
                    created_by, created_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fields["entity_id"], fields["project_id"],
                 fields.get("customer_po_id"), fields.get("period_id"),
                 fields.get("status", "forecast"), fields["amount_cents"],
                 fields.get("percent_bp"), fields.get("phase"),
                 fields.get("task"), fields.get("detail"),
                 fields.get("reference"), fields.get("claim_date"),
                 0, fields.get("is_retention_release", 0), actor_id, now))
            cid = cur.lastrowid
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'claim_create','claim_line',?,?)""",
                (now, actor_id, str(cid),
                 f"{fields.get('status','forecast')} {fields['amount_cents']}c"))
            return dict(c.execute("SELECT * FROM claim_line WHERE id = ?",
                                  (cid,)).fetchone())

    CLAIM_MUTABLE = ("customer_po_id", "period_id", "amount_cents",
                     "percent_bp", "phase", "task", "detail", "reference",
                     "claim_date", "approved_date", "invoice_number",
                     "invoiced_date", "paid_date")

    def update_claim_line(self, claim_id, changes, actor_id, reason=None):
        """Field-level, with every money- or timing-bearing change recorded.

        A change of `period_id` is SLIPPAGE and is recorded as such. Silently
        overwriting the month makes a forecast look like it was always right,
        which is precisely the number forecasting accuracy is measured
        against.
        """
        now = int(time.time())
        with self._tx() as c:
            before = c.execute("SELECT * FROM claim_line WHERE id = ?",
                               (claim_id,)).fetchone()
            if before is None:
                return None
            applied = {}
            for key in self.CLAIM_MUTABLE:
                if key not in changes:
                    continue
                old_value, new_value = before[key], changes[key]
                if old_value == new_value:
                    continue
                c.execute(f"UPDATE claim_line SET {key} = ? WHERE id = ?",
                          (new_value, claim_id))
                applied[key] = (old_value, new_value)
                c.execute(
                    """INSERT INTO claim_line_revision
                       (claim_line_id, field, old_value, new_value, reason,
                        changed_by, changed_ts)
                       VALUES (?,?,?,?,?,?,?)""",
                    (claim_id, key,
                     None if old_value is None else str(old_value),
                     None if new_value is None else str(new_value),
                     reason, actor_id, now))
            return {"claim": dict(c.execute(
                "SELECT * FROM claim_line WHERE id = ?", (claim_id,)).fetchone()),
                "changed": sorted(applied)}

    def transition_claim(self, claim_id, to_status, fields, reason, actor_id):
        """Move a claim along its lifecycle, recording the move.

        Retention is RECOMPUTED at the moment of invoicing, not carried from
        creation. Two forecasts each computed against the same remaining
        capacity would both take the full 10%, and together exceed the cap
        the moment both were invoiced. Only invoicing withholds anything, so
        that is where the figure is fixed.
        """
        now = int(time.time())
        with self._tx() as c:
            before = c.execute("SELECT * FROM claim_line WHERE id = ?",
                               (claim_id,)).fetchone()
            if before is None:
                return None
            from_status = before["status"]
            sets, params = ["status = ?"], [to_status]
            for key in ("approved_date", "invoice_number", "invoiced_date",
                        "paid_date"):
                if key in fields:
                    sets.append(f"{key} = ?")
                    params.append(fields[key])

            retention = before["retention_cents"]
            if to_status == "invoiced" and not before["is_retention_release"]:
                retention = self.retention_for_claim(
                    before["customer_po_id"], before["amount_cents"],
                    exclude_claim_id=claim_id) if before["customer_po_id"] else 0
                sets.append("retention_cents = ?")
                params.append(retention)
            elif to_status in ("forecast", "due", "approved", "cancelled"):
                # Stepping back out of invoiced releases the withholding: the
                # customer is not holding money against an invoice that no
                # longer exists.
                sets.append("retention_cents = ?")
                params.append(0)
                retention = 0

            params.append(claim_id)
            c.execute(f"UPDATE claim_line SET {', '.join(sets)} WHERE id = ?",
                      params)
            c.execute(
                """INSERT INTO claim_line_revision
                   (claim_line_id, field, old_value, new_value, reason,
                    changed_by, changed_ts)
                   VALUES (?,'status',?,?,?,?,?)""",
                (claim_id, from_status, to_status, reason, actor_id, now))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'claim_status','claim_line',?,?)""",
                (now, actor_id, str(claim_id),
                 f"{from_status} -> {to_status}"
                 + (f": {reason}" if reason else "")))
            return {"claim": dict(c.execute(
                "SELECT * FROM claim_line WHERE id = ?", (claim_id,)).fetchone()),
                "from": from_status, "to": to_status,
                "retention_cents": retention}

    # -------------------------------------------------------- claim plan
    ITEM_MUTABLE = ("name", "value_cents", "sequence", "note", "is_variation")

    def create_claim_item(self, fields, actor_id):
        now = int(time.time())
        with self._tx() as c:
            nxt = c.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM claim_item "
                "WHERE project_id = ?", (fields["project_id"],)).fetchone()[0]
            cur = c.execute(
                """INSERT INTO claim_item (entity_id, project_id, name,
                       value_cents, is_variation, sequence, note,
                       created_by, created_ts)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (fields["entity_id"], fields["project_id"], fields["name"],
                 fields["value_cents"], fields.get("is_variation", 0),
                 fields.get("sequence", nxt), fields.get("note"),
                 actor_id, now))
            item_id = cur.lastrowid
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'claim_item_create','claim_item',?,?)""",
                (now, actor_id, str(item_id),
                 f"{fields['name']} {money.format(fields['value_cents'])}"))
            return dict(c.execute("SELECT * FROM claim_item WHERE id = ?",
                                  (item_id,)).fetchone())

    def update_claim_item(self, item_id, changes, actor_id):
        now = int(time.time())
        with self._tx() as c:
            before = c.execute("SELECT * FROM claim_item WHERE id = ?",
                               (item_id,)).fetchone()
            if before is None:
                return None
            applied = []
            for key in self.ITEM_MUTABLE:
                if key not in changes or before[key] == changes[key]:
                    continue
                c.execute(f"UPDATE claim_item SET {key} = ? WHERE id = ?",
                          (changes[key], item_id))
                applied.append(key)
                c.execute(
                    """INSERT INTO audit_log (ts, actor_user_id, action,
                           target_type, target_id, detail)
                       VALUES (?,?,'claim_item_update','claim_item',?,?)""",
                    (now, actor_id, str(item_id),
                     f"{key}: {before[key]!r} -> {changes[key]!r}"))
            return {"item": dict(c.execute(
                "SELECT * FROM claim_item WHERE id = ?", (item_id,)).fetchone()),
                "changed": sorted(applied)}

    def claim_item_is_deletable(self, item_id):
        locked = self.scalar(
            """SELECT COUNT(*) FROM claim_allocation
               WHERE claim_item_id = ? AND locked_claim_id IS NOT NULL""",
            (item_id,))
        if locked:
            return (f"{locked} of its months have been invoiced; the plan "
                    "behind an issued invoice is history")
        return None

    def delete_claim_item(self, item_id, actor_id):
        now = int(time.time())
        with self._tx() as c:
            row = c.execute("SELECT name, value_cents FROM claim_item "
                            "WHERE id = ?", (item_id,)).fetchone()
            if row is None:
                return False
            c.execute("DELETE FROM claim_allocation WHERE claim_item_id = ?",
                      (item_id,))
            c.execute("DELETE FROM claim_item WHERE id = ?", (item_id,))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'claim_item_delete','claim_item',?,?)""",
                (now, actor_id, str(item_id),
                 f"{row['name']} {money.format(row['value_cents'])}"))
            return True

    def set_allocation(self, item_id, period_id, percent_bp, amount_cents,
                       actor_id, note=None):
        """One item's share of one month.

        The AMOUNT is the fact and the percentage is how it was expressed --
        `33.33%` of $79,444 is $26,478.69 while the agreed figure was
        $26,481.33, a third displayed rounded. Deriving one from the other
        at read time would move money.

        Setting an amount of zero removes the allocation: a month an item no
        longer contributes to should not linger as a row saying nothing.
        """
        now = int(time.time())
        with self._tx() as c:
            existing = c.execute(
                """SELECT id, amount_cents, locked_claim_id FROM claim_allocation
                   WHERE claim_item_id = ? AND period_id = ?""",
                (item_id, period_id)).fetchone()
            if existing and existing["locked_claim_id"] is not None:
                raise ValueError(
                    "that month has been invoiced; amend the claim instead")
            if not amount_cents:
                if existing:
                    c.execute("DELETE FROM claim_allocation WHERE id = ?",
                              (existing["id"],))
                return {"removed": bool(existing)}
            if existing:
                c.execute(
                    """UPDATE claim_allocation
                       SET percent_bp = ?, amount_cents = ?, note = ?
                       WHERE id = ?""",
                    (percent_bp, amount_cents, note, existing["id"]))
                allocation_id = existing["id"]
            else:
                cur = c.execute(
                    """INSERT INTO claim_allocation (claim_item_id, period_id,
                           percent_bp, amount_cents, note, created_by, created_ts)
                       VALUES (?,?,?,?,?,?,?)""",
                    (item_id, period_id, percent_bp, amount_cents, note,
                     actor_id, now))
                allocation_id = cur.lastrowid
            return {"id": allocation_id, "amount_cents": amount_cents}

    def generate_plan_claims(self, project_id, actor_id, customer_po_id=None):
        """Turn the plan into claims. IDEMPOTENT, and it never touches a
        month that has been invoiced.

        AT MOST one claim per project per month: a month with no allocations
        produces nothing rather than a zero row, which is what the workbook
        does when a project is not claimed that month.

        The detail reads the way the workbook writes it -- `Project
        Management, 10% $3,350 - Design / Engineering, 25% $7,512.50` -- so
        the claim says what it is made of rather than presenting a total
        nobody can decompose.
        """
        now = int(time.time())
        project = self.query_one(
            "SELECT entity_id FROM project WHERE id = ?", (project_id,))
        if project is None:
            return {"created": 0, "updated": 0, "locked": 0, "months": []}
        po_id = customer_po_id or self.scalar(
            "SELECT id FROM customer_po WHERE project_id = ? ORDER BY id LIMIT 1",
            (project_id,))
        if po_id is None:
            # A claim has to bill against something. On a job where orders
            # arrive as the work does there may be none yet, so a
            # placeholder carries the claims until a real one is raised --
            # the same device the claims importer uses, and excluded from
            # `ordered` for the same reason.
            with self._tx() as c:
                cur = c.execute(
                    """INSERT INTO customer_po (entity_id, project_id,
                           amount_cents, note, is_placeholder, created_by,
                           created_ts)
                       VALUES (?,?,0,?,1,?,?)""",
                    (project["entity_id"], project_id,
                     "placeholder: planned claims with no order raised yet",
                     actor_id, now))
                po_id = cur.lastrowid
        created, updated, locked, touched = 0, 0, 0, []
        for allocation in self.query(
                """SELECT a.id, a.period_id, a.amount_cents, a.percent_bp,
                          a.note, a.claim_line_id, a.locked_claim_id,
                          i.name AS item_name, pe.label
                   FROM claim_allocation a
                   JOIN claim_item i ON i.id = a.claim_item_id
                   JOIN period pe ON pe.id = a.period_id
                   WHERE i.project_id = ?
                   ORDER BY pe.month_start, i.sequence, i.id""",
                (project_id,)):
            if allocation["locked_claim_id"] is not None:
                locked += 1
                continue
            # The detail says what the claim IS -- the item, its share, and
            # whatever the workbook called the task. A total nobody can
            # decompose is a total nobody can check.
            detail = f"{allocation['item_name']}"
            if allocation["percent_bp"]:
                detail += f", {money.format_rate(allocation['percent_bp'])}"
            if allocation["note"]:
                detail += f" \u2014 {allocation['note']}"
            existing = None
            if allocation["claim_line_id"]:
                existing = self.query_one(
                    "SELECT id, status, period_id FROM claim_line WHERE id = ?",
                    (allocation["claim_line_id"],))
            with self._tx() as c:
                if existing is None:
                    cur = c.execute(
                        """INSERT INTO claim_line (entity_id, project_id,
                               customer_po_id, period_id, status, amount_cents,
                               detail, from_plan, created_by, created_ts)
                           VALUES (?,?,?,?, 'forecast', ?,?,1,?,?)""",
                        (project["entity_id"], project_id, po_id,
                         allocation["period_id"], allocation["amount_cents"],
                         detail, actor_id, now))
                    c.execute(
                        "UPDATE claim_allocation SET claim_line_id = ? WHERE id = ?",
                        (cur.lastrowid, allocation["id"]))
                    created += 1
                elif existing["status"] in ("invoiced", "paid"):
                    locked += 1
                    continue
                else:
                    c.execute(
                        """UPDATE claim_line
                           SET amount_cents = ?, period_id = ?, detail = ?
                           WHERE id = ?""",
                        (allocation["amount_cents"], allocation["period_id"],
                         detail, existing["id"]))
                    updated += 1
            if allocation["label"] not in touched:
                touched.append(allocation["label"])
        if created or updated:
            with self._tx() as c:
                c.execute(
                    """INSERT INTO audit_log (ts, actor_user_id, action,
                           target_type, target_id, detail)
                       VALUES (?,?,'plan_generate','project',?,?)""",
                    (now, actor_id, str(project_id),
                     f"{created} created, {updated} updated: "
                     f"{', '.join(touched)}"))
        return {"created": created, "updated": updated, "locked": locked,
                "months": touched}

    def move_plan_allocations(self, claim_id, from_period, to_period, actor_id):
        """Follow a plan-backed claim when it moves month.

        Only the allocation that OWNS this claim moves. Moving every
        allocation of the month took four other claims' worth with it --
        `200 Victoria` has five Commissioning claims in Sep-26, and moving
        one of them relocated the whole $88,500.

        A locked allocation does not move: that month was invoiced, and its
        claim cannot be moved either.
        """
        now = int(time.time())
        with self._tx() as c:
            cur = c.execute(
                """UPDATE claim_allocation SET period_id = ?
                   WHERE claim_line_id = ? AND locked_claim_id IS NULL""",
                (to_period, claim_id))
            moved = cur.rowcount
            if moved:
                c.execute(
                    """INSERT INTO audit_log (ts, actor_user_id, action,
                           target_type, target_id, detail)
                       VALUES (?,?,'plan_follow','claim_line',?,?)""",
                    (now, actor_id, str(claim_id),
                     f"its allocation moved with it"))
            return moved

    def lock_plan_for_claim(self, claim_id, actor_id):
        """Fix the allocations behind a claim once it has been invoiced.

        Called when a plan-generated claim reaches `invoiced`. From then on
        re-spreading an item cannot move the months already billed -- the
        same boundary as the slippage rule, for the same reason.
        """
        now = int(time.time())
        claim = self.query_one(
            "SELECT project_id, period_id FROM claim_line WHERE id = ?",
            (claim_id,))
        if claim is None:
            return 0
        with self._tx() as c:
            cur = c.execute(
                """UPDATE claim_allocation SET locked_claim_id = ?
                   WHERE claim_line_id = ? AND locked_claim_id IS NULL""",
                (claim_id, claim_id))
            n = cur.rowcount
            if n:
                c.execute(
                    """INSERT INTO audit_log (ts, actor_user_id, action,
                           target_type, target_id, detail)
                       VALUES (?,?,'plan_lock','claim_line',?,?)""",
                    (now, actor_id, str(claim_id),
                     f"{n} allocation(s) fixed by invoicing"))
            return n

    def amend_invoiced_claim(self, claim_id, new_amount, reason, actor_id):
        """Change a claim that has already been invoiced. Rare, and real.

        The amendment records WHAT THE INVOICE SAID as well as what it says
        now: reconciling to Xero later means matching against the figure
        that was actually issued, not the one it was corrected to.
        """
        if not (reason or "").strip():
            raise ValueError("a reason is required")
        now = int(time.time())
        with self._tx() as c:
            claim = c.execute(
                "SELECT * FROM claim_line WHERE id = ?", (claim_id,)).fetchone()
            if claim is None:
                return None
            if claim["status"] not in ("invoiced", "paid"):
                raise ValueError("this claim has not been invoiced; edit it")
            if claim["amount_cents"] == new_amount:
                return {"changed": False, "amount_cents": new_amount}
            c.execute(
                """INSERT INTO claim_amendment (claim_line_id, invoice_number,
                       invoiced_cents, amended_cents, reason, amended_by,
                       amended_ts)
                   VALUES (?,?,?,?,?,?,?)""",
                (claim_id, claim["invoice_number"], claim["amount_cents"],
                 new_amount, reason.strip(), actor_id, now))
            c.execute("UPDATE claim_line SET amount_cents = ? WHERE id = ?",
                      (new_amount, claim_id))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'claim_amend','claim_line',?,?)""",
                (now, actor_id, str(claim_id),
                 f"invoice {claim['invoice_number'] or '(none)'} said "
                 f"{money.format(claim['amount_cents'])}, amended to "
                 f"{money.format(new_amount)}: {reason.strip()}"))
            return {"changed": True, "invoiced_cents": claim["amount_cents"],
                    "amount_cents": new_amount,
                    "invoice_number": claim["invoice_number"]}

    def set_claim_tasks(self, pairs, actor_id):
        """Fill in the workbook's line item on claims that arrived without
        one, because the importer mapped `Phase` and not `Task`.

        Audited as one entry rather than 106: it is a single correction to a
        single mistake, and a hundred rows in the log would bury the
        interesting entries either side of it.
        """
        now = int(time.time())
        if not pairs:
            return 0
        with self._tx() as c:
            for claim_id, task in pairs:
                c.execute(
                    "UPDATE claim_line SET task = ? WHERE id = ? AND task IS NULL",
                    (task, claim_id))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'claim_task_backfill','claim_line',?,?)""",
                (now, actor_id, "",
                 f"{len(pairs)} claim(s) given the workbook's task, which "
                 "the importer had folded into detail"))
        return len(pairs)

    def adopt_claims_into_plan(self, project_id, actor_id, rebuild=False):
        """Build the plan from the claims that already exist.

        Every project imported from the workbook arrived with its forecast
        already typed, and a plan panel that says `no plan yet` beside
        thirteen forecast claims is lying by omission. The claims carry the
        PHASE they came from, so the phases become items and the claims
        become allocations -- the plan describes what is already there
        rather than asking for it to be entered twice.

        Claims with no phase are gathered under one item named for the
        project, because an unnamed item is still better than a plan that
        pretends the money is not planned.

        The item's VALUE is the sum of its claims, not a share of the
        contract: what is planned is a fact, and whether it adds up to the
        contract is the question the panel then answers.
        """
        now = int(time.time())
        project = self.query_one(
            "SELECT entity_id, name FROM project WHERE id = ?", (project_id,))
        if project is None:
            return {"items": 0, "allocations": 0, "skipped": 0}
        existing = self.scalar(
            "SELECT COUNT(*) FROM claim_item WHERE project_id = ?", (project_id,))
        if existing and not rebuild:
            return {"items": 0, "allocations": 0, "skipped": 0,
                    "reason": "this project already has a plan"}
        if existing:
            # The plan is DERIVED from the claims, so rebuilding it loses
            # nothing that cannot be rebuilt -- including the locks, which
            # come from claim status either way. Anything typed by hand
            # goes, which is why this is a deliberate action.
            with self._tx() as c:
                c.execute(
                    """DELETE FROM claim_allocation WHERE claim_item_id IN
                       (SELECT id FROM claim_item WHERE project_id = ?)""",
                    (project_id,))
                c.execute("DELETE FROM claim_item WHERE project_id = ?",
                          (project_id,))
                # Opening balances are immutable, and they are not part of
                # any plan anyway -- touching them made the whole rebuild
                # fail on every project that has one.
                c.execute("UPDATE claim_line SET from_plan = 0 "
                          "WHERE project_id = ? AND is_opening_balance = 0",
                          (project_id,))
        claims = self.query(
            """SELECT id, period_id, amount_cents, percent_bp, phase, task,
                      status, detail
               FROM claim_line
               WHERE project_id = ? AND is_opening_balance = 0
                 AND period_id IS NOT NULL AND from_plan = 0
               ORDER BY id""", (project_id,))
        if not claims:
            return {"items": 0, "allocations": 0, "skipped": 0}
        skipped_zero = 0
        groups = {}
        for claim in claims:
            if not claim["amount_cents"]:
                # A zero row would become a zero item -- `Progress Claim #2`
                # at $0.00 is a month nobody claimed, not a part of the
                # contract.
                skipped_zero += 1
                continue
            # The workbook's LINE ITEM is the task -- `Client Training`,
            # `SAT`, `Design - Stage 2`. The phase groups them above. Using
            # the phase made five tasks share one row, so four of them were
            # invisible in the grid.
            groups.setdefault(
                self.plan_group_name(claim["task"] or claim["phase"],
                                     project["name"]),
                []).append(claim)
        items, allocations, skipped = 0, 0, 0
        for name, rows in groups.items():
            item = self.create_claim_item({
                "entity_id": project["entity_id"], "project_id": project_id,
                "name": name,
                "value_cents": sum(r["amount_cents"] for r in rows),
                "note": "adopted from claims imported with the register",
            }, actor_id)
            items += 1
            # ONE ALLOCATION PER CLAIM, and it owns that claim. Aggregating
            # several claims of a month into one share left generation
            # unable to say which claim it had produced: it updated one to
            # the month's whole total and left the rest standing.
            for row in rows:
                with self._tx() as c:
                    c.execute(
                        """INSERT INTO claim_allocation (claim_item_id,
                               period_id, percent_bp, amount_cents, note,
                               claim_line_id, locked_claim_id, created_by,
                               created_ts)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (item["id"], row["period_id"], row["percent_bp"] or 0,
                         row["amount_cents"], row["task"], row["id"],
                         # Already invoiced is fixed on adoption, not later:
                         # it was history before the plan existed.
                         row["id"] if row["status"] in ("invoiced", "paid")
                         else None,
                         actor_id, now))
                    c.execute("UPDATE claim_line SET from_plan = 1 WHERE id = ?",
                              (row["id"],))
                allocations += 1
        with self._tx() as c:
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'plan_adopt','project',?,?)""",
                (now, actor_id, str(project_id),
                 f"{items} item(s) and {allocations} allocation(s) built from "
                 f"{len(claims)} existing claims"))
        return {"items": items, "allocations": allocations,
                "skipped": skipped + skipped_zero}

    #: Phase values that are not phases. The workbook's `Phase` column is
    #: used inconsistently -- of 147 forecast rows, 70 hold a claim NUMBER
    #: (`Progress Claim #4`), 10 are blank, and `Monthly Claim` appears
    #: twice over through a typo. Grouping on those would produce an item
    #: per month rather than an item per part of the contract.
    NOT_A_PHASE = re.compile(
        r"^(progress claim|monthly claim|montly claim|claim|expected\b|tba)",
        re.IGNORECASE)

    A_MONTH = re.compile(r"^[A-Za-z]{3}[- ]?\d{2}$")

    @classmethod
    def plan_group_name(cls, phase, fallback):
        """Which item a claim belongs to. Newlines collapse: some rows hold
        a claim number and a phase in one cell."""
        text = " ".join((phase or "").split())
        if not text:
            return fallback
        if not cls.NOT_A_PHASE.match(text):
            return text
        # `Progress Claim #4 Deployment of the ISP` holds both: strip the
        # number and keep what is left, which IS a phase. Only when nothing
        # is left does the project name stand in.
        stripped = cls.NOT_A_PHASE.sub("", text, count=1)
        stripped = stripped.lstrip("#0123456789 -\u2013\u2014.:").strip()
        # A month is not a phase either: `Expected Aug-26` says WHEN, and
        # the allocation already carries that.
        if not stripped or cls.A_MONTH.match(stripped):
            return fallback
        return stripped

    def plan_health(self, project_id):
        """The three questions the workbooks answer by hand."""
        plan = self.query_one(
            "SELECT * FROM v_project_claim_plan WHERE project_id = ?",
            (project_id,))
        items = self.query(
            "SELECT * FROM v_claim_item_coverage WHERE project_id = ? "
            "ORDER BY is_variation, sequence, claim_item_id", (project_id,))
        return {
            "contract_value_cents": plan["contract_value_cents"] if plan else 0,
            "opening_balance_cents": plan["opening_balance_cents"] if plan else 0,
            # What a plan can describe: the contract less what was billed
            # before this platform's window opened.
            "plannable_cents": plan["plannable_cents"] if plan else 0,
            "item_value_cents": plan["item_value_cents"] if plan else 0,
            "variation_value_cents": plan["variation_value_cents"] if plan else 0,
            "unitemised_cents": plan["unitemised_cents"] if plan else 0,
            "allocated_cents": plan["allocated_cents"] if plan else 0,
            "items": [dict(i) for i in items],
            "months": [dict(m) for m in self.query(
                """SELECT m.*, pe.label, pe.month_start, pe.fy_label
                   FROM v_planned_month m JOIN period pe ON pe.id = m.period_id
                   WHERE m.project_id = ? ORDER BY pe.month_start""",
                (project_id,))],
        }

    # --------------------------------------------------------- schedules
    STEP_MONTHS = {"monthly": 1, "quarterly": 3, "annual": 12}

    def create_schedule(self, fields, actor_id):
        now = int(time.time())
        with self._tx() as c:
            cur = c.execute(
                """INSERT INTO claim_schedule
                   (entity_id, project_id, customer_po_id, description,
                    amount_cents, frequency, start_period_id, end_period_id,
                    renewal_date, renewal_notice_days, renewal_note,
                    created_by, created_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fields["entity_id"], fields["project_id"],
                 fields["customer_po_id"], fields["description"],
                 fields["amount_cents"], fields["frequency"],
                 fields["start_period_id"], fields["end_period_id"],
                 fields.get("renewal_date"),
                 fields.get("renewal_notice_days", 60),
                 fields.get("renewal_note"), actor_id, now))
            sid = cur.lastrowid
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'schedule_create','claim_schedule',?,?)""",
                (now, actor_id, str(sid),
                 f"{fields['description']} {fields['frequency']} "
                 f"{fields['amount_cents']}c"))
            return dict(c.execute("SELECT * FROM claim_schedule WHERE id = ?",
                                  (sid,)).fetchone())

    def schedule_periods(self, schedule_id):
        """The periods this schedule covers, stepping by its frequency.

        Quarterly means every third month FROM THE START, not calendar
        quarters -- a maintenance agreement beginning in August bills in
        August, November, February, May, and aligning it to Jan/Apr/Jul/Oct
        would invent a billing date nobody agreed to.
        """
        s = self.query_one("SELECT * FROM claim_schedule WHERE id = ?",
                           (schedule_id,))
        if s is None:
            return []
        step = self.STEP_MONTHS[s["frequency"]]
        rows = self.query(
            """SELECT p.id, p.label, p.month_start FROM period p
               WHERE p.month_start >= (SELECT month_start FROM period WHERE id = ?)
                 AND p.month_start <= (SELECT month_start FROM period WHERE id = ?)
               ORDER BY p.month_start""",
            (s["start_period_id"], s["end_period_id"]))
        return rows[::step]

    def generate_schedule_claims(self, schedule_id, actor_id):
        """Create the claims this schedule implies. IDEMPOTENT.

        A unique index on (schedule_id, period_id) means running it twice
        cannot produce a second November, so it is safe to run on a timer, on
        demand, or by accident.

        Generated claims are ordinary claims: individually editable, able to
        slip, invoiced like anything else. Only their origin is recorded.
        """
        now = int(time.time())
        s = self.query_one("SELECT * FROM claim_schedule WHERE id = ?",
                           (schedule_id,))
        if s is None or not s["is_active"]:
            return {"created": 0, "existing": 0, "periods": []}
        created, existing, made = 0, 0, []
        for period in self.schedule_periods(schedule_id):
            already = self.query_one(
                "SELECT id FROM claim_line WHERE schedule_id = ? AND period_id = ?",
                (schedule_id, period["id"]))
            if already:
                existing += 1
                continue
            with self._tx() as c:
                c.execute(
                    """INSERT INTO claim_line
                       (entity_id, project_id, customer_po_id, period_id,
                        status, amount_cents, detail, schedule_id,
                        created_by, created_ts)
                       VALUES (?,?,?,?,'forecast',?,?,?,?,?)""",
                    (s["entity_id"], s["project_id"], s["customer_po_id"],
                     period["id"], s["amount_cents"], s["description"],
                     schedule_id, actor_id, now))
            created += 1
            made.append(period["label"])
        if created:
            with self._tx() as c:
                c.execute(
                    """INSERT INTO audit_log (ts, actor_user_id, action,
                           target_type, target_id, detail)
                       VALUES (?,?,'schedule_generate','claim_schedule',?,?)""",
                    (now, actor_id, str(schedule_id),
                     f"{created} claims: {', '.join(made)}"))
        return {"created": created, "existing": existing, "periods": made}

    SCHEDULE_MUTABLE = ("description", "amount_cents", "frequency",
                        "start_period_id", "end_period_id", "renewal_date",
                        "renewal_notice_days", "renewal_note", "is_active")

    def update_schedule(self, schedule_id, changes, actor_id):
        now = int(time.time())
        with self._tx() as c:
            before = c.execute("SELECT * FROM claim_schedule WHERE id = ?",
                               (schedule_id,)).fetchone()
            if before is None:
                return None
            applied = {}
            for key in self.SCHEDULE_MUTABLE:
                if key not in changes or before[key] == changes[key]:
                    continue
                c.execute(f"UPDATE claim_schedule SET {key} = ? WHERE id = ?",
                          (changes[key], schedule_id))
                applied[key] = (before[key], changes[key])
                c.execute(
                    """INSERT INTO audit_log (ts, actor_user_id, action,
                           target_type, target_id, detail)
                       VALUES (?,?,'schedule_update','claim_schedule',?,?)""",
                    (now, actor_id, str(schedule_id),
                     f"{key}: {before[key]!r} -> {changes[key]!r}"))
            return {"schedule": dict(c.execute(
                "SELECT * FROM claim_schedule WHERE id = ?",
                (schedule_id,)).fetchone()), "changed": sorted(applied)}

    def adopt_claims_into_schedule(self, schedule_id, actor_id):
        """Attach claims that already exist to the schedule that describes
        them.

        Every recurring project in the register arrived with twelve rows
        already typed. Generating over the top would double the year;
        adopting recognises that the schedule is a description of work
        already planned, not a source of new work.

        Amount differences are REPORTED, not corrected: a month where the
        maintenance charge differed is a fact about that month, and the
        schedule does not get to overwrite it.
        """
        now = int(time.time())
        schedule = self.query_one("SELECT * FROM claim_schedule WHERE id = ?",
                                  (schedule_id,))
        if schedule is None:
            return {"adopted": 0, "differing": [], "periods": [],
                    "not_adopted": []}
        adopted, differing, taken, extra = 0, [], [], []
        for period in self.schedule_periods(schedule_id):
            # ONE claim per period, because the unique index says so. A
            # project can legitimately hold two claims in a month -- 200
            # Victoria carries a $0.00 invoiced row alongside its monthly
            # maintenance in Jul-26 -- and adopting the second would breach
            # the constraint. Skip the period and report the leftovers: a
            # month with two claims is worth a human look, not a 500.
            if self.query_one(
                    """SELECT id FROM claim_line
                       WHERE schedule_id = ? AND period_id = ?""",
                    (schedule_id, period["id"])):
                spare = self.query(
                    """SELECT id, amount_cents FROM claim_line
                       WHERE project_id = ? AND period_id = ?
                         AND schedule_id IS NULL AND is_opening_balance = 0""",
                    (schedule["project_id"], period["id"]))
                for other in spare:
                    extra.append({"period": period["label"],
                                  "claim_cents": other["amount_cents"]})
                continue
            row = self.query_one(
                """SELECT id, amount_cents FROM claim_line
                   WHERE project_id = ? AND period_id = ?
                     AND schedule_id IS NULL AND is_opening_balance = 0
                   ORDER BY id LIMIT 1""",
                (schedule["project_id"], period["id"]))
            if row is None:
                continue
            with self._tx() as c:
                c.execute("UPDATE claim_line SET schedule_id = ? WHERE id = ?",
                          (schedule_id, row["id"]))
            adopted += 1
            taken.append(period["label"])
            if row["amount_cents"] != schedule["amount_cents"]:
                differing.append({"period": period["label"],
                                  "claim_cents": row["amount_cents"],
                                  "schedule_cents": schedule["amount_cents"]})
        if adopted:
            with self._tx() as c:
                c.execute(
                    """INSERT INTO audit_log (ts, actor_user_id, action,
                           target_type, target_id, detail)
                       VALUES (?,?,'schedule_adopt','claim_schedule',?,?)""",
                    (now, actor_id, str(schedule_id),
                     f"{adopted} existing claims adopted: {', '.join(taken)}"))
        return {"adopted": adopted, "differing": differing,
                "periods": taken, "not_adopted": extra}

    def upcoming_renewals(self, entity_ids=None, within_days=None):
        """Renewals that need attention, soonest first.

        Overdue ones sort first and stay in the list. A maintenance
        agreement that lapsed last month is more urgent than one due next
        month, and dropping it off the end is how revenue quietly stops.
        """
        sql = "SELECT * FROM v_upcoming_renewals WHERE renewal_date IS NOT NULL"
        params: list[Any] = []
        if entity_ids:
            sql += f" AND entity_id IN ({','.join('?' * len(entity_ids))})"
            params.extend(entity_ids)
        if within_days is not None:
            sql += " AND days_until <= ?"
            params.append(within_days)
        return self.query(sql + " ORDER BY days_until", tuple(params))

    # --------------------------------------------------------- retention
    def retention_for_claim(self, customer_po_id, amount_cents,
                            exclude_claim_id=None):
        """How much this claim would have withheld from it.

        `min(rate x amount, remaining capacity)` -- the second term is what
        makes the cap bite. On a $700k PO at 10% capped at 2.5%, the first
        claim of $100k withholds $10,000, the second withholds only $7,500
        because that reaches $17,500, and the third withholds nothing.

        Rounding goes through ops.money, so the mode is ADR-15's and not
        whatever the hardware did.
        """
        po = self.query_one(
            "SELECT * FROM v_po_retention_position WHERE customer_po_id = ?",
            (customer_po_id,))
        if po is None or not po["retention_applies"] or not po["rate_bp"]:
            return 0
        remaining = po["remaining_to_withhold_cents"]
        if exclude_claim_id is not None:
            # Re-costing an existing claim: its own withholding is already
            # counted in `withheld_cents`, so give it back before capping,
            # or an edit silently shrinks its own capacity.
            own = self.scalar(
                "SELECT retention_cents FROM claim_line WHERE id = ?",
                (exclude_claim_id,)) or 0
            remaining += own
        want = money.apply_rate(amount_cents, po["rate_bp"])
        return max(0, min(want, remaining))

    def retention_release_schedule(self, customer_po_id):
        """What is held, and when it becomes claimable.

        `split` releases part at practical completion and the rest at DLP
        end; `dlp` releases everything at DLP end. A date that is not set
        yet returns None rather than a guess -- an unknown release date is
        information, and inventing one puts a number in a cash forecast that
        nobody can defend.
        """
        po = self.query_one(
            """SELECT r.*, p.practical_completion_date, p.dlp_end_date
               FROM v_po_retention_position r
               JOIN project p ON p.id = r.project_id
               WHERE r.customer_po_id = ?""", (customer_po_id,))
        if po is None or not po["retention_applies"]:
            return []
        held = po["held_cents"]
        if held <= 0:
            return []
        # A DLP typically ends 12 months after practical completion. Where
        # only PC is known, the release date is DERIVED and SAID TO BE --
        # never written to the project, because a date nobody agreed to
        # becomes a fact the moment it is stored.
        dlp_date, dlp_estimated = po["dlp_end_date"], False
        if dlp_date is None and po["practical_completion_date"]:
            dlp_date = self.add_months(po["practical_completion_date"], 12)
            dlp_estimated = dlp_date is not None
        if po["release_policy"] == "split" and po["release_split_bp"]:
            at_pc = money.apply_rate(held, po["release_split_bp"])
            return [
                {"stage": "practical_completion", "amount_cents": at_pc,
                 "due_date": po["practical_completion_date"],
                 "estimated": False},
                {"stage": "dlp_end", "amount_cents": held - at_pc,
                 "due_date": dlp_date, "estimated": dlp_estimated},
            ]
        return [{"stage": "dlp_end", "amount_cents": held,
                 "due_date": dlp_date, "estimated": dlp_estimated}]

    @staticmethod
    def add_months(iso_date, months):
        """`2027-03-31` + 12 months. Clamps to the last day of the target
        month, so 31 January + 1 month is 28 February rather than an error."""
        try:
            y, m, d = (int(x) for x in str(iso_date).split("-"))
        except (ValueError, AttributeError):
            return None
        total = (y * 12 + (m - 1)) + months
        y2, m2 = divmod(total, 12)
        m2 += 1
        last = [31, 29 if (y2 % 4 == 0 and (y2 % 100 or y2 % 400 == 0)) else 28,
                31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m2 - 1]
        return f"{y2:04d}-{m2:02d}-{min(d, last):02d}"

    # ---------------------------------------------------------- worklist
    # Codes that are not codes. Aliasing one would map five projects to
    # "TBA", which makes an alias lookup useless -- a placeholder belongs in
    # the audit trail, not the lookup table.
    PLACEHOLDER_CODES = frozenset(
        {"TBA", "NA", "N/A", "VARIOUS", "TBC", "TBD", "-", "", "(BLANK)"})

    def resolve_issue(self, issue_id, action, actor_id, job_code=None,
                      reason=None):
        """Close one job-code issue. Returns a dict describing what changed.

        The four actions are not variations on one thing:
          issue    -- allocate the next number from the sequence
          assign   -- set a specific code the customer or a predecessor gave us
          keep     -- accept a legitimately shared code (class C)
          dismiss  -- this is not project work and needs no number

        `reason` is mandatory for keep and dismiss because both leave the
        register looking wrong to the next reader: an unnumbered project or
        two projects on one code. Without the reason recorded, someone
        re-raises it in six months.
        """
        now = int(time.time())
        with self._tx() as c:
            issue = c.execute(
                "SELECT * FROM job_code_issue WHERE id = ?", (issue_id,)).fetchone()
            if issue is None or issue["status"] != "open":
                return None
            project_id = issue["project_id"]
            old_code = c.execute(
                "SELECT job_code FROM project WHERE id = ?",
                (project_id,)).fetchone()["job_code"] if project_id else None

            new_code = None
            if action == "issue":
                new_code = self._issue_job_number(c)
            elif action == "assign":
                new_code = job_code
                # Typing the number iTrade gave us is the normal path now
                # (ADR-29), which makes this the place a duplicate would
                # enter -- the class C defect coming back through the one
                # door left open.
                if new_code and new_code.upper() not in self.PLACEHOLDER_CODES:
                    clash = c.execute(
                        "SELECT name FROM project WHERE job_code = ? AND id <> ?",
                        (new_code, project_id)).fetchone()
                    if clash is not None:
                        raise ValueError(
                            f"{new_code} is already used by {clash['name']}")

            if new_code and project_id:
                c.execute("UPDATE project SET job_code = ? WHERE id = ?",
                          (new_code, project_id))
                # The legacy code is kept as an alias ONLY when it was a real
                # code. A placeholder like TBA carries no history, and five
                # projects all aliased from "TBA" would be worse than nothing.
                if old_code and old_code.upper() not in self.PLACEHOLDER_CODES:
                    c.execute(
                        """INSERT OR IGNORE INTO job_code_alias
                           (legacy_code, project_id, note, created_ts)
                           VALUES (?,?,?,?)""",
                        (old_code, project_id, "reissued at worklist resolution", now))

            # Migration 001 requires a reason on any resolved class C row.
            # For `issue` and `assign` the reason IS the action -- the code
            # stops being shared -- so record that rather than making someone
            # type "because I am giving it a new number".
            recorded = reason or (
                f"reissued as {new_code}" if new_code else None)
            c.execute(
                """UPDATE job_code_issue
                   SET status = ?, resolved_by = ?, resolved_at = ?, reason = ?
                   WHERE id = ?""",
                ("dismissed" if action == "dismiss" else "resolved",
                 actor_id, now, recorded, issue_id))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'worklist_resolve','job_code_issue',?,?)""",
                (now, actor_id, str(issue_id),
                 f"{action}: {old_code!r} -> {new_code or old_code!r}"
                 + (f" ({reason})" if reason else "")))

            cascaded = self._close_unshared_class_c(c, actor_id, now)
            self._refresh_needs_resolution(c, project_id)
            return {"issue_id": issue_id, "action": action,
                    "job_code": new_code or old_code,
                    "cascaded": cascaded}

    @staticmethod
    def _close_unshared_class_c(c, actor_id, now):
        """A class C issue says "this code covers two projects". Once one of
        them is reissued the statement is false, so the sibling issue is
        closed automatically rather than sitting open claiming something that
        is no longer true. Recorded, not silent.
        """
        closed = []
        rows = c.execute(
            """SELECT i.id, i.raw_code FROM job_code_issue i
               WHERE i.status = 'open' AND i.class = 'C'""").fetchall()
        for row in rows:
            n = c.execute("SELECT COUNT(*) FROM project WHERE job_code = ?",
                          (row["raw_code"],)).fetchone()[0]
            if n > 1:
                continue
            c.execute(
                """UPDATE job_code_issue SET status = 'resolved',
                       resolved_by = ?, resolved_at = ?, reason = ?
                   WHERE id = ?""",
                (actor_id, now, "code is no longer shared", row["id"]))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'worklist_resolve','job_code_issue',?,?)""",
                (now, actor_id, str(row["id"]),
                 f"auto-closed: {row['raw_code']} is no longer shared"))
            closed.append(row["id"])
        return closed

    @staticmethod
    def _refresh_needs_resolution(c, project_id):
        """The flag on a project mirrors whether it still has open issues.
        Two places holding the same fact is how a register shows a flag with
        nothing behind it."""
        if project_id is None:
            return
        for pid in {project_id} | {
                r[0] for r in c.execute(
                    "SELECT DISTINCT project_id FROM job_code_issue "
                    "WHERE project_id IS NOT NULL").fetchall()}:
            open_count = c.execute(
                "SELECT COUNT(*) FROM job_code_issue "
                "WHERE project_id = ? AND status = 'open'", (pid,)).fetchone()[0]
            c.execute("UPDATE project SET needs_resolution = ? WHERE id = ?",
                      (1 if open_count else 0, pid))

    def change_job_code(self, project_id, new_code, reason, actor_id):
        """Correct a wrong job code. ADMIN only, reason mandatory.

        `job_code` is otherwise immutable, because reassigning one breaks
        every downstream reference including Xero. But that argument holds
        once a project has history -- not at the point where a number was
        issued in error, which is exactly what happened when the create form
        allocated numbers for two projects that already had codes of their
        own.

        So: allowed, expensive, and traceable. The old code is kept as an
        alias, the change is audited with a reason, and it is refused once
        the project carries money -- the same guard as delete, for the same
        reason.
        """
        now = int(time.time())
        with self._tx() as c:
            row = c.execute(
                """SELECT job_code, purchase_order_cents, invoiced_prior_cents
                   FROM project WHERE id = ?""", (project_id,)).fetchone()
            if row is None:
                return None
            old_code = row["job_code"]
            if old_code == new_code:
                return {"changed": False, "job_code": old_code}
            # Uniqueness applies to real codes only. A placeholder is
            # non-unique BY DEFINITION -- several projects legitimately sit
            # on "TBA" at once, which is exactly what it means. Enforcing
            # uniqueness on it blocks the one honest way to say "this number
            # was issued in error and there is no correct one yet".
            if new_code.upper() not in self.PLACEHOLDER_CODES:
                clash = c.execute(
                    "SELECT name FROM project WHERE job_code = ? AND id <> ?",
                    (new_code, project_id)).fetchone()
                if clash is not None:
                    raise ValueError(
                        f"{new_code} is already used by {clash['name']}")
            c.execute("UPDATE project SET job_code = ? WHERE id = ?",
                      (new_code, project_id))
            if old_code and old_code.upper() not in self.PLACEHOLDER_CODES:
                c.execute(
                    """INSERT OR IGNORE INTO job_code_alias
                       (legacy_code, project_id, note, created_ts)
                       VALUES (?,?,?,?)""",
                    (old_code, project_id, f"corrected to {new_code}", now))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'job_code_change','project',?,?)""",
                (now, actor_id, str(project_id),
                 f"{old_code} -> {new_code}: {reason}"))
            # A code the platform does not own goes back on the worklist.
            if new_code.upper() in self.PLACEHOLDER_CODES:
                c.execute(
                    """INSERT INTO job_code_issue
                       (raw_code, class, project_id, created_ts)
                       VALUES (?,'B',?,?)""", (new_code, project_id, now))
                c.execute(
                    "UPDATE project SET needs_resolution = 1 WHERE id = ?",
                    (project_id,))
            return {"changed": True, "from": old_code, "job_code": new_code}

    def job_code_is_changeable(self, project_id):
        """None if it may be corrected, else the reason it may not."""
        row = self.query_one(
            """SELECT purchase_order_cents, invoiced_prior_cents
               FROM project WHERE id = ?""", (project_id,))
        if row is None:
            return "no such project"
        if row["invoiced_prior_cents"]:
            return ("project has invoicing history against this code; "
                    "correcting it would orphan those references")
        return None

    OPENING_TRIGGERS = ("claim_line_opening_no_update",
                        "claim_line_opening_no_delete")

    @contextlib.contextmanager
    def _opening_balances_writable(self):
        """Stand the immutability triggers down, briefly and explicitly.

        Opening balances are the boundary of what this platform knows and
        nothing should edit them by accident -- but they are MIGRATION
        ARTIFACTS, and an artifact built from a register that has since been
        corrected has to be correctable too. Restored in a `finally`, so an
        exception cannot leave the guarantee switched off.
        """
        definitions = {
            name: self.scalar("SELECT sql FROM sqlite_master WHERE name = ?",
                              (name,))
            for name in self.OPENING_TRIGGERS}
        missing = [n for n, sql in definitions.items() if not sql]
        if missing:
            raise RuntimeError(
                f"cannot find trigger definitions {missing}; refusing to "
                "make opening balances writable")
        with self._tx() as c:
            for name in self.OPENING_TRIGGERS:
                c.execute(f"DROP TRIGGER {name}")
            try:
                yield c
            finally:
                for name in self.OPENING_TRIGGERS:
                    c.execute(definitions[name])

    def apply_retention_to_opening_balances(self, project_id, actor_id):
        """Retention was withheld on pre-FY27 invoicing too.

        The opening balance represents invoices that were issued, and the
        customer held retention against them -- on three of the seven
        retention projects the full cap was reached before the platform's
        window even opened. Leaving it at zero would report $82,240 of held
        money as not held.

        The figure is DERIVED (rate x opening, capped), because the workbook
        never recorded what was actually withheld. It is stored rather than
        computed on read so it can be corrected when the real number is
        known, and it is audited as a derivation rather than a fact.
        """
        now = int(time.time())
        rows = self.query(
            """SELECT cl.id, cl.amount_cents, cl.retention_cents,
                      po.id AS po_id, po.amount_cents AS contract_cents,
                      po.retention_rate_bp, po.retention_cap_bp
               FROM claim_line cl
               JOIN customer_po po ON po.project_id = cl.project_id
               WHERE cl.project_id = ? AND cl.is_opening_balance = 1
                 AND po.retention_applies = 1""", (project_id,))
        if not rows:
            return 0
        changed = 0
        with self._opening_balances_writable() as c:
            for row in rows:
                cap = money.divide(
                    row["contract_cents"] * (row["retention_cap_bp"] or 0), 10000)
                want = min(money.apply_rate(row["amount_cents"],
                                            row["retention_rate_bp"] or 0), cap)
                if want == row["retention_cents"]:
                    continue
                c.execute(
                    """UPDATE claim_line SET retention_cents = ?,
                           customer_po_id = COALESCE(customer_po_id, ?)
                       WHERE id = ?""", (want, row["po_id"], row["id"]))
                c.execute(
                    """INSERT INTO audit_log (ts, actor_user_id, action,
                           target_type, target_id, detail)
                       VALUES (?,?,'retention_on_opening','claim_line',?,?)""",
                    (now, actor_id, str(row["id"]),
                     f"derived {money.format(want)} withheld on an opening "
                     f"balance of {money.format(row['amount_cents'])} "
                     f"(not recorded in the workbook)"))
                changed += 1
        return changed

    def set_retention_terms(self, project_id, cap_bp, rate_bp, policy,
                            split_bp, actor_id):
        """Apply retention terms to every PO on a project.

        The register states retention per PROJECT; the model holds it per PO
        (ADR: a variation raising the PO raises its cap with it). Applying
        the project's figure to each of its POs is the faithful reading --
        each then caps independently, which is what happens in practice when
        scope is split across orders.
        """
        now = int(time.time())
        applies = 1 if cap_bp else 0
        with self._tx() as c:
            pos = c.execute(
                "SELECT id, retention_cap_bp, retention_applies FROM customer_po "
                "WHERE project_id = ?", (project_id,)).fetchall()
            changed = []
            for po in pos:
                if (po["retention_applies"] == applies
                        and po["retention_cap_bp"] == (cap_bp or None)):
                    continue
                c.execute(
                    """UPDATE customer_po
                       SET retention_applies = ?, retention_cap_bp = ?,
                           retention_rate_bp = ?, release_policy = ?,
                           release_split_bp = ?
                       WHERE id = ?""",
                    (applies, cap_bp or None, rate_bp if applies else None,
                     policy if applies else None,
                     split_bp if applies else None, po["id"]))
                changed.append(po["id"])
            if changed:
                c.execute(
                    """INSERT INTO audit_log (ts, actor_user_id, action,
                           target_type, target_id, detail)
                       VALUES (?,?,'retention_terms','project',?,?)""",
                    (now, actor_id, str(project_id),
                     f"cap {cap_bp}bp, {rate_bp}bp per claim, {policy}"
                     if applies else "retention removed"))
        # Retention was held on the pre-FY27 invoicing too.
        if applies:
            self.apply_retention_to_opening_balances(project_id, actor_id)
        return changed

    def project_is_deletable(self, project_id):
        """A project carrying money is history, not a record you remove.

        Returns None if deletable, else the reason. The check is on money
        rather than on age or a flag, because the real case for deletion is
        'created by mistake a minute ago' and the real danger is deleting
        something a signed-off total depends on.
        """
        row = self.query_one(
            """SELECT purchase_order_cents, invoiced_prior_cents
               FROM project WHERE id = ?""", (project_id,))
        if row is None:
            return "no such project"
        if row["purchase_order_cents"] or row["invoiced_prior_cents"]:
            return ("project carries contract or invoiced value; set its "
                    "status to Complete instead of deleting it")
        return None

    def delete_project(self, project_id, actor_id):
        now = int(time.time())
        with self._tx() as c:
            row = c.execute(
                "SELECT name, job_code FROM project WHERE id = ?",
                (project_id,)).fetchone()
            if row is None:
                return False
            c.execute("DELETE FROM job_code_issue WHERE project_id = ?", (project_id,))
            c.execute("DELETE FROM job_code_alias WHERE project_id = ?", (project_id,))
            c.execute("DELETE FROM project WHERE id = ?", (project_id,))
            c.execute(
                """INSERT INTO audit_log (ts, actor_user_id, action,
                       target_type, target_id, detail)
                   VALUES (?,?,'project_delete','project',?,?)""",
                (now, actor_id, str(project_id),
                 f"{row['job_code']} {row['name']}"))
            return True
