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
            po_cents = fields.get("purchase_order_cents", 0)
            if po_cents:
                c.execute(
                    """INSERT INTO customer_po
                       (entity_id, project_id, po_number, amount_cents,
                        note, created_by, created_ts)
                       VALUES (?,?,?,?,?,?,?)""",
                    (fields["entity_id"], pid, fields.get("po_number"),
                     po_cents, fields.get("po_note"), actor_id, now))
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
            # Same dual write. Until the PO editor exists (STP-2), a change
            # to the legacy column has to move the PO it was migrated into,
            # or the register and the view disagree about the same project.
            if "purchase_order_cents" in applied:
                _old_v, new_v = applied["purchase_order_cents"]
                po = c.execute(
                    """SELECT id, amount_cents FROM customer_po
                       WHERE project_id = ? ORDER BY id LIMIT 1""",
                    (project_id,)).fetchone()
                if po is None:
                    c.execute(
                        """INSERT INTO customer_po
                           (entity_id, project_id, amount_cents, note,
                            created_by, created_ts)
                           SELECT entity_id, id, ?, 'created by contract edit',
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
        if po["release_policy"] == "split" and po["release_split_bp"]:
            at_pc = money.apply_rate(held, po["release_split_bp"])
            return [
                {"stage": "practical_completion", "amount_cents": at_pc,
                 "due_date": po["practical_completion_date"]},
                {"stage": "dlp_end", "amount_cents": held - at_pc,
                 "due_date": po["dlp_end_date"]},
            ]
        return [{"stage": "dlp_end", "amount_cents": held,
                 "due_date": po["dlp_end_date"]}]

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
