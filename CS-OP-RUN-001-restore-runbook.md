# CS-OP-RUN-001 — Restore runbook

- **As at:** 21 August 2026
- **Applies to:** CS-OP-ARCH-002 §12
- **Cadence:** rehearse monthly, from the off-box copy

---

## Read this first

**The off-box copy is the backup.** `/data/backups` is a convenience;
backups living on the volume they protect are not backups.

**Restore is a deliberate operator act.** Image rollback is automatic;
database rollback is not. Restoring **discards every write since the
snapshot** — that is why `restore.py` refuses to overwrite a live database
without `--force`.

**Secrets and TLS are NOT in the backup.** See "What the backup does not
cover" below. A restore stalls at boot until they are replaced, and
discovering that at 2am is exactly what this runbook exists to prevent.

---

## Restore

### 1. Verify the snapshot before touching anything

```
python3 tools/restore.py --check /mnt/backup/cs-ops/backups/ops-<stamp>.db
```

Read the output. It reports integrity, applied migrations, seed counts, and
whether the register balances and matches the validated figures. **A missing
"matches the 21 Aug 2026 validated figures" line means rows are absent** —
the snapshot can still balance while being short 49 projects, so this is the
line to look at, not the balance flag.

Nothing is written by `--check`. If it aborts, try the next snapshot down.

### 2. Stop the app

```
docker stop ops
```

### 3. Restore — documents first, then the database

```
python3 tools/restore.py --restore /mnt/backup/cs-ops/backups/ops-<stamp>.db \
    --data /var/lib/cs-ops \
    --documents /mnt/backup/cs-ops/documents \
    --force
```

`--force` is required whenever an `ops.db` is already present. The tool
removes stale `-wal`/`-shm` files: they belong to the *old* database and
would be replayed over the restored one.

### 4. Replace what the backup does not carry

```
# OIDC client secret — the app will not boot without it
docker run --rm -it -v /var/lib/cs-ops:/data cs-ops \
    sh -c 'printf "%s" "$SECRET" | python3 -m ops.secrets set OIDC_CLIENT_SECRET'

# TLS certificate and key
cp server.crt server.key /var/lib/cs-ops/tls/
```

The session signing key regenerates automatically. Every existing session is
invalidated by that, so **everyone signs in again** — expected, and worth
telling people before they report it as a fault.

### 5. Start and confirm

```
docker start ops
curl -s https://ops.commsecurity.com.au/healthz
```

Expect `{"ok": true, "schema": {...}, "integrity": "ok", "warnings": []}`.

Then confirm the money, not just the process: sign in and check orders in
hand against the figure in CS-OP-BUILD-001. A restore that only proves a
file was copied has not proven a restore.

---

## What the backup does not cover

The off-box sync copies `backups/` and `documents/` **only**. After a volume
loss these are gone:

| Lost | Consequence | Recovery |
|---|---|---|
| `secrets/store.json` | **App cannot boot** — fail-loud on an unresolved `secret://` reference | Re-enter `OIDC_CLIENT_SECRET` via the CLI (step 4) |
| `secrets/session.key` | All sessions invalidated | Regenerates on boot; everyone signs in again |
| `tls/server.crt`, `server.key` | No TLS listener | Reissue from the internal CA |

**This is deliberate.** Copying a secret store off-box would put live
credentials on a second machine with a different threat model, so the
absence is a security posture rather than an oversight — but it makes step 4
mandatory, and it means **`OIDC_CLIENT_SECRET` must be recoverable from
somewhere other than this system.** If it exists only on the `/data` volume,
a volume loss is unrecoverable without re-registering the OIDC client.
Keep it in the company password manager.

---

## Rehearsal record

### 21 August 2026 — first rehearsal, full volume loss

Performed against a system carrying the real 59-project register, one user
with a grant, and a document blob.

| Step | Result |
|---|---|
| Snapshot (`VACUUM INTO`) | 3 ms, 0.13 MB |
| Off-box sync | 1 snapshot + 1 document; **`ops.db` correctly absent** |
| Volume destroyed | db, wal, snapshots, documents, secrets — all of it |
| Pre-flight `--check` | integrity ok, 001 applied, 59 projects, $3,520,041.73, balances |
| Restore | **0.03 s** — against the 60 s budget |
| App boot on restored volume | 12 ms |
| `/healthz` | 200, schema 1/1 |
| `/api/projects` | 200, 59 projects, **$3,520,041.73** |
| User and grant | restored intact |
| Document blob | restored |
| Post-snapshot write | **absent, as expected** — restore discards writes since |

**Verdict: pass**, well inside budget.

### What the rehearsal found

**Secrets and TLS are not backed up.** Not visible from reading §12 — the
app simply refused to start after the restore, which is correct behaviour
and completely opaque if you have not met it before. This is now step 4
above and is the single reason the rehearsal was worth doing.

**`rsync` was assumed.** The sync script failed with a bare
`rsync: not found`. It now pre-flights and says so.

**An `admin` cannot read projects.** During the rehearsal the restored user
held `admin` and got a 403 from `/api/projects`. That is §9 working as
designed — no role implies another, because approval is a named
responsibility — but in practice staff need both grants. Worth remembering
when granting roles, and worth surfacing clearly in the STP-1 admin screen.

---

## Off-box sync

`tools/offbox_sync.sh` runs on the **host**, hourly:

```
17 * * * * /opt/cs-ops/offbox_sync.sh >> /var/log/cs-ops-backup.log 2>&1
```

It copies `backups/` and `documents/` only, warns if the newest off-box
snapshot is more than 3 hours old — a sync that succeeds while the app has
stopped snapshotting looks healthy and is not — and prunes beyond 90 days.

**Not yet installed on the VM.** Outstanding.
