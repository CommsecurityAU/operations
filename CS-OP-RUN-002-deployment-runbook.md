# CS-OP-RUN-002 — Deployment runbook

- **As at:** 25 August 2026
- **Applies to:** CS-OP-ARCH-002 §13, CS-OP-STP-001 STP-0
- **Status:** **DEFERRED 25 Aug 2026** — infrastructure not ready. STP-2 is
  now built and holds real data; this is the next thing to do.

---

## Deferred, and what that costs

The first deploy is on hold until the VM and supporting infrastructure are
available. That is a resourcing decision, not a change of plan, and the
runbook stands ready.

Two things follow, worth holding in view rather than discovering later:

1. **STP-001's standing rule is suspended, not met.** A phase ships and is
   used in anger before the next begins. STP-2 is being built while STP-0
   and STP-1 have been used by one person on one laptop. Every deployment
   assumption therefore stays unverified while more code is stacked on it,
   and the first deploy will surface its problems with a larger system
   attached.
2. **There is still no off-box backup.** The only data at risk today is a
   dev database that can be rebuilt from the workbook, so the exposure is
   near zero — but it stops being near zero the moment anyone enters real
   invoicing data, which is what STP-2 makes possible.

**Revisit when:** the VM exists, or STP-2 is ready to carry real data —
whichever comes first. The second is the harder deadline.

**That moment arrived on 25 August 2026, and STP-2 finished building on
the 26th.** The platform now also holds customer orders, retention terms on
seven projects, recurring schedules and PO revision history — none of which
exists anywhere else.

**Original note, 25 August:** The platform now holds 202
imported claims, the full FY27 forward position of $3,203,976.74, and
corrections that exist nowhere else — four opening balances and ten project
leads applied through `sync_register.py`. **There is still no off-box
backup.** The only copy of that state is one laptop's `data/ops.db`. This is
no longer a theoretical exposure and the runbook should be executed as soon
as the VM allows.

---

## Four things this runbook does not know

Marked **[SITE]** throughout. Fill them in as you go; the runbook is only
finished once they are answered, and it should be corrected in place rather
than remembered.

1. **[SITE-VM]** — hostname, how you reach it, whether Docker is installed
2. **[SITE-FLEET]** — how a release is created in the fleet manager and what
   it expects
3. ~~**[SITE-CA]**~~ — answered 4 September 2026: there is no pre-existing
   internal CA. `tools/issue_cert.sh` creates one, on the operator's own
   machine, and issues the server pair; see §2b for where and when
4. **[SITE-BACKUP]** — what `/mnt/backup/cs-ops` actually is

---

## Before you start

**Have these ready.** Each has bitten already or is known to be missing:

- [ ] **OIDC client registered** — Cloud project inside the Workspace org,
      consent screen **Internal**, redirect URIs
      `https://ops.commsecurity.com.au/auth/callback` **and**
      `http://localhost:5173/auth/callback`
- [ ] **`OIDC_CLIENT_SECRET` in the company password manager.** The backup
      deliberately excludes `secrets/`, so if it exists only on the volume,
      a volume loss is unrecoverable without re-registering (CS-OP-RUN-001)
- [ ] **Certificate and key as a matched pair**, issued with
      `tools/issue_cert.sh` on your own machine (§2b), and the CA root
      pushed to staff machines so browsers trust it
- [ ] **A release tag pushed**, so the N-1 gate has a baseline

**What is deliberately NOT ready, and should stay that way:** no
job-number range is reserved, so allocation refuses (ADR-29). That is
correct until a block is agreed with whoever runs iTrade.

---

## 1. The image

CI has already built and pushed it. Confirm what you are deploying:

```
docker pull ghcr.io/commsecurityau/cs-ops:latest
docker inspect --format='{{index .RepoDigests 0}}' ghcr.io/commsecurityau/cs-ops:latest
```

The release must pin **that digest**, not `:latest` — a release means
exactly those bytes forever (§13). The fleet manager rewrites `repo:tag` to
a digest at release creation, so each image reference must sit on its own
`image:` line.

---

## 2. The volume, before first boot

`/data` is all state, bind-mounted from `/var/lib/cs-ops` on the VM. The
directory must exist and be owned by the container's user **before the
first boot**. The container runs as uid 100, gid 101 (`ops`), and a plain
`mkdir -p` leaves the directory root-owned, which is exactly the
`unable to open database file` the first deploy failed with:

```
sudo install -d -o 100 -g 101 -m 750 /var/lib/cs-ops
```

With the secret and certificate delivered in the release (§3), nothing
else needs to be in it. If they are not, two things must exist before the
app starts, and it refuses to start without them — deliberately, because
a service that boots with a blank credential fails later and more
confusingly.

```
/var/lib/cs-ops/            (bind-mounted as /data)
├── secrets/store.json     OIDC_CLIENT_SECRET       (step 2a)
└── tls/
    ├── server.crt         from the internal CA     [SITE-CA]
    └── server.key         the matching key
```

### 2a. The secret

Set from **stdin**, never argv — argv lands in shell history, the process
list, and any `ps` a colleague runs while it is in flight.

```
docker run --rm -i -v /var/lib/cs-ops:/data ghcr.io/commsecurityau/cs-ops:<digest> \
    sh -c 'python3 -m ops.secrets set OIDC_CLIENT_SECRET' <<'EOF'
<paste the secret>
EOF
```

Confirm it is there without printing it:

```
docker run --rm -v /var/lib/cs-ops:/data ghcr.io/commsecurityau/cs-ops:<digest> \
    python3 -m ops.secrets list
```

### 2b. The certificate

**Where and when.** On your own machine, not the VM, before creating the
release that will carry it. There is no pre-existing internal CA;
`tools/issue_cert.sh` creates one on first run and issues the server
pair on every run:

```
./tools/issue_cert.sh                                   # CA in ~/cs-ops-ca
./tools/issue_cert.sh ~/cs-ops-ca ops.commsecurity.com.au 172.16.x.x   # also valid by IP
```

It prints `OPS_TLS_CERT=...` and `OPS_TLS_KEY=...`, each one line of about
2,300 characters, ready for the release JSON (§3). Three things follow
from it:

1. **`~/cs-ops-ca` is the CA.** Its `ca.key` signs everything staff
   browsers will trust for this host. Put the directory in the company
   password manager and nowhere else; it never goes near the VM.
2. **`ca.crt` must reach every staff browser** or sign-in shows a
   certificate warning. Push it through Workspace device management
   (Devices → Networks → Certificates), or install it by hand.
3. **`ops.commsecurity.com.au` must resolve** to the VM's VPN address on
   staff machines, or the name on the certificate never matches.

**Renewal owner: whoever holds the CA directory.** The leaf is valid 730
days; the app warns at boot and hourly from 30 days out. Renewal is
running the script again and creating a release with the new values.

**Delivery.** Put the pair in the release environment as base64 and the
app writes it to `/var/lib/cs-ops/tls/` at boot, key 0600, before the
checks below run. Renewal is then a new release, not a login to the host,
and a restore needs nothing copied by hand.

Both or neither: one without the other exits 2 naming the missing one,
rather than pairing a new certificate with a stale key from the volume.
Values already on the volume are overwritten, which is the point.

**Fallback: copy it onto the volume.**
Copy the pair into `/var/lib/cs-ops/tls/`. **The container runs as the non-root
`ops` user**, so a key copied in as root with mode 600 is unreadable to it —
the most likely way this deploy fails:

```
sudo chown 100:101 /var/lib/cs-ops/tls/server.crt /var/lib/cs-ops/tls/server.key
sudo chmod 600 /var/lib/cs-ops/tls/server.key
```

If either file is missing, unreadable, or the pair does not match, boot
exits 2 and says which file and what to do. That is by design: non-zero =
unhealthy = automatic rollback.

---

## 3. The release

Raven-Fleet writes the release's environment JSON to `.env` beside the
compose file, and Compose fills every `${...}` from it. The compose file
in git therefore holds no host-specific value at all.

**Environment (JSON).** The secret and the TLS pair travel here (decided 4
September 2026, superseding the `secret://`-only rule in §10: the store on
the volume remains the fallback when `OIDC_CLIENT_SECRET` is unset).

```
{
  "OIDC_CLIENT_ID": "<the client id — not a secret>",
  "OIDC_REDIRECT_URI": "https://ops.commsecurity.com.au/auth/callback",
  "OIDC_CLIENT_SECRET": "<the secret>",
  "OPS_PORT": "8443",
  "OPS_TLS": "true",
  "OPS_HOSTED_DOMAIN": "commsecurity.com.au",
  "OPS_BACKUP_INTERVAL_S": "3600",
  "OPS_BACKUP_KEEP": "48",
  "OPS_TLS_CERT": "<base64 -w0 server.crt>",
  "OPS_TLS_KEY": "<base64 -w0 server.key>",
  "OPS_BOOTSTRAP_ADMIN": "richard@commsecurity.com.au"
}
```

`OPS_BOOTSTRAP_ADMIN` names who receives every role on every entity at
their next sign-in, **only while the system has no active admin at all**.
The first deploy produced one viewer and no way to promote them; a restore
into an empty volume does the same. Once an admin exists the variable is
inert, so it stays in the release. The grant is audited as
`bootstrap_admin`.

The two TLS values are each one line of roughly two thousand characters.
A value of 34 characters is the placeholder text, not a certificate, and
the boot log's `"tls_cert_b64": "<34 bytes>"` is how that shows up.

**Host privileges (JSON).** The fleet manager gates bind mounts, and
privileges are fixed at release creation — an existing release cannot be
edited, so a missing grant means a new release:

```
{"cs-ops": ["bind:/var/lib/cs-ops"]}
```

**The VM's ceiling.** Provisioning writes `/etc/raven-fleet/policy.json`
only when absent, so an existing box needs the edit by hand, as root:

```
printf '{"version":1,"max_allow":["bind:/var/lib/cs-ops"]}\n' | sudo tee /etc/raven-fleet/policy.json
sudo chown root:root /etc/raven-fleet/policy.json && sudo chmod 0644 /etc/raven-fleet/policy.json
```

**Why an absolute bind mount.** Staging directories are wiped on
supersede, so a relative `./data` would have put the database in a
directory that vanishes on the second deploy — and Docker created it
root-owned, which is the `unable to open database file` the first deploy
hit. `/var/lib/cs-ops` persists, and is where `offbox_sync.sh` and
`deploy.sh` look.

---

## 4. Deploy

The agent verifies the signed manifest, pulls over the tunnel, stages, then
health-gates on `/healthz`. On failure it rolls back locally, with no
network and no operator.

Watch for, in order:

```
{"event": "boot", "code": "<fingerprint>", "release": "<sha>", ...}
{"event": "migrated", "versions": ["001_foundation.sql", "002_job_number_range.sql"]}
{"event": "listening", "port": 8443, "tls": true}
```

**A missing `migrated` line on a first deploy means the volume already had a
database.** Worth stopping to understand rather than pressing on.

---

## 5. Confirm

```
curl -s https://ops.commsecurity.com.au/healthz
```

Expect `{"ok": true, "schema": {...}, "integrity": "ok", "warnings": []}`
plus `code` and `release`.

Then the things `/healthz` cannot tell you:

- [ ] **Sign in with a real Workspace account.** First sign-in provisions
      `viewer` on **zero** entities, so the correct result is a 403 from the
      project list, not an empty list
- [ ] **Grant yourself a role** and confirm the register appears **without
      signing in again** — roles resolve per request
- [ ] **Grant `viewer` AND `operations`.** No role implies another, so an
      admin who is not also a viewer cannot read the register. This has
      caught us twice
- [ ] **Import the register** if this volume is new:
      `docker exec ops python3 tools/import_register.py --csv ... --db /data/ops.db`
- [ ] **Orders in hand reads $3,520,041.73** at FY27 start

---

## 6. Off-box backup — not optional

Currently **no off-box backup exists**. The 21 Aug rehearsal used a copy
made by hand.

```
cp tools/offbox_sync.sh /opt/cs-ops/offbox_sync.sh
chmod +x /opt/cs-ops/offbox_sync.sh
crontab -e
  17 * * * * /opt/cs-ops/offbox_sync.sh >> /var/log/cs-ops-backup.log 2>&1
```

Set `OPS_DATA` and `OPS_OFFBOX` in the script or the crontab environment.
**[SITE-BACKUP]** — what `/mnt/backup/cs-ops` is.

It copies `backups/` and `documents/` **only**. Never the live `ops.db`: a
WAL database copied mid-transaction yields a `.db` and a `-wal` that
disagree, and the copy fails only at restore.

Run it once by hand and read the output — it warns if the newest off-box
snapshot is over 3 hours old, because a sync that succeeds while the app has
stopped snapshotting looks healthy and is not.

---

## 7. Then, and only then, close STP-0

- [ ] **Restore rehearsal against the real deployment**, from the off-box
      copy (CS-OP-RUN-001). The 21 Aug rehearsal was against a container in
      a sandbox; this is the one that counts
- [ ] Record the elapsed time against the 60 s budget
- [ ] Note the date; next rehearsal one month on

---

## If the deploy fails

**Rollback is automatic** — the agent health-gates and reverts the image
without asking. What it does *not* do is roll back the database: migrations
have already run, and the pre-migration snapshot exists but restoring it is
a deliberate act that discards writes since.

That asymmetry is why migrations are expand-only and why `/healthz` reports
`applied ⊇ expected` rather than equality. A rolled-back release must be
able to come up against a schema newer than itself.

Common first-deploy failures, all of which now name themselves:

| Symptom | Cause |
|---|---|
| exit 2, "no certificate at /data/tls/server.crt" | cert not placed |
| exit 2, "cannot be read ... runs as the non-root 'ops' user" | key ownership |
| exit 2, "do not go together" | cert and key from different issuances |
| exit 2, "unresolved secret references" | `OIDC_CLIENT_SECRET` not set on the volume |
| exit 2, "OIDC is not fully configured: ..." | a missing env var, named |
| 403 after signing in | correct — grant yourself a role |
| `/healthz` 503, `missing: [...]` | migrations did not run; read the boot log |

---

## After the first deploy

Update this runbook **in place** with the four **[SITE]** answers and
anything that surprised you. A runbook that was written before the thing was
done, and never corrected afterwards, is a document that describes an
imagined system.
