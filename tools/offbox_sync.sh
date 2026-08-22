#!/bin/sh
# Off-box backup sync. Runs on the HOST, not in the container (§12).
#
# Installed as an hourly cron entry on the VM:
#   17 * * * * /opt/cs-ops/offbox_sync.sh >> /var/log/cs-ops-backup.log 2>&1
#
# ONLY backups/ and documents/ are copied. NEVER the live ops.db: a WAL
# database copied mid-transaction yields a .db and a -wal that disagree, and
# the copy fails only at restore -- on the day you need it. The snapshots in
# backups/ are produced by VACUUM INTO and are atomic and consistent by
# construction; blobs in documents/ are content-addressed and immutable, so
# both are safe to copy while the app runs.
#
# Backups living on the volume they protect are not backups. THIS is the
# backup; /data/backups is a convenience.
set -eu

SRC="${OPS_DATA:-/var/lib/docker/volumes/ops-data/_data}"
DEST="${OPS_OFFBOX:-/mnt/backup/cs-ops}"
KEEP_DAYS="${OPS_OFFBOX_KEEP_DAYS:-90}"

command -v rsync >/dev/null 2>&1 || {
    echo "$(date -Is) FATAL: rsync is not installed on this host" >&2
    exit 1
}

if [ ! -d "$SRC/backups" ]; then
    echo "$(date -Is) FATAL: $SRC/backups does not exist" >&2
    exit 1
fi

mkdir -p "$DEST"

# --ignore-existing: snapshots are immutable once written, so never re-copy.
rsync -a --ignore-existing "$SRC/backups/" "$DEST/backups/"
[ -d "$SRC/documents" ] && rsync -a --ignore-existing "$SRC/documents/" "$DEST/documents/"

# Deliberately absent: any rule that would copy $SRC/ops.db.

newest=$(ls -1t "$DEST/backups"/ops-*.db 2>/dev/null | head -1 || true)
if [ -z "$newest" ]; then
    echo "$(date -Is) FATAL: no snapshots off-box after sync" >&2
    exit 1
fi

# A sync that succeeds while the app has stopped snapshotting looks healthy
# and is not. Age the newest copy so silence is detectable.
age_h=$(( ( $(date +%s) - $(stat -c %Y "$newest") ) / 3600 ))
count=$(ls -1 "$DEST/backups"/ops-*.db | wc -l)
echo "$(date -Is) ok: $count snapshots off-box, newest ${age_h}h old"
if [ "$age_h" -gt 3 ]; then
    echo "$(date -Is) WARNING: newest off-box snapshot is ${age_h}h old" >&2
fi

find "$DEST/backups" -name 'ops-*.db' -mtime "+$KEEP_DAYS" -delete
