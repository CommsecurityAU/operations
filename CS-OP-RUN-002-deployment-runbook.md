# CS-OP-RUN-002 — Deployment runbook

- **As at:** 4 September 2026
- **Applies to:** CS-OP-ARCH-002 §13, CS-OP-STP-001 STP-0
- **Status:** **DEPLOYED 4 September 2026** on the internal VM through
  Raven-Fleet, at `https://ops.commsecurity.com.au` over the VPN. Seeded
  from the laptop database the same day. §1–§5 record how it was done and
  what was wrong the first time; **§5a is the routine for every update
  since.** Still open: §6, the off-box backup.

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

1. ~~**[SITE-VM]**~~ — answered 4 September 2026: `172.16.224.83` on the
   VPN, `ssh commsecurity@172.16.224.83` (key auth; passwordless sudo;
   user in the `docker` group). Docker and the Raven-Fleet agent are
   installed; the agent pulls images from the fleet's mirror at
   `100.64.0.1:5000`. `ops.commsecurity.com.au` has no DNS record yet —
   staff machines need a hosts entry until it does.
2. ~~**[SITE-FLEET]**~~ — answered 4 September 2026: a release is the
   compose file pasted in, the environment JSON (§3), and the host
   privileges JSON (§3). The fleet pins `image: commsecurityau/cs-ops:latest`
   to a digest on its mirror at release creation; the mirror only learns
   about a new build when someone adds the image again (§5a).
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

CI builds on every push to `main` and pushes `ghcr.io/commsecurityau/cs-ops`
as `:latest` and `:<short sha>`. The fleet's mirror is a **copy** of that,
taken when someone adds the image, and it never refreshes cs-ops on its own
(the fleet's auto-refresh covers only its own `raven-*` packages).

So, in Raven-Fleet → **Images** → **Add image**, enter
`ghcr.io/commsecurityau/cs-ops:latest`. The mirror stores it as
`commsecurityau/cs-ops`, host dropped, which is why the compose file names
the bare path. Adding it again after a new build is what moves the mirror's
`latest` forward — skip this and the next release quietly pins the OLD
build.

The compose file in git carries `image: commsecurityau/cs-ops:latest` and
the fleet rewrites it to `<mirror>/commsecurityau/cs-ops@sha256:…` when the
release is created, so a release still means exactly those bytes forever
(§13). Each image reference must sit on its own `image:` line, and a gate
keeps it to the bare path with an explicit tag.

To see what a running device actually has:

```
docker inspect cs-ops --format '{{.Config.Image}}'
curl -ks https://localhost/healthz        # "code" is the fingerprint of the Python + SQL running
```

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

In Raven-Fleet: **Releases** → the new version → **Deploy** to the device.
Then on the VM, `docker logs -f cs-ops`. Watch for, in order:

```
{"event": "boot", "code": "<fingerprint>", "release": null, "config": {... "tls_cert_b64": "<2272 bytes>" ...}}
{"event": "migrated", "versions": ["001_foundation.sql", ...]}      first boot, or a new migration
{"event": "tls_material", "source": "env", "path": "/data/tls"}
{"event": "listening", "port": 8443, "tls": true}
```

Read the `boot` line before anything else. `tls_cert_b64` at 34 bytes is
the placeholder text, not a certificate; `oidc_client_secret` should say
`<value, not a reference>`. `release` is null until `OPS_RELEASE` is set in
the environment JSON.

**A missing `migrated` line on a first deploy means the volume already had a
database.** Worth stopping to understand rather than pressing on. On an
update it is normal: it appears only when the release carries a new
migration.

The container listens on 8443; Docker publishes it on **443** so the
registered redirect URI, which has no port, matches.

---

## 5. Confirm

On the VM, then from a laptop over the VPN:

```
curl -ks https://localhost/healthz
curl -s  https://ops.commsecurity.com.au/healthz
```

Expect `{"ok": true, "schema": {"expected": N, "applied": N, ...},
"integrity": "ok", "warnings": []}` plus `code`. The laptop form also
proves the certificate chain and the hostname: it needs `ca.crt` trusted
(§2b) and the name resolving (hosts entry until the A record exists).

Then the things `/healthz` cannot tell you:

- [ ] **Sign in with a real Workspace account.** On an EMPTY system the
      address in `OPS_BOOTSTRAP_ADMIN` receives every role on every entity
      (§3) and lands on the register. Anyone else, and any sign-in once an
      admin exists, provisions `viewer` on **zero** entities, so their
      correct result is a 403 from the project list, not an empty list
- [ ] **Grant a second person a role** from Access and confirm they see the
      register **without signing in again** — roles resolve per request
- [ ] **Grant `viewer` AND `operations`.** No role implies another, so an
      admin who is not also a viewer cannot read the register. This has
      caught us twice
- [ ] **Seed or import the data.** On 4 September the laptop database was
      restored over the empty one with `tools/restore.py` (CS-OP-RUN-001,
      pointed at a snapshot made with the SQLite backup API rather than a
      file copy). Otherwise import the register:
      `docker exec cs-ops python3 tools/import_register.py --csv ... --db /data/ops.db`
- [ ] **After a restore, sign out and in again.** The browser's session
      names a user id; the restored database may give that id to someone
      else. Every session that predates the restore must be re-established
- [ ] **Deactivate any dev account** that came across with the data, from
      Access, once you hold admin yourself

---

## 5a. Releasing an update — the routine

This is every deploy after the first. Done end to end on 4 September 2026
for the claim-plan wrapping fix and the Sign out button; nothing on the VM
is touched by hand, and the database is never involved.

**1. Prove it locally.** `.\dev.ps1` serves the working tree on
`http://localhost:5173`, TLS off, against `.\data`; `.\dev.ps1 -Session`
mints a cookie so no OIDC is needed. Static files are read per request, so
CSS and JS edits show on reload; Python edits need the server restarted
(`.\dev.ps1 -Stale` says whether it is behind the tree). Look at the actual
screen — a test proves the rule, the screen proves the result.

**2. Run what CI will run.** `py -3 -m unittest discover -s tests` for the
suite, `py -3 -m unittest tests.test_gates` for the gates alone (pinning,
secrets, the compose file, the deploy script). CI fails on exactly these,
so finding it here saves a round trip.

**3. Commit and push.**

```
git push origin main
```

CI builds the image and pushes `ghcr.io/commsecurityau/cs-ops:latest` and
`:<short sha>`. Wait for the green tick; the next step copies whatever is
there.

**4. Refresh the mirror.** Raven-Fleet → **Images** → **Add image** →
`ghcr.io/commsecurityau/cs-ops:latest`. This is the step that is easy to
forget and impossible to notice: without it the new release pins the
previous build, deploys cleanly, and `/healthz` shows the OLD `code`.

**5. Create the release.** **Releases** → new version. Paste the current
`docker-compose.yml` from the repo (a release stores its own copy, so a
compose change ships only when it is pasted in), the environment JSON, and
the host privileges JSON — the last two unchanged unless something in §3
changed. Privileges do not carry over between versions; paste them every
time.

**6. Deploy** the version to the device and watch `docker logs -f cs-ops`
as in §4. Then `/healthz`: `code` must differ from before (it is the
fingerprint of the running Python and SQL), `schema.applied` must equal
`expected`, `integrity` must be `ok`.

**What an update does NOT do.** It does not touch `/var/lib/cs-ops`:
the database, the certificate and the session key all persist, so nobody
is signed out. It does not roll back the database on failure: a release
that fails its health gate is rolled back to the previous image
automatically, but a migration it ran has run (§"If the deploy fails").
Migrations are expand-only for exactly this reason.

**Ownership of the steps.** 1–3 are the developer's. 4–6 are whoever holds
the Raven-Fleet login. Today that is the same person; it need not stay so.

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
| `sqlite3.OperationalError: unable to open database file` (older builds), or exit 2 "/data is not writable by the app (uid 100 ...)" | `/var/lib/cs-ops` is root-owned: `sudo chown -R 100:101 /var/lib/cs-ops`. This was the first deploy, twice |
| exit 2, "/data does not exist" | the bind mount is missing: the release lacks the host-privilege grant, or the directory was never created (§2) |
| exit 2, "OPS_TLS_CERT is set but is not base64 of a PEM file" | the release JSON still holds placeholder text; `tls_cert_b64` in the boot line reads `<34 bytes>` |
| exit 2, "OPS_TLS_CERT and OPS_TLS_KEY must be set together" | half a pair in the release JSON |
| exit 2, "no certificate at /data/tls/server.crt" | neither in the release nor on the volume |
| exit 2, "cannot be read ... runs as the non-root 'ops' user" | key ownership on a hand-copied pair |
| exit 2, "do not go together" | cert and key from different issuances |
| exit 2, "unresolved secret references" | `OIDC_CLIENT_SECRET` absent from the release JSON AND the store on the volume |
| exit 2, "OIDC is not fully configured: ..." | a missing env var, named |
| Compose: "required variable X is missing a value" | a key missing from the release's environment JSON |
| 403 after signing in | correct for anyone but the bootstrap admin — grant them a role |
| browser: certificate warning | `ca.crt` not trusted on that machine (§2b), or the site opened by IP — the certificate names the hostname only |
| sign-in bounces or 404s after Google | the app is not on 443, or the hostname does not resolve on that machine; Google will not redirect to an IP address |
| `/healthz` `code` unchanged after a deploy | the mirror was not refreshed (§5a step 4): the release pinned the previous build |
| `/healthz` 503, `missing: [...]` | migrations did not run; read the boot log |

---

## After the first deploy

Update this runbook **in place** with the four **[SITE]** answers and
anything that surprised you. A runbook that was written before the thing was
done, and never corrected afterwards, is a document that describes an
imagined system.

**What surprised us on 4 September 2026**, now folded into the sections
above so nobody meets them again:

- The compose file's `./data` landed in the fleet's release directory,
  root-owned and wiped on the next release. Absolute bind mount (§2, §3).
- The named volume the runbook assumed is not something the fleet offers;
  bind mounts need a privilege grant and a per-box ceiling (§3).
- The container is uid 100, not 1000 as an earlier comment said; a plain
  `mkdir -p` reproduces the failure exactly (§2).
- `latest` in the fleet's mirror is a copy that never refreshes itself
  (§1, §5a step 4).
- The registered redirect URI has no port, so the app had to be published
  on 443 (§4).
- Google refuses raw-IP redirect URIs, so "just use the IP" cannot work
  for sign-in; a hosts entry stood in for DNS (§5).
- The first user was a viewer with no admin anywhere to promote them, hence
  `OPS_BOOTSTRAP_ADMIN` (§3).
- A restore reassigns user ids under live sessions; sign out and in (§5).
- No internal CA existed; `tools/issue_cert.sh` is now the CA (§2b).
