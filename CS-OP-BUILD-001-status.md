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

## What changed 26 August

**Retention is loaded and reconciling.** The register carries one number per
project — the cap, as a percentage — and the rest is the standard agreement:
10% withheld per claim until the cap, half released at practical completion
and half at DLP end. Seven projects, **$115,029 of capacity**.

**Retention on pre-FY27 invoicing is counted: $82,240.36.** Those invoices
were issued and the customer held money against them; on three of the seven
the cap was reached before the platform's window opened. The figure is
DERIVED (rate x opening, capped) because the workbook never recorded what
was actually withheld — so it is stored rather than computed on read, can be
corrected when the real number is known, and its audit entry says
*"derived ... (not recorded in the workbook)"* rather than stating it as
fact. A test asserts that wording: an inference that reads like a fact is
how a bad number survives.

**Forecasting across months.** The invoicing grid loads a financial year and
filters client-side on FY, month, project, type, client, status and
retention state. The month cell IS the move control on a forecast row —
change it and the claim moves, no dialog — because that is the activity, not
an exception to it (ADR-32).

**A DLP date is derived from practical completion + 12 months** where only
PC is known, flagged `estimated`, and never written to the project. A date
nobody agreed to becomes a fact the moment it is stored.

**Tables use the screen.** `.content` had `max-width: 1200px` — a
readability constraint borrowed from prose that was throttling every table
and forcing a sideways scroll. Free-text columns truncate with an ellipsis
and keep the full value in `title`.

**CSV export** on both grids, exporting what is on screen: the filtered set,
every page. Money leaves as `1234.56`, not `$1,234.56`, because a formatted
string arrives in Excel as text that will not sum.

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

**Two fingerprints, because there are two questions (ADR-35)**

- `code` — is the running server the Python on disk? Answers the stale
  MODULE problem, which has cost five round trips.
- `assets` — is the static on disk what was delivered? Answers the stale
  FILE problem, which `code` deliberately could not: static is read per
  request, so the server is never stale with respect to it, and that
  correct reasoning answered the wrong question.

**Names that describe the source rather than the thing**

- `register_state` held what the procurement sheet said. When the grid
  gained a dropdown, the same column held what a person said — the same
  fact, a state with nothing dated behind it. Renamed to `stated_state`
  immediately, because **this is exactly how `purchase_order_cents` came to
  mean contract value** (ADR-34) and cost a day.

**A whole class of error the suite could not see**

- `datatable.js` used a module constant that was never declared: one edit
  added it, a later edit to the same region removed it. `ReferenceError` on
  every page, the register would not render — and **696 tests passed**. The
  Python suite never executes the JavaScript and the Alpine image has no
  runtime to execute it with. There is now a static scan for CAPS constants
  used but not declared or imported, with a mutation test, because a scan
  that matches nothing passes silently.

**Checks that were wrong about themselves**

- A guardrail nobody has tried to break is a guarantee nobody has checked.
  The filter-ordering test looked for `col.sortKey` ANYWHERE in
  datatable.js; the header-sort code contains that string too, so breaking
  the filter order left it green. Mutation-testing found it. Scope a check
  to the function whose behaviour it describes.

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

## What changed 26 August, later

**Schedules.** Recurring maintenance is one agreement, not twelve rows a
year. A new schedule ADOPTS the claims that already exist rather than
generating over them — every recurring project in the register arrived with
its rows already typed, so generating first would have doubled the year.
Coverage reads as a fraction (`12 / 12`), because "12 claims" never said
whether that was all of them. Renewals lead the screen and appear on the
register too; only overdue and due ones, because a reminder that is always
present stops being a reminder.

**Customer orders (ADR-34).** The correction that mattered most today. The
register's `Purchase Order` column was the CONTRACT VALUE, and migration 003
had turned it into a customer order — so adding the real POs alongside it
double-counted. `200 Victoria - IBP` read $422,833.33 against a $295,000
contract. Contract now belongs to the project; POs are what has actually
been ordered; orders in hand keeps its original meaning.

**A project row expands to its orders**, with add, edit, revise, move and
delete. A revision states whether it is a VARIATION (the contract grew, on a
date) or a CORRECTION (the figure was always wrong) — identical in the
numbers, and distinguishable only if someone says which at the time.

**Remaining versus forecast**, per project: everything still to bill ought to
sit in a month somewhere. 56 of 65 projects agree. The nine that do not are
findings — `88 Robertson St - QLD` plans $173,350 against $93,350 left,
which usually means an unrecorded variation.

**Smaller:** CSV export of whatever the filters leave; type colour chips at
low chroma so they group without competing with an alarm; the brand navy and
orange in the chrome only; the CSSB mark as a favicon; a `no route for
METHOD /path` message so a stale server stops looking like a bad id.

---

## What changed 27 August — the claim plan

**The gap that mattered.** A claim line was the atom and there was no way to
make one: every claim in the platform came from the importer, so a project
created here could not have a dollar of its invoicing planned. The
progress-claim workbooks did that work, and this is the layer that replaces
them (ADR-37).

    A contract splits into ITEMS with values.
    Each item is spread across months by PERCENTAGE.
    A month's claim is the SUM of that month's contributions.

`720 Bourke St` reproduces to the cent — three phases, nine allocations,
seven months totalling $198,610.00, and `Jul-26` producing nothing because
nothing is allocated there.

**Adoption, not re-entry.** Every project imported from the workbook arrived
with its forecast already typed, and a panel saying `no plan yet` beside
thirteen forecast claims is lying by omission. Adopt builds the plan from
what is there.

---

## The day's real lesson

**Four separate bugs, all from one assumption I made and the register
disproves.** I decided a project has at most one claim per month.
`200 Victoria - IBP` has five in Sep-26, and two in Aug-26 sharing
`Inv No. 6072/5` — which is the answer: a claim line is one CONTRIBUTION and
an invoice groups them (ADR-38).

What that assumption cost, in order of discovery:

1. **Generate doubled the money.** Adoption did not mark the claims it built
   the plan from, so generation created a second set. $30,000 of forecast
   became $60,000, silently.
2. **Generate inflated a month.** Five claims, one updated to the month's
   whole total, four left standing: $88,500 became $159,300.
3. **Moving one claim moved the whole month**, and moving it back was
   refused as `occupied` — so the plan silently stayed put while the claim
   returned.
4. **Rebuild threw on every project with an opening balance**, because it
   cleared `from_plan` across the project and those rows are immutable. The
   UI reported `internal error` and the plan quietly stayed as it was.

**None of the four was caught by a test.** Every one was found by the Ops
Manager using the screens on real data, and every one was invisible to a
suite that asserted the model as designed.

**And a fifth, upstream of all of them:** the claims importer mapped the
workbook's `Phase` column and never its `Task`. `Task` is the LINE ITEM —
`Client Training`, `SAT`, `Design - Stage 2` — so grouping had nothing to
work with and five tasks collapsed into the phase above them. Fixed at the
importer, with `tools/backfill_task.py` for the 106 claims already loaded.

---

## What changed 28 August

**OIDC works end to end.** Client registered Internal in the Workspace org,
both redirect URIs, signed in as a real account. The dev cookie is a
fallback rather than the way in, and roles are granted from an **Access**
screen instead of a Python script — the last admin on an entity cannot be
removed, including yourself, because a system nobody can administer needs a
database client to recover.

**STP-3 is built.** 92 suppliers imported from iTrade, and the procurement
register — 58 lines, 22 quotes, 28 orders, 14 invoices, **$160,501.20
committed** — with every state matching the sheet exactly.

The screen does the two things the workbook cannot: recording WHEN
something was delivered or paid rather than what state somebody last typed,
and showing committed cost against the project it belongs to. EOM and state
are dropdowns in the grid; a quote or an order can be created from the row
that needs it, because the reason it does not exist is that nobody had
reached that line yet.

**Five register rows that looked wrong were right.** `$33.00 x 7` at
1.388561 is $320.76 converting the extended total and $320.74 converting
per unit. The sheet converts last, and so does `Db.extend`.

---

## The expense forecast, 28 August

**$1.57m of estimates are in.** The Project Expenses matrix carries early
estimates of future procurement, flagged in orange: 31 cells,
$1,576,928.29, of which 29 could be placed. They are ten times the value of
the real orders, so `is_estimate` keeps them apart and every view reports
committed, estimated and forecast separately (ADR-43).

**Reading the flags took three attempts and taught something.** Colour
survives no export, so a script runs inside the workbook — but the legend
cell says `#F26722`, the brand colour, while the flags are `#FF9900`,
Google's palette orange. Nobody types a hex when flagging a cell; they pick
the nearest swatch. A tolerance of 90 found nothing while looking at 31
orange cells, and the first attempt read the background when the flag is on
the FONT (ADR-44).

**The confirmation is worth keeping.** From Oct-26 onward every month in
the matrix is entirely estimate, matching the sheet's own monthly totals to
the dollar — and `Jun-27` is short by exactly the one row that could not be
placed.

    Oct-26   $94,381   all estimate      Feb-27  $117,000   all estimate
    Nov-26   $13,000   all estimate      Apr-27  $200,954   all estimate
    Dec-26   $25,000   all estimate      May-27  $389,900   all estimate

**And a screen that disagreed with itself.** Twenty rows read `complete` or
`paid - pending delivery` while the Paid figure said $0.00, because the
figures counted dates and the state column read the stated state as well.
`is_paid` and `is_delivered` are computed once now, in the view (ADR-45).
Paid reads $34,415.74.

---

## STP-4, 29 August — office expenses

**Both sides of the business are now in one place.** Contract, claims and
retention on one side; suppliers, orders, the expense forecast and now
payroll and overhead on the other.

    projects              65      contract      $7,233,942.00
    suppliers             94      invoiced      $4,129,345.61
    procurement lines     87      committed       $160,501.20
    expense lines         54      estimated     $1,567,877.00
                                  office FY27   $1,774,311.54

**A salary is the fact and the months are its consequence** (ADR-47). The
sheet stored eighteen monthly figures per person, so a rise had to be typed
twelve times. Now a salary revision carries an effective month, super is 12%
of that person's wages, and Work Cover and payroll tax are a rate on wages
plus super for their state. Change one salary and five other lines follow.

**`finance` is a fifth role** (ADR-46), implied by nothing including admin.
These figures are wages.

---

## What computing it found

**Three errors in the sheet, none of which anyone had noticed.**

1. **VIC payroll tax froze** when wages rose in Oct-26 while Work Cover
   moved with them: **$792.16 a month, $16,635.36** across the two years
   shown.
2. **NSW Work Cover computed 0.405%** on a line called 0.39%. Here — unlike
   the `#F26722` legend or the `Phase` column — **the label was the correct
   one**, which is why it was worth asking rather than assuming the usual
   direction.
3. **The sheet's own total row exceeds the sum of its own rows** by a
   constant $526.39 before October and $1,416.56 after.

A figure that has to be dragged across a row by hand is a figure that
eventually is not.

---

## The same defect, three times in one week

A field the API accepts and the database drops. **Accepted-and-ignored is
worse than refused, because the screen says it worked.**

- `project_id` on a procurement line — moving a cost to the right job
  appeared to work and did nothing.
- `threshold_annual_cents` on an expense line, days later, in the module
  written after the first.
- `expense_line.note` — in the table, accepted by the API, **missing from
  the view**, so the dialog would have shown it blank and saved blank.

Three routes to the same outcome. There are now gates for all three: the
mutable list against what each module offers, and what the dialogs read
against what the view provides. Each was verified by breaking it.

---

## Access, and what it costs to see a salary

**Six roles now** (ADR-49). `finance` opens the expense screen — the rent,
the subscriptions, the total cost of running the business, which reporting
will need. `payroll` is what shows individual salaries, and it is a separate
grant implied by nothing, including admin.

| To do this | Needs |
|---|---|
| Open Expenses | `finance` |
| See or set a salary | `payroll` **+** re-authenticated within 15 minutes |
| Export with salaries | the same |
| Grant either | `admin` |

**A salary is withheld, not hidden** (ADR-50). `annual_cents` is not in the
response at all, so it is not in the network tab either — hiding a figure in
the interface hides it from nobody with the developer tools open. There is
no password in this platform, so demanding one means demanding a fresh
Google authentication; `prompt=login` gets it. Every salary viewed is
audited.

**Categories start collapsed**, so signing in to finance shows what the
business costs rather than eleven people's monthly pay.

---

## STP-5, 1 September — the dashboard

**Every phase is built.** The screen the workbook chain was doing badly, and
the reason for all of this:

    revenue          $3,529,018.00
    project cost     $1,726,158.20
    office cost      $1,774,311.54
                    ---------------
    gross profit        $28,548.26
    corporate tax        $7,137.07
    net profit          $21,411.19

Nine cards, three SVG charts drawn without a library, a monthly table, cost
by category, the largest contracts, and invoicing by project by month. It
lands on the year we are in.

**Two rules it turns on, and the workbook got both wrong** (ADR-51). Tax is
assessed on the YEAR — the sheet taxed profitable months and gave no credit
for losses, a quarter of a million out from the block sitting beside it. And
office overhead comes off the bottom line rather than being spread across
jobs, which would invent a margin nobody agreed to.

**What happened is marked apart from what is expected** (ADR-52). Actual
months against projections, billed revenue against forecast, committed cost
against estimated. The workbook mixed them silently in every row.

---

## Resume point

**STP-0 through STP-5 are built.** The work left is not building.

### Tomorrow, in order

1. **RESYNC FROM SOURCE.** Projects, invoicing and expenses have all moved
   since the imports. Fresh CSV exports, then:

   ```powershell
   Copy-Item data\ops.db data\ops-before-resync.db
   py tools\sync_register.py    --db data\ops.db --csv "$d\...Projects.csv"
   py tools\import_claims.py    --db data\ops.db --invoicing ... --future ... --matrix ... --sync
   py tools\drift_check.py      --db data\ops.db --csv "$d\...Projects.csv"
   ```

   Office expenses are one-shot, so a changed sheet means deciding whether
   to edit in the platform or rebuild that table. Worth thinking about
   before running anything.

2. **CONFIRM THE FIGURES** against the dashboard: contract value, invoiced,
   orders in hand, and the FY27 bottom line.

3. **DEPLOY**, then **BACKUPS**. CS-OP-RUN-002 has four **[SITE]** gaps
   needing answers when the VM appears, and `tools/offbox_sync.sh` has
   never run anywhere. Everything above exists once, on one laptop.

### Still open, none blocking

- **Two projects are missing** with rows waiting: `36 Wellington St - ICN
  Maintenance (JN-6963)` and `PDNSW - 6PSQ L13 Tenancy Access Door`.
- **ABNs** on all 92 suppliers; one without is withheld at 47%.
- **The iTrade job-number block**, which four `TBA` rows wait on.
- **UI debts**: the project panel closes on every action; a 500 reads as
  `internal error` while the traceback sits in the terminal; the register's
  `Invoiced prior` column now means all invoicing.

---

## Where to pick up

```powershell
cd C:\Dev\operations
Get-ChildItem -Recurse -File | Unblock-File
.\dev.ps1                 # serve on 5173, then sign in with Google
.\dev.ps1 -Stale          # code AND assets, against the delivered values
py -W error::ResourceWarning -m unittest discover -s tests   # expect 927
```

Fingerprints: code `52fd0b92618b`, assets `faee16d54b0d`.
Migrations applied: `001` through `023`.
Roles: `viewer`, `operations`, `approver`, `admin`, `finance`, `payroll`.
