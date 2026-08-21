"""Snapshots, pruning, integrity checks (CS-OP-ARCH-002 §12, ADR-17).

Runs IN PROCESS on a daemon thread, not under crond. §0 promises one Python
process and that stays literally true: crond means a second process, a
PID-1/supervisor question, and a scheduler with no visibility of the write
lock.

The cost of that choice is that a job dying quietly takes the RPO with it
and nothing external notices -- so failure is recorded and surfaced through
`/healthz` rather than only logged.

`VACUUM INTO` produces an atomic, consistent copy without stopping writes.
Note what is NOT safe: copying the live `ops.db` while the app runs yields a
`.db` and a `-wal` that disagree, and the copy fails only at restore. The
host rsync takes `backups/` and `documents/` ONLY.
"""

import logging
import os
import threading
import time

log = logging.getLogger("ops.backup")

PREFIX = "ops-"
SUFFIX = ".db"


def snapshot(db, backup_dir, now=None):
    """Atomic consistent copy. Returns the path written."""
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ",
                          time.gmtime(now if now is not None else time.time()))
    path = os.path.join(backup_dir, f"{PREFIX}{stamp}{SUFFIX}")
    if os.path.exists(path):  # same-second call; don't clobber
        path = os.path.join(backup_dir, f"{PREFIX}{stamp}-{os.getpid()}{SUFFIX}")
    # VACUUM INTO cannot run inside a transaction, so it does not go through
    # _tx(). It takes its own read snapshot and does not block writers.
    db._write.execute("VACUUM INTO ?", (path,))
    return path


def prune(backup_dir, keep):
    """Oldest first. Names are UTC timestamps, so lexical order is
    chronological order."""
    if not os.path.isdir(backup_dir):
        return []
    files = sorted(f for f in os.listdir(backup_dir)
                   if f.startswith(PREFIX) and f.endswith(SUFFIX))
    removed = []
    for name in files[:max(0, len(files) - keep)]:
        try:
            os.unlink(os.path.join(backup_dir, name))
            removed.append(name)
        except OSError as e:
            log.warning("could not prune %s: %s", name, e)
    return removed


def integrity_check(db):
    result = db._write.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        log.error("INTEGRITY CHECK FAILED: %s", result)
    return result


class Scheduler:
    """Daemon thread. Records the last error on the Db so `/healthz` can
    report it -- a backup silently failing for a fortnight is worse than no
    backup, because it buys false confidence."""

    def __init__(self, db, backup_dir, interval_s=3600, keep=48):
        self.db = db
        self.backup_dir = backup_dir
        self.interval_s = interval_s
        self.keep = keep
        self._stop = threading.Event()
        self._thread = None
        self.last_ok_ts = None
        self.runs = 0

    def run_once(self):
        try:
            path = snapshot(self.db, self.backup_dir)
            prune(self.backup_dir, self.keep)
            self.last_ok_ts = int(time.time())
            self.db.last_backup_error = None
            self.runs += 1
            log.info("snapshot %s", os.path.basename(path))
            return path
        except Exception as e:
            # Surfaced on /healthz, not just logged.
            self.db.last_backup_error = f"{type(e).__name__}: {e}"
            log.exception("snapshot failed")
            return None

    def _loop(self):
        while not self._stop.wait(self.interval_s):
            self.run_once()

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, name="backup", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
