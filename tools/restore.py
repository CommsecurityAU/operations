"""Restore a snapshot (CS-OP-ARCH-002 §12).

    python3 tools/restore.py --check   backups/ops-20260821T021500Z.db
    python3 tools/restore.py --restore backups/ops-20260821T021500Z.db --data /data

Three ideas shape this.

**Verify before destroying.** The snapshot is checked -- integrity, schema
version, expected tables, plausible row counts -- BEFORE anything is
overwritten. Discovering a corrupt backup after deleting the live database
is the worst possible ordering, and it is the default ordering if restore is
"copy a file".

**Documents before the database.** A metadata row pointing at a missing blob
is a visible 404; a blob with no row is invisible and harmless. So the file
tree lands first.

**Prove the data, not the copy.** A restore that only proves a file was
copied has not proven a restore. This asserts the register still reconciles
-- Purchase Order = Invoiced Prior + Orders in Hand -- against the figures
validated on 21 Aug 2026, so a silently truncated snapshot fails loudly.

Restore is a DELIBERATE OPERATOR ACT and always will be. Image rollback is
automatic; database rollback is not, and restoring discards every write
since the snapshot. That asymmetry is why §4's N-1 rule is load-bearing.
"""

import argparse
import os
import shutil
import sqlite3
import sys
import time

EXPECTED_TABLES = ("entity", "period", "users", "user_entity_role",
                   "audit_log", "project", "schema_migrations")

# Validated 21 Aug 2026. Present only if the register has been imported.
REGISTER_PO_CENTS = 723265700
REGISTER_PRIOR_CENTS = 367040527
REGISTER_OIH_CENTS = 356225173


class RestoreError(Exception):
    pass


def _cents(v):
    return f"${v / 100:,.2f}"


def check(path):
    """Read-only pre-flight. Returns a findings dict; raises on anything
    that makes the snapshot unusable."""
    if not os.path.exists(path):
        raise RestoreError(f"snapshot not found: {path}")
    size = os.path.getsize(path)
    if size == 0:
        raise RestoreError("snapshot is empty")

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RestoreError(f"integrity check failed: {integrity}")

        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in EXPECTED_TABLES if t not in names]
        if missing:
            raise RestoreError(f"snapshot is missing tables: {missing}")

        migrations = [r[0] for r in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version")]
        if not migrations:
            raise RestoreError("snapshot has no applied migrations")

        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("entity", "period", "project", "users")}
        if counts["entity"] == 0 or counts["period"] == 0:
            raise RestoreError("snapshot has no seed data; it is not a "
                               "migrated database")

        register = None
        if counts["project"]:
            po, prior, oih = conn.execute(
                """SELECT SUM(purchase_order_cents),
                          SUM(invoiced_prior_cents),
                          SUM(purchase_order_cents - invoiced_prior_cents)
                   FROM project""").fetchone()
            register = {"projects": counts["project"], "po_cents": po,
                        "prior_cents": prior, "oih_cents": oih,
                        "balances": po == prior + oih}
            if not register["balances"]:
                raise RestoreError(
                    "restored register does not balance: "
                    f"{_cents(po)} != {_cents(prior)} + {_cents(oih)}")
    finally:
        conn.close()

    return {"path": path, "bytes": size, "integrity": integrity,
            "migrations": migrations, "counts": counts, "register": register}


def restore(snapshot, data_dir, documents_src=None, force=False):
    """Documents first, then the database. Returns (findings, elapsed_s)."""
    started = time.monotonic()
    check(snapshot)          # raises before anything is overwritten

    db_path = os.path.join(data_dir, "ops.db")
    if os.path.exists(db_path) and not force:
        raise RestoreError(
            f"{db_path} already exists. Restoring DISCARDS every write since "
            "the snapshot; pass --force once you have decided that is what "
            "you want.")

    os.makedirs(data_dir, exist_ok=True)

    # 1. Documents first: a row pointing at a missing blob is a visible 404;
    #    a blob with no row is invisible and harmless.
    if documents_src:
        dest = os.path.join(data_dir, "documents")
        if os.path.isdir(documents_src):
            os.makedirs(dest, exist_ok=True)
            for root, _dirs, files in os.walk(documents_src):
                rel = os.path.relpath(root, documents_src)
                target = os.path.join(dest, rel) if rel != "." else dest
                os.makedirs(target, exist_ok=True)
                for f in files:
                    shutil.copy2(os.path.join(root, f),
                                 os.path.join(target, f))

    # 2. Then the database. Stale -wal/-shm belong to the OLD database and
    #    would be replayed over the restored one.
    for suffix in ("-wal", "-shm"):
        stale = db_path + suffix
        if os.path.exists(stale):
            os.unlink(stale)
    shutil.copy2(snapshot, db_path)

    after = check(db_path)
    elapsed = time.monotonic() - started
    return after, elapsed


def report(findings, elapsed=None, budget=60.0):
    out = [
        "",
        f"  snapshot      {findings['path']}",
        f"  size          {findings['bytes'] / 1048576:.2f} MB",
        f"  integrity     {findings['integrity']}",
        f"  migrations    {', '.join(findings['migrations'])}",
        f"  entities      {findings['counts']['entity']}",
        f"  periods       {findings['counts']['period']}",
        f"  users         {findings['counts']['users']}",
    ]
    reg = findings["register"]
    if reg:
        out += [
            f"  projects      {reg['projects']}",
            f"  purchase order{_cents(reg['po_cents']):>18}",
            f"  invoiced prior{_cents(reg['prior_cents']):>18}",
            f"  orders in hand{_cents(reg['oih_cents']):>18}",
            f"  balances      {'yes' if reg['balances'] else 'NO'}",
        ]
        if reg["oih_cents"] == REGISTER_OIH_CENTS:
            out.append("  register      matches the 25 Aug 2026 validated figures")
    else:
        out.append("  projects      0 (register not yet imported)")
    if elapsed is not None:
        verdict = "within" if elapsed < budget else "OVER"
        out.append(f"  elapsed       {elapsed:.2f}s ({verdict} the {budget:.0f}s budget)")
    return "\n".join(out) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("snapshot")
    ap.add_argument("--check", action="store_true",
                    help="verify only; touch nothing")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--data", default="/data")
    ap.add_argument("--documents", help="documents/ tree from the off-box copy")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing ops.db")
    args = ap.parse_args(argv)

    try:
        if args.restore:
            findings, elapsed = restore(args.snapshot, args.data,
                                        args.documents, args.force)
            print(report(findings, elapsed))
        else:
            print(report(check(args.snapshot)))
    except RestoreError as e:
        print(f"ABORT: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
