# CS-OP-BUILD-001 — Build status

- **As at:** 24 August 2026 (end of day)
- **Repo:** `C:\Dev\operations` → `git@github-roberts:CommsecurityAU/operations.git`
- **Spec:** CS-OP-ARCH-002 (locked; changes require an ADR in §16)
- **Plan:** CS-OP-STP-001 (delivery phases; supersedes ARCH-001 §11)
- **Runbook:** CS-OP-RUN-001 (restore; rehearsal record)
- **Phase:** STP-0 Foundation — **code complete, CI green, image published**

---

## Where things stand

| Piece | State |
|---|---|
| `ops/migrations/001_foundation.sql` | Done — entities, 144 periods, identity, project register, job-code worklist, job-number sequence |
| `ops/db.py` | Done — write/read connection split, pragmas, migration runner, health check, write methods |
| `ops/secrets.py` | Done — `secret://` resolver, 0600 store, stdin-only writes, fail-loud boot |
| `ops/http_util.py` | Done — four hardening settings, routing, security headers, CSRF |
| `ops/auth.py` | Done — OIDC fail-closed claims, HMAC identity-only sessions, per-request roles |
| `ops/config.py`, `ops/backup.py`, `ops/main.py` | Done — boots end to end |
| `tools/import_register.py` | Done — validates and imports the FY27 register, one-shot |
| `Dockerfile`, `Makefile`, `.github/workflows/ci.yml` | Done — full pipeline green |
| `tools/restore.py`, `tools/offbox_sync.sh` | Done — rehearsed 21 Aug, 0.03 s |
| `ops/static/` | Done — register screen, editor, tokens, datatable, guardrails |
| `ops/modules/projects.py` | Done — CRUD, validation, client find-or-create |
| `ops/modules/worklist.py` | Done — four actions, cascade, mandatory reasons |
| `ops/money.py` | Done — the one rounding function (ADR-15) |
| `tools/drift_check.py` | Done — workbook vs platform (ADR-27) |
| `tools/job_number_range.py` | Done — reserve a block (ADR-29) |
| `tools/dev_session.py`, `dev.ps1` | Done — Windows dev loop |
| `tests/` | **382 tests**, ~7 s Linux, ~13 s Windows |
| Deploy to the internal VM | **Next** |
| `ops/modules/`, `ops/render.py` | Not started |

Measured against the §14 budgets: **image 47 MB** (limit 75), **suite ~6 s**
(limit 10; ~11 s on Windows, where temp-file work is dearer — CI measures on
Linux, but the local run is worth watching), **pyright --strict 0 errors**, **0 pip deps**, **0 npm**.

```
py -W error::ResourceWarning -m unittest discover -s tests     # Windows
make test                                                      # container / CI
make check                                                     # test + gates
```

STP-0 was verified end to end against a running container, not only in
tests: `/healthz` returns `{"ok": true, ...}`, and the acceptance path was
walked over HTTP — sign in with zero grants → 403, admin grants viewer →
same cookie, no re-login → 59 projects and $3,520,041.73, token bump → 401.

---

## Source data — validated, reconciles to zero

| | |
|---|---:|
| Projects | 63 |
| Purchase Order | $7,232,657.00 |
| Invoiced Prior (25 opening rows) | $3,670,405.27 |
| **Orders in Hand, FY27 start** | **$3,562,251.73** |
| Residual | $0.00 |

**FY27 to date**, after the claims import (202 claims):

| | |
|---|---:|
| Invoiced Jul-26 + Aug-26 | $457,655.34 |
| Forecast, Sep-26 onward | $3,203,976.74 |
| **Orders in Hand, today** | **$3,104,596.39** |

`Monthly Data` is a pivot of Invoicing, Future Invoicing and the register.
It reconciles to the detail **exactly**, every project, every month — which
is worth asserting on every import: a pivot disagreeing with its own source
means a row was missed on the way in.

`Purchase Order == Invoiced Prior + Contract Value FY27` holds on every row.
The importer **asserts** this rather than deriving it; one bad row aborts
the whole import before anything is written. Pinned in
`tests/test_import_register.py` as cents: `723265700`, `367040527`,
`356225173`.

The `Invoiced FY26` → `Invoiced Prior` rename was the important cleanup:
five DLP projects had FY25 billing the old column never reached, so
sourcing opening balances from it would have understated them by
**$858,354** and shown that much in phantom orders in hand.

**Worklist carried in — 8 rows, no merged history.** Class B (6): `TBA` ×5,
`na` ×1. Class C (2 codes): `JN-4335`, `JN-4407` — one customer job number
per site, two projects by work type, which is why `job_code_alias` is
one-to-many. Leave alone: `P-3655`, `P-3707`, `JN-CommS`. Resolution gates
**STP-5**, not STP-1.

---

## What changed 25 August

**Worklist** reviewed and working against the real 8 rows. Four actions
rather than one Resolve button, a mandatory reason for the judgement calls,
and reissuing one side of a shared code auto-closes the sibling. One real
bug found by running it: reissuing a class C demanded a typed reason, which
blocked the most natural response to a shared code.

**Project CRUD** with client find-or-create. A typed near-miss folds into
the existing record on a normalised key and the user is told, because
`MSquared` / `M Squared` / `M-Squared` as three rows splits the by-client
rollup and is painful to unpick once invoices reference all three.

**`ops/money.py`** — the single rounding function ADR-15 has required since
the review. The pinned FY27 totals are identical under every rounding mode
(nothing in STP-1 rounds), but the check found the importer TRUNCATING
sub-cent values. Harmless on this register, consistently downward on the
office-expense grids and on Xero.

**Drift detection (ADR-27)** replaces the read-only-tab rule for the Project
List. It found 16 real differences on its first run, including two projects
carrying different job numbers on each side.

**Job numbers (ADR-28, ADR-29).** That finding changed a decision: the
platform no longer allocates. iTrade still issues, so a number allocated
here could collide with one issued there tomorrow, surfacing only in Xero.
Creation records the code we were given or records `TBA`; allocation
requires a reserved block and refuses until one is agreed.

---

## What changed 25 August — STP-2

**Migrations 003, 004, 005.** Orders in hand now derives from
`sum(customer_po) − sum(claims invoiced)` rather than two columns on
`project`, and the figure did not move: a test zeroes the legacy columns and
asserts the view is unmoved, because otherwise "we changed the source" is a
claim rather than a fact. **No financial year appears in the view** — a test
greps for one — because `contract − claims up to X` answers FY27 opening,
FY28 opening and today with a single definition.

**Retention (004)** belongs to the **PO, not the project**: a variation that
raises the PO raises its cap with it, and scope run as a separate PO carries
its own terms or none. Withholding is computed **at invoicing, not at
creation** — two forecasts each computed against the same remaining capacity
would both take the full 10% and together breach the cap the moment both
were invoiced.

**Schedules (005).** Maintenance is one agreement spread over a year, not
twelve claims someone typed. Generation is idempotent by database
constraint, not by a check in the code. Renewal dates carry a notice period,
and overdue renewals sort first and stay in the list.

**Claims imported: 202.** Every figure reconciles to the workbook.

**`tools/sync_register.py`** completes ADR-27: `drift_check` finds
differences and never writes; this applies the safe ones. It corrects
opening balances despite their immutability triggers — standing them down
for exactly as long as the correction takes, restoring them in a `finally`,
requiring a reason, auditing each one. An opening balance is a migration
artifact, not an invoice anyone issued, so a wrong one has to be
correctable.

---

## Open items

0. **Install `tools/offbox_sync.sh` on the VM** (hourly cron). The restore
   rehearsal passed on 21 Aug, but the off-box copy it used was made by
   hand — nothing is syncing on a schedule yet, so there is currently no
   off-box backup.
0b. **Put `OIDC_CLIENT_SECRET` in the company password manager.** The backup
   deliberately excludes `secrets/`, so if that value exists only on the
   `/data` volume, a volume loss is unrecoverable without re-registering
   the OIDC client.
1. **Agree a job-number block with whoever runs iTrade**, then
   `py tools\job_number_range.py --db data\ops.db --from 9000 --to 9999
   --note "agreed with <name>, <date>"`. Until then allocation refuses,
   which is the correct default (ADR-29).
2. **Register the OIDC client.** Cloud project inside the Workspace org,
   consent screen **Internal**, redirect URIs
   `https://ops.commsecurity.com.au/auth/callback` and
   `http://localhost:5173/auth/callback`. **The only thing between here and
   STP-0's exit criteria.**
3. **Run `drift_check.py` after any session of workbook edits.** Sixteen
   differences are outstanding from the 24 Aug run: twelve project leads
   filled in on the sheet since import, two projects added there, and two
   job codes to correct in the platform.
4. **Tag a new release.** `v0.1.0` predates the fixes to two tests, so the
   N-1 job fails against it for reasons that are not incompatibility. Tag
   from current HEAD and the next run has a healthy baseline. Until a release tag exists the `n1` job no-ops with
   "no release tag yet", so the next migration gets no N-1 check — which is
   exactly when one is worth having.
5. **Confirm the corporate tax rate with the accountant.** 25% (2500 bp) is
   recorded as an estimate; the 25%/30% split in the source may be a real
   per-entity difference.

6. **Minor, source data:** 50 Queens Rd shows *Live, 50%* on the Project tab
   and *DLP* on the register. One is stale.

---

## Things that cost time — don't rediscover them

**Tests that pass for the wrong reason**

- Concurrency tests built from SINGLE SQL statements are theatre. The first
  pair here passed with the write lock deleted, because SQLite's own mutex
  makes single statements atomic. A lost update needs a read AND a write in
  one transaction. Mutation-test every safeguard: remove it, confirm a test
  fails. Done for the lock, the four secrets safeguards, seven HTTP
  hardening settings and eight auth checks.
- `server.timeout` is NOT the connection read timeout — it is the
  `handle_request()` poll interval. The guarantee comes from the HANDLER
  class attribute. The original code had no read timeout at all while a
  green test claimed otherwise.
- A gate's exit code must come from the gate. `pyright … || pyright …`
  meant the retry decided the exit status, so the type gate could not fail.

**Things only Windows catches** (CI is Linux, container is Alpine)

- Read connections opened by other threads were never closed. Linux unlinks
  open files happily; Windows raises `WinError 32`.
- The connection-cap 503 was lost to an RST, because closing a socket with
  unread data in the receive buffer discards queued output. Linux delivered
  it anyway; Windows raised `WinError 10053`. **Removing the fix still
  passes on Linux** — only detectable on Windows.
- Keep test teardown strict (no `ignore_errors`); that failure is the leak
  detector. Run the suite on Windows periodically.

**Budgets that are about to bite**

- Per-page JS is **45.5 KB against the 50 KB limit** (§14) with the invoicing
  grid added. One more screen of that size fails the gate. The answer is not
  to raise the budget: it is that every screen currently loads every module,
  so the fix is loading a screen's JS when the screen is opened.

**Platform and library traps**

- `sqlite3.executescript()` does not roll back on failure — it leaves the
  transaction open and completed statements in place. The runner's explicit
  `rollback()` is what stops a failed migration leaving a half-applied
  schema.
- It also commits any pending transaction first, so the runner must wrap the
  script text in `BEGIN`/`COMMIT`; a `BEGIN` issued beforehand is discarded.
- SQLite has no `%y` in `strftime`, only `%Y`.
- No private stdlib APIs. `ssl._ssl._test_decode_cert` was load-bearing for
  cert-expiry warnings until pyright found it. Replaced with a DER walk,
  verified against openssl.
- `serve_forever()` polls at 0.5 s and `shutdown()` waits for the next poll
  — 500 ms of teardown per socket test. Pass `poll_interval=0.01` in tests
  (14 s → 2.4 s).
- The access log must record what was actually SENT. Handlers that write
  their own response (redirects, 204s) return `None`, and defaulting those
  to 200 hides exactly the responses you go looking for during an incident.
- On Windows use `py`, not `python3`; `curl` is an alias for
  `Invoke-WebRequest` (use `curl.exe` or `irm`). Inside the container and in
  CI, `python3` is correct — don't change it there.
- PowerShell here-strings can drop a leading dot: `.dockerignore` saved as
  `dockerignore` and silently sent the whole repo as build context.
- A downloaded `.ps1` carries the Mark of the Web and is blocked even under
  RemoteSigned, EVERY time it is re-downloaded. `Unblock-File .\dev.ps1`.
- Tools do NOT migrate; the app does, at boot. So any tool run against a
  stale database must SAY the database is behind rather than failing on a
  missing column.

**The most expensive mistake so far**

- **The Google Drive markdown conversion is NOT a data source.** It silently
  dropped rows from one tab, merged two unrelated tabs into another, and
  reported 131 rows where the CSV export has 147. On the strength of it I
  spent an hour telling Richard his workbook was missing $126,268.91 of line
  items that were there all along, and pushed back twice when he said so.
  **Read structure from it if you like; take every figure from a CSV
  export.** The tell was present early and I missed it: the pivot in the
  SAME export already accounted for rows the detail tab appeared to lack —
  which is only possible if the export, not the workbook, was wrong.
- Corollary: when a control total and its own detail disagree, suspect the
  reader before the data. Two independent parses agreeing means nothing if
  both read the same corrupted copy.

**Checks that were wrong about themselves**

- N-1 runs the OLD TAG'S tests, which are frozen — so it fails when an old
  test hardcoded schema details or was flaky, neither of which is an
  incompatibility. Both happened on its first real run. The remedy is to tag
  a new release from a commit whose tests are correct, never to weaken the
  gate. Confirm the migration is expand-only by inspection; that is the
  check that actually matters.

- Naive string search over source produced three false failures: `innerHTML`
  in the comment banning innerHTML, `round()` in the docstring explaining
  why round() is not used, and `INSERT` matching `sys.path.insert` after
  uppercasing. All three are now `ast`-based. **Parse code; do not read it
  as prose.**
- Undelivered work is worse than unwritten work, because it looks like
  nothing at all. A finished worklist implementation sat in the working copy
  for a day and a second one was started before it was found.

**Windows dev-loop traps** (all cost real time; all now have a guard)

- BEFORE debugging code when tests pass and the browser disagrees, find out
  WHAT IS ANSWERING THE PORT. A stale Docker container held 8080 for half an
  hour of misdiagnosis. `.\dev.ps1` now names the occupant before binding.
- Python loads a module once, so a running server can be several edits
  behind the working tree. `.\dev.ps1 -Stale` compares the `/healthz` code
  fingerprint against the on-disk one. Four separate confusions came from
  this before it existed.
- Windows reserves port ranges for Hyper-V/WSL; a bind inside one fails with
  `WinError 10013`, which mentions nothing about reservations. Dev port is
  now 5173.
- A downloaded `.ps1` carries the Mark of the Web and is blocked even under
  `RemoteSigned`. `Unblock-File .\dev.ps1` once.
- A read-only command must not write state: `-Stale` used to create the dev
  secret store on the way past.

---

## Resume point

**Tomorrow, in order of value:**

1. **Schedule UI.** 36 Wellington, 200 Victoria and 627 Chapel each carry
   twelve hand-entered rows. The model is built and tested; there is no
   screen to set one up. This is the one thing here the workbook genuinely
   cannot do, and the only item that removes recurring work rather than
   recording it.
2. **Retention terms per PO.** Every PO currently reads
   `retention_applies = 0` because there is no way to set them. Richard is
   assigning these per project.
3. **PO management** — a second PO on a project, and a variation recorded as
   a revision. `update_project` currently moves the migrated PO as a side
   effect; that is the expand-window compromise and should not outlive it.

**Outside the phase, and now more pressing:** deployment. The platform holds
real invoicing data and there is still no off-box backup. The deferral note
in CS-OP-RUN-002 said the deadline was not the VM but "the moment STP-2 can
carry real data" — that moment has arrived.

**Small:** `222 Bourke St` and `Samma Pl` exist in the workbook, not the
platform. They need creating through the UI because each needs a job-code
decision (ADR-28).

---

## Where to pick up

```powershell
cd C:\Dev\operations
.\dev.ps1                 # serve on 5173
.\dev.ps1 -Stale          # is the running server current?
py -W error::ResourceWarning -m unittest discover -s tests   # expect 503
py tools\drift_check.py --csv "<register>.csv" --db data\ops.db
```

Code fingerprint at end of day: `7b36b58bd776`. Migrations applied: `001`
through `005`.
