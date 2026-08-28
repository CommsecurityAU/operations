# CS-OP-STP-001 — Delivery plan

- **As at:** 28 August 2026
- **Status:** Live. **Supersedes CS-OP-ARCH-001 §11 in full.**
- **Depends on:** CS-OP-ARCH-002 (stack, budgets, ADR-08…26)
- **Companions:** CS-OP-BUILD-001 (build status), CS-OP-RUN-001 (restore runbook)

ARCH-001 §11 was written against a stack that no longer exists — Go,
PostgreSQL, Caddy, a three-service compose file. ADR-08 through ADR-10
replaced all of it. Its Situations also quote pre-cleanup figures (49
projects, $7,299,574) that the 20–21 August source validation has since
moved, and three of its exit criteria are now contradicted by later ADRs.

This document rebases the plan. The **framing is unchanged** — Situation →
Target → Proposal, with exit criteria — because that framing did its job:
a phase that cannot describe what is wrong today cannot demonstrate it has
been fixed.

---

## Two standing rules

**Phases ship and are used in anger before the next begins.** Not built in
sequence and released together.

**On completion of a phase, the corresponding workbook tab is made
read-only** — or, where that is refused, drift between the two is **detected
on a schedule** instead (ADR-27). What matters is not that the workbook is
locked but that divergence cannot happen in silence. The Project List tab
stays editable and is covered by `tools/drift_check.py`; every later phase
should choose one of the two before it closes, and neither is optional.

---

## Migration ↔ STP map

Stated explicitly so nobody has to infer it again.

| Migration | STP | Contents |
|---|---|---|
| `001_foundation` | STP-0 + STP-1 | entity, period, users, user_entity_role, audit_log, client, project_type, project, job_code_alias, job_code_issue, job_number_sequence |
| `002` | STP-1 | job-number range columns (ADR-29) — **applied** |
| `003` | STP-2 | customer_po, customer_po_revision, claim_line, claim_line_revision, opening balances — **applied** |
| `004` | STP-2 | retention per PO, practical completion and DLP dates — **applied** |
| `005` | STP-2 | claim_schedule, renewal dates — **applied** |
| `006` | STP-2 | customer_po_revision kinds: variation vs correction — **applied** |
| `007` | STP-2 | contract value on the project (ADR-34) — **applied** |
| `008` | STP-2 | claim_item, claim_allocation, claim_amendment (ADR-37) — **applied** |
| `009` | STP-2 | a plan describes what is left to claim (ADR-39) — **applied** |
| `010` | STP-2 | an allocation owns its claim (ADR-38) — **applied** |
| `011` | STP-3 | supplier — **applied** |
| `012` | STP-3 | supplier_quote, supplier_po, procurement_line, supplier_invoice (ADR-40) — **applied** |
| `013` | STP-3 | supplier_alias: names resolved, never guessed (ADR-41) — **applied** |
| `014`, `015` | STP-3 | a state with nothing dated behind it — **applied** |
| `016` | STP-3 | an estimate is not a commitment (ADR-43) — **applied** |
| `017` | STP-3 | one definition of paid and delivered (ADR-45) — **applied** |
| `018` | STP-4 | office_expense_line, payroll_rate, tax_rate |
| `019` | STP-5 | rollup views only |

**STP-0 and STP-1 share migration `001`.** ARCH-001 assigned the project
register to STP-1's migration, but the register is what STP-0's exit
criterion renders, so splitting it would have meant a schema change between
two phases that ship together. The phases remain separate; the migration
does not.

---

## Current position

| STP | State |
|---|---|
| **STP-0** Foundation | Code complete, CI green, image published. **One exit criterion unmet: OIDC registration.** |
| **STP-1** Project register | Schema, importer, register screen and CRUD done. Job-number allocation built but switched off (ADR-28). Worklist screen built but unreviewed. Read-only tab outstanding. |
| STP-2 … STP-6 | Not started |

---

## STP-0 — Foundation

**Situation (historical).** The architecture existed on paper. No
repository, no environment, nowhere to deploy, and no mechanism enforcing
the §14 budgets.

**Target.** A running, authenticated, empty application on the internal VM,
reachable over TLS, with a repeatable deploy path and a backup **that has
been proven by restoring it**.

**Proposal — as built** (ARCH-001's proposal is void; ADR-08/09/10):

- Repository with `CLAUDE.md` carrying stack, budgets and ruled-out list
- One image on a digest-pinned `python:3.12-alpine`, one `/data` volume
- No Postgres, no Caddy, no compose stack, no pgweb — one Python process
- Migration runner: numbered, forward-only, one transaction each, explicit
  rollback (`sqlite3.executescript` does not roll back for you)
- Google Workspace OIDC, fail-closed claims, `sub`-keyed provisioning at
  `viewer` on zero entity grants
- CI enforcing every §14 budget, with the gates written as unittest cases
  so they run locally and on Windows
- In-process hourly `VACUUM INTO` snapshots; host rsync of `backups/` and
  `documents/` only

**Exit criteria.**

- [x] Staff sign-in over TLS at the internal URL → empty project list
- [x] Admin grant makes projects appear **without re-login**
- [x] A token with missing or wrong `hd` is observed to be rejected
- [x] One deploy performed end to end
- [ ] **OIDC client registered** — Cloud project inside the Workspace org,
      consent screen **Internal**, both redirect URIs
- [x] **One documented restore performed end to end, from the off-box copy** — 21 Aug 2026, full volume loss, 0.03 s, verified over HTTP (CS-OP-RUN-001)

**The OIDC registration is now the only outstanding item.** The restore
rehearsal was performed on 21 Aug 2026 and found what a rehearsal is for:
the backup excludes `secrets/` and `tls/`, so the app does not boot after a
restore until they are replaced. Deliberate, but invisible from reading §12,
and now step 4 of CS-OP-RUN-001.

Outstanding from the rehearsal: **install `tools/offbox_sync.sh` on the VM**.
The off-box copy the rehearsal used was created by hand; nothing is
syncing on a schedule yet.

---

## STP-1 — Project register & job number authority

**Situation.** Rewritten against the validated source, not ARCH-001's
pre-cleanup figures. The Project List tab now holds **59 rows** carrying
**$7,231,907** of contract value, reconciling to **$0.00 residual** against
`Purchase Order = Invoiced Prior + Contract Value FY27`. That is a much
better starting position than ARCH-001 described — because the cleanup
happened during the review rather than during the migration.

What remains wrong: job numbers are still issued by iTrade, so every
downstream integration still inherits whatever that produces; the FY split
is still maintained by hand; and eight rows carry codes that need a human
(`TBA` ×5, `na` ×1, plus `JN-4335` and `JN-4407` shared across two projects
each by work type). Two genuine collisions — `JN-676` and `JN-5416`, each
against unrelated sites — were resolved at source on 20 August.

**Target.** This platform is authoritative for projects and job numbers.
Every ambiguous code is **visible and owned**, not silently guessed.

**Proposal.**

- Migration `001` — **done**
- Importer emitting a worklist rather than guessing — **done**, verified
  against source, one bad row aborts the whole import
- `ops/static/` — `index.html`, `tokens.css`, `base.css`, `app.js`
  (`h` / `api` / `fmt`), `datatable.js`, plus the guardrail suite — **done**
- Project register screen over `/api/projects`, with type/client/status
  multi-select filters and totals that follow the filters — **done**
- Project CRUD; a project cannot exist without client, type and lead —
  **done**. Clients may be picked or typed; a typed near-miss folds into the
  existing record on a normalised key and the user is told, because
  `MSquared` / `M Squared` / `M-Squared` as three rows splits the by-client
  rollup and is painful to unpick once invoices reference all three
- Job numbers: allocation is built and **deliberately switched off**
  (ADR-28). Creation records the code iTrade gave us, or records `TBA` and
  puts the project on the worklist. When allocation is turned on it happens
  at commit, never on opening a form — a number handed out early leaves a
  gap every time someone changes their mind. `JN-6889` and `JN-6890` were
  issued before this was settled and are now permanent gaps
- Worklist screen over `job_code_issue`, resolution writing `job_code_alias`
  and an `audit_log` row — **built, not yet reviewed**

**Exit criteria.**

- [x] 59 projects visible in the platform and reconciling to $3,520,041.73
  orders in hand
- [ ] The next new job number is issued by the platform, not iTrade —
  **un-ticked 24 Aug 2026 (ADR-28)**. iTrade still issues, so the platform
  records the code it is given or records `TBA`. Allocation exists and is
  switched off; this criterion stays open until job-number authority
  actually moves.
- Every one of the 8 worklist rows is visible with an owner
- **Drift detection running on the Project List tab** (ADR-27), replacing
  the read-only rule for this tab. Run `tools/drift_check.py` after any
  session of workbook edits, and before any figure is quoted from either
  side

**Changed from ARCH-001.** Its exit criterion was "zero unresolved job
codes". ADR-23 replaced that with import-flagged: resolution gates **STP-5**,
not STP-1, so the register ships without waiting on a cleanup exercise. The
criterion here is *visibility with an owner*, which is what actually
protects against silent guessing.

---

## STP-2 — Customer invoicing

**Situation.** Invoicing and Future Invoicing are separate tabs joined by a
manual copy-forward ritual each month. Invoice numbers are captured
inconsistently — some `Inv No. 7250`, some bare, some absent. Customer POs
are adjusted in place, so every historical orders-in-hand figure derived
from a PO moves retrospectively.

**Target.** One fact table with a status lifecycle, replacing both tabs and
eliminating the copy-forward. History does not move when a PO is corrected.

**Proposal.**

- Migrations `003`–`005`: `customer_po`, `customer_po_revision`,
  `claim_line`, `claim_line_revision`, retention, schedules — **done**
- **29 synthetic opening `claim_line` rows** (ADR-22): dated 30 Jun 2026,
  `is_opening_balance = 1`, immutable, `customer_po_id` NULL, totalling
  $3,711,865.27
- `CHECK (customer_po_id IS NOT NULL OR is_opening_balance = 1)` — exactly
  one kind of claim line may float free of a PO
- Orders in hand becomes `sum(customer_po) − sum(claims up to X)`;
  `v_project_orders_in_hand` is replaced. **No financial year in the
  formula** — anything shaped like "claims since «hardcoded date»" is the
  workbook's July ritual reimplemented in SQL
- New scope is a new PO row; a correction is a `customer_po_revision`
- Month-end review flow; interactive invoicing grid (server-rendered table,
  no optimistic UI — the server's response is the truth painted back)
- Non-blocking report: `sum(customer_po)` vs `Purchase Order`, in four
  buckets

**Exit criteria.**

- One month invoiced end to end from the platform
- Opening rows reconcile: orders in hand still $3,520,041.73 at FY27 start
- The PO reconciliation report is produced and its exceptions have owners
- **Invoicing and Future Invoicing tabs read-only**

---

## STP-3 — Procurement & project expenses

**Situation.** Procurement is a Google Sheet a project engineer fills in and
the accounts team works from. A line is entered, emailed for approval, a PO
is raised, the supplier invoices, delivers and is paid — in whatever order.
Nothing connects it to what a project is worth.

**Target.** The register in the platform, on the same monthly axis as
invoicing, so committed cost meets claimed revenue.

**Status: BUILT, 28 August 2026.** 92 suppliers from iTrade; 58 register
lines, 22 quotes, 28 orders and 14 invoices imported, $160,501.20 committed,
every state matching the sheet. The screen edits everything and creates
quotes and orders from the row that needs them.

**What the model turned on** (ADR-40): payment and delivery are INDEPENDENT
facts, so they are dates and the state is derived; a quote may cover several
projects and carries the FX rate agreed with the supplier; one invoice
regularly covers several orders, so it links per line; and the foreign
amount is the fact, converted once at the extended total.

**No `fx_rate` table.** AUD and USD only, and the rate is agreed with the
supplier and fixed at quote — so it belongs on `supplier_quote` beside the
USD amount. The USD figure and the rate are the facts; the AUD is
reproducible from them forever.

**The expense forecast is in too.** 29 of the 31 orange-flagged cells in
the Project Expenses matrix, $1,567,877 — early estimates of future
procurement, kept apart from committed cost because they are ten times its
size (ADR-43).

**Still open.** Every supplier is missing an ABN, and one without is
withheld at 47%. Two projects are named in the source and absent from the
platform: `36 Wellington St - ICN Maintenance (JN-6963)` and `PDNSW - 6PSQ
L13 Tenancy Access Door`.

## STP-4 — Office expenses

**Situation.** Roughly $148,202 per month and $1,778,428 for FY27. The
summary block is almost entirely `#REF!` — total invoiceable, total project
costs and both financial summaries all fail. Payroll on-costs (WorkCover VIC
1.785%, NSW iCare 0.39%, Payroll Tax VIC 4.85% / NSW 5.45%, superannuation)
are spreadsheet formulas, so one wage change needs about seven manual edits.

**Target.** Category / subject / period model with on-costs derived from a
dated rate table. One wage change propagates automatically.

**Proposal.**

- Migration `018`: `office_expense_line`, `payroll_rate`, `tax_rate`
- Rates are **dated rows per entity, never configuration** (ADR-20). Every
  computed figure records the `rate_bp` it used, so changing a rate cannot
  restate a prior year
- Corporate tax seeded at 2500 bp — **estimate pending the accountant**;
  the 25%/30% split in the source may be a real per-entity difference, since
  base-rate-entity eligibility is assessed annually and per company
- Derivation engine driven by `payroll_rate`, with `is_derived` making
  computed lines visible
- Importer for both the FY26/27 and FY27/28 grids, mapping
  `FY26/27 → FY27` and `FY27/28 → FY28` **explicitly, asserted against a
  known row** — an off-by-one-year import is silent and survives every
  total-level reconciliation

**Exit criteria.**

- Monthly totals reconcile to the workbook within rounding for ≥ 3 months
- The `#REF!` chain no longer exists
- **Office Expenses workbook read-only**

**Sequencing.** Office expenses are independent of project data, so STP-4
can run in parallel with STP-2 and STP-3 if the dev team is working
alongside. It is the natural first hand-off.

---

## STP-5 — Operations Dashboard

**Situation.** The Financial Summary depends on both upstream workbooks and
inherits their faults. Its second monthly block shows `#N/A` for every
Office Expenses month, so Total Expenses silently equals Project Expenses
alone and Net Profit is overstated by roughly $150k per month. Total Project
Expenses reads $1,673,985 in one place and $1,683,036 in another on the same
workbook. No figure can be traced back to the rows that produced it.

**Target.** One dashboard over one source, where every figure is traceable
and no cell can be `#REF!` or `#N/A` by construction.

**Proposal.**

- Migration `019`: `v_project_financials`, `v_monthly_pl`, `v_dashboard`,
  `v_by_type`, `v_by_client` — views only, no new fact tables
- Operations Summary, monthly P&L, actual vs plan vs forecast, by type, by
  client, by project
- **Drill-through from every figure** to the rows behind it
- FY filter; dashboard p95 < 150 ms over the committed 250 k-row fixture

**Exit criteria.**

- **The `job_code_issue` worklist is empty.** This is the ADR-23 gate: a
  dashboard over unresolved codes is worse than no dashboard
- No rollup silently absorbs a flagged row — excluded totals are reported
  alongside, or surfaced as their own line
- Dashboard figures match independently computed totals with no unexplained
  variance
- **Financial Summary workbook retired**

---

## STP-6 — Xero reconciliation *(gated on API access)*

**Situation.** No API access yet. Actuals are keyed by hand from Xero into
the Invoicing tab. Nothing detects a divergence between what the platform
forecasts and what Xero actually issued.

**Target.** Actuals pulled automatically, matched on `job_code` +
`invoice_number`, with unmatched rows on **either** side presented as a work
queue rather than an error.

**Proposal.**

- OAuth 2.0 with the restricted API user and scoped app per CS-OP-SOW-001
  (retained as the reference for Xero mappings, superseded elsewhere)
- Staging tables retaining the raw payload; transform separately
- Token refresh job; refresh token held in the secret store, never on file
- Matcher and variance report — **read-only, no write-back**

**Exit criteria.** A month closed by reviewing a variance report rather than
re-keying invoices.

---

## Cross-cutting — not phases, but not optional

**Restore rehearsal, monthly.** From the **off-box** copy, not
`/data/backups/`. Record the elapsed time against the §14 60 s budget. First
one performed 21 Aug 2026 (0.03 s); next due 21 Sep.

**Tag a release before each migration.** Until a `v*` tag exists the CI
`n1` job no-ops, so the first schema change after a deploy gets no N-1
check — which is exactly when one is worth having.

**Second legal entity.** Schema is ready from `001` (`entity_id NOT NULL`
everywhere). Activating CommSecurity Pty Ltd or RAVEN BOX is a UI change and
a set of grants, not a migration. Do it when a real project needs it.

**Source data comes from CSV exports, never the Drive markdown
conversion.** The conversion drops rows and merges tabs; on 25 Aug it cost
an hour and produced a $126,268.91 discrepancy that did not exist. Every
importer takes CSV.

**Rounding verification — done, 24 Aug 2026.** The three pinned FY27 totals
are identical under half-up, banker's and truncation: no source value in the
register carries sub-cent precision, so nothing in STP-1 rounds. `ops/money.py`
now holds the single rounding function ADR-15 requires, ahead of STP-2 where
GST makes the mode start to matter.

---

## Deliberately deferred

Operational project management — task trackers, IBP system categories,
commissioning checklists, the "120 Balmain Road" style content. The data
model reserves room and nothing in STP-0 through STP-6 forecloses it.

The financial picture is the one that is currently broken. That is the one
being fixed first.
