# CommSecurity Operations Platform — Architecture

| | |
|---|---|
| **Document ID** | CS-OP-ARCH-001 |
| **Description** | Architecture & approach for the internal Operations / Project Management application |
| **Revision** | **Draft** \| Released \| Final |
| **Author** | Richard Roberts |
| **Date** | 19 August 2026 |
| **Related** | CS-OP-SOW-001 (Operations — Scope of Works) |

---

## 1. Purpose & Scope

This document records the **architectural approach** for the internal Operations platform. It is deliberately narrow: it covers Release 1 — **financial tracking** (projects, customer invoicing, customer POs, procurement, project expenses, office expenses) and the Operations Dashboard that sits on top of them.

Operational project management (tasks, IBP system categories, commissioning checklists, the "120 Balmain Road" style trackers) is **explicitly deferred**. It is accommodated in the data model but not built in Release 1.

The platform serves **three legal entities** — CommSecurity Smart Buildings Pty Ltd, CommSecurity Pty Ltd and RAVEN BOX (§5.2). Release 1 is built and operated for Smart Buildings, but the schema is multi-entity from the first migration (ADR-07).

**Out of scope for Release 1:** sales pipeline / opportunity forecasting (orders-in-hand only), payroll processing, inventory, timesheet capture, and inter-entity recharge accounting.

---

## 2. Where We Are Today

### 2.1 Systems inventory

| System | Role today | Role in target state |
|---|---|---|
| **Google Sheets** (3 workbooks) | System of record for projects, forecast invoicing, procurement, project & office expenses, dashboard | Retired as system of record; optional read-only export during transition |
| **Xero** | Accounting system of record — AR invoices, AP bills, spend money, contacts | Remains system of record for **actuals**. Read via API (access pending) |
| **iTrade** | Job Number issuance, Supplier POs, timesheets | Job Numbers and Supplier POs migrate to this platform. Timesheets remain in iTrade for now |

### 2.2 The problem, stated plainly

The three workbooks form a chain: **Office Expenses** + **Operations** → **Financial Summary (Operations Dashboard)**. That chain is currently broken in observable ways:

- The Office Expenses summary block is almost entirely `#REF!` — the FY25/26 financial summary, project totals and invoiceable totals all fail.
- The Financial Summary's second monthly block shows `#N/A` for every Office Expenses month, so `Total Expenses` on that block silently equals Project Expenses only, and Net Profit is overstated by roughly $150k/month.
- Total Project Expenses is reported as **$1,673,985** in one place and **$1,683,036** in another on the same workbook.
- `Job Code` is not a reliable key: `TBA`, `Various`, `na`, `JN 5108` (space, not hyphen), `P-3655` vs `JN-` prefixes, and `JN-676` / `JN-5416` each appearing against two unrelated projects.
- The month-end ritual — review Future Invoicing, move rows into Invoicing, freeze a copy as a new tab — is manual, lossy, and produces no audit trail.

None of this is a criticism of the spreadsheets; they have carried the business to ~$3.5M of FY27 planned invoicing across 49 projects. But they have reached the point where **the reconciliation cost exceeds the build cost.** That is the business case.

### 2.3 Scale (this matters for the technology choice)

| Dimension | Current | 5-year projection |
|---|---|---|
| Active projects | 49 | ~150 |
| Staff / users | 11 | ~25 |
| Claim lines (invoice + forecast) / yr | ~1,200 | ~4,000 |
| Procurement lines / yr | ~1,500 | ~5,000 |
| Office expense lines / month | ~60 | ~120 |
| **Total rows, 5 yrs, all tables** | — | **< 250,000** |
| **Database size on disk** | — | **< 300 MB with indexes** |

This is a *small data* problem with a *high correctness* requirement. The entire dataset fits comfortably in RAM. Every architectural decision below follows from that.

---

## 3. Principles

1. **Correctness over throughput.** The dataset is tiny. Optimise for a single unambiguous answer to "what did we invoice in March," not for scale we will never reach.
2. **One system of record per fact.** Every field has exactly one owner. Where two systems hold the same fact, we *reconcile and show variance* — we do not silently merge.
3. **Store long, pivot at read.** The monthly matrices in the sheets are presentation, not storage. Facts are stored one row per (entity, period).
4. **Budgets are requirements.** Cold start, memory footprint and dashboard latency have hard numbers (§10) enforced in CI, not aspirations.
5. **Every dependency pays rent.** A library, service or container must justify itself against what it costs to run, patch and understand. Default answer is no.
6. **Standalone first, integrated second.** The platform must be fully useful with zero external integrations. Xero and iTrade are adapters at the edge, never load-bearing.
7. **Boring, inspectable technology.** An internal tool for 11 people that must run for a decade on our own hardware. Novelty is a liability.

---

## 4. Target Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Browser (internal network / VPN)                            │
│  Server-rendered HTML + progressive enhancement              │
│  No SPA, no client-side router, no build-time framework       │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTPS
┌───────────────────────────▼──────────────────────────────────┐
│  Application (single binary/process)                          │
│                                                               │
│  ┌───────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐ │
│  │ HTTP /    │  │ Domain    │  │ Reporting │  │ Auth      │ │
│  │ templates │  │ services  │  │ / rollups │  │ (OIDC)    │ │
│  └───────────┘  └───────────┘  └───────────┘  └───────────┘ │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │ Integration adapters (isolated, optional, replaceable)  │ │
│  │  xero/   itrade/   sheets/   csv/                       │ │
│  └─────────────────────────────────────────────────────────┘ │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│  PostgreSQL 16 — single instance                              │
│  Facts + dimensions + SQL views for all rollups               │
└──────────────────────────────────────────────────────────────┘
```

Three moving parts: a browser, one application process, one database. That is the whole system.

**Why server-rendered.** The dashboard is the heaviest page and it is a set of tables and monthly rollups. Rendering that on the server is a single query plus a template — a few milliseconds, no JSON serialisation, no hydration, no client state to keep consistent with the server. Interactive editing (the invoicing grid) gets progressive enhancement via small, targeted fragment swaps. There is no scenario at this data scale where shipping a SPA and a separate API earns back its cost.

---

## 5. Data Model

### 5.1 Grain and the central insight

**"Invoicing" and "Future Invoicing" are the same table.** In the workbook they are separate tabs, with a manual monthly migration between them. In the platform they are one fact table — `claim_line` — differentiated by a `status` column.

```
forecast → planned → approved → invoiced → paid
                                     ↘ void
```

The month-end ritual becomes a status transition with a timestamp and a user. The "frozen monthly tab" becomes a query against the status history. The reconciliation step against Xero becomes a variance report, not a re-keying exercise.

This single change removes the largest source of manual effort and the largest source of error in the current process.

### 5.2 Multi-entity model

The platform serves **three legal entities**, not one:

| Entity | ABN | Role |
|---|---|---|
| **CommSecurity Smart Buildings Pty Ltd** | 19 677 520 339 (ACN 677 520 339) | Primary entity. Owns the current project portfolio |
| **CommSecurity Pty Ltd** | 42 636 706 146 | Separate trading entity |
| **RAVEN BOX** | *TBC* | Product entity — see the caveat below |

Shared address (Level 1/250 Canterbury Road, Surrey Hills VIC 3127), shared phone, shared accounts inbox, and **shared people** — Justin Anders and Richard Roberts appear in both CommSecurity entities.

**This is not SaaS multi-tenancy, and treating it as such would be a mistake.** In multi-tenancy the goal is isolation: tenants must never see each other. Here the goal is the opposite — consolidated visibility across entities, *plus* the ability to produce clean per-ABN statutory figures. Entity is a **scoping dimension**, not a security boundary. It is enforced in the query layer, and a consolidated view is a legitimate, commonly used mode rather than an administrative escape hatch.

#### Why this lands in STP-1 and not later

Adding `entity_id` to the schema now costs a column and a foreign key. Retrofitting it after four phases means touching every table, every view and every query — *and* backfilling entity attribution onto historical rows where the correct answer may no longer be recoverable. Same asymmetry as ADR-02: take the reversible path.

**Decision: the schema is multi-entity from migration `001`. The interface is single-entity until there is a second entity to show.** Every fact table carries a non-null `entity_id`. STP-1 through STP-5 run with one entity seeded and the selector hidden. No UI cost, no retrofit cost.

#### What is scoped by entity

| Concern | Treatment |
|---|---|
| `project` | **Exactly one entity.** A project invoices from one ABN; this is not optional or shared |
| `client_po`, `claim_line`, `supplier_po`, `supplier_invoice` | Inherit the project's entity |
| Invoice numbers, supplier PO numbers | **Per-entity sequences.** These appear on documents bearing a specific ABN and must not collide or interleave across entities |
| Job numbers | **One global sequence.** Job codes are an internal lookup key joined across every system; entity-prefixing adds ambiguity for no benefit. `project.entity_id` carries the attribution |
| `client`, `supplier` | Shared master data, with a per-entity relationship record — the same client may trade with two entities under different terms |
| `office_expense_line` | Entity plus optional allocation — see below |
| User roles | Per-entity (`user_entity_role`), since someone may be operations in one entity and read-only in another |
| Xero connection | **One per entity.** Three ABNs means three Xero organisations, three OAuth tenant connections |

#### Shared resources — the genuinely hard part

Justin's and Richard's wages are paid by one entity, but their work serves projects in another. The existing data already contains a hint of this: the Operations workbook carries a `COMMSecurity Labour (Techs / Tayler)` line with `CommSecurity` itself as the client.

Proposed treatment, in increasing order of rigour:

1. **Attribution only** (STP-4): `office_expense_line.entity_id` records which entity bears the cost. Sufficient for per-entity office expense reporting.
2. **Allocation** (STP-4): an optional `office_expense_allocation` child table splitting a line across entities by percentage. Justin at 60/40 becomes data rather than a spreadsheet note.
3. **Inter-entity recharge** (deferred): where one entity actually invoices another for labour, that is a real transaction in both Xero organisations and must appear as revenue in one and cost in the other.

**Consolidation must eliminate inter-entity transactions.** If CommSecurity Pty Ltd invoices Smart Buildings, that amount is revenue on one side and cost on the other; summing all three entities without elimination double-counts. `claim_line` and `supplier_invoice` therefore need an `is_intercompany` flag and a counterparty entity reference, set at STP-2/STP-3 even though elimination logic is only exercised at STP-5.

> **This has tax and transfer-pricing implications that are outside my competence.** Recharge arrangements between related entities should be confirmed with your accountant before the allocation model is finalised — the architecture should record whatever they specify, not lead it.

#### RAVEN BOX is probably a different financial shape

The two CommSecurity entities are project businesses: contract value, progress claims, project expenses. RAVEN BOX looks like a **product** business — the workbooks show RAVEN gateways as test equipment and `Raven R&D` (JN-5108) as an R&D project. If it sells units or licences, its revenue is unit sales and possibly recurring licence income, not progress claims against a contract value.

`claim_line` models progress claims well and unit sales badly. **Recommendation: bring RAVEN BOX in as an entity from `001`, but do not force its revenue through the claim model until we know its shape.** It may need a separate revenue fact table, and possibly inventory — which §1 currently places out of scope. This is flagged in §13 rather than designed speculatively.



**Dimensions**

| Entity | Notes |
|---|---|
| `entity` | The three legal entities (§5.2). Legal name, ABN, ACN, registered address, phone, accounts email, Xero tenant id, active FY range. **Every fact table carries `entity_id NOT NULL`** |
| `period` | The FY spine. `(fy, month_no 1–12, month_start, eom_date)`. Australian FY: **month_no 1 = July**. Pre-populated FY24 → FY35. Every fact joins here. |
| `client` | Business name, ABN, addresses, invoice email, office/main/site contacts |
| `supplier` | As per client, plus the invoice-submission email |
| `project_type` | ICN, IBP, EMS, NSW, Consulting, Service, Security, Q-Access, R&D — reference table, not an enum, so types can be added without a deploy |
| `project_status` | Active, Live, DLP, SLA, Complete, Cancelled |
| `fx_rate` | `(currency, effective_date, rate_to_aud)` — see §5.5 |
| `payroll_rate` | `(jurisdiction, kind, rate, effective_from)` — WorkCover VIC 1.785%, NSW iCare 0.39%, Payroll Tax VIC 4.85%, NSW 5.45%, superannuation. **Rates are data, not formulas.** |

**Facts**

| Entity | Grain | Owner of truth |
|---|---|---|
| `project` | One per job | **This platform** |
| `client_po` | One per customer PO | This platform (entered on receipt) |
| `claim_line` | One per claim/invoice line, per period | **Split** — forecast fields ours, actuals from Xero |
| `supplier_po` / `supplier_po_line` | One per PO / per line item | **This platform** (migrating from iTrade) |
| `supplier_invoice` | One per AP bill | **Xero** |
| `project_expense_estimate` | One per (project, period) | This platform — manual forward estimate |
| `office_expense_line` | One per (category, subject, period) | This platform |

### 5.4 Key relationships

```
entity ──< project
   │           │
   │      client ──< project ──< client_po
   │                       │
   │                       ├──< claim_line >── period
   │                       ├──< supplier_po ──< supplier_po_line >── period
   │                       ├──< supplier_invoice
   │                       └──< project_expense_estimate >── period
   │
   ├──< office_expense_line >── period
   │        └──< office_expense_allocation >── entity
   │
   ├──< user_entity_role >── user
   └──< number_sequence          (invoice / supplier PO, per entity)

document >── (entity_type, entity_id)   soft polymorphic, see §5.6
```

Every fact hangs off `entity` as well as its natural parent. `project.entity_id` is the attribution point — `claim_line`, `supplier_po` and `supplier_invoice` inherit it rather than storing an independent copy that could drift.

`project.job_code` is the universal join key across every system — Xero, iTrade, supplier correspondence, and internally. §6 covers why that key needs an owner.

### 5.5 Decisions the model must make explicit

These are currently implicit in the spreadsheets and are a recurring source of disagreement. They get columns and constraints.

- **GST.** All stored amounts are **ex-GST**, in a column named `amount_ex_gst`. Tax is derived at presentation. No column is ever ambiguously named `amount`.
- **Currency.** Procurement has USD lines with an FX rate stored loose in the sheet header (currently 0.70732). Each `supplier_po_line` stores `currency`, `unit_cost`, `fx_rate_used` and the derived `unit_cost_aud`. The rate is **captured at PO issue and frozen** — restating history when the dollar moves is not acceptable for cost tracking.
- **Derived vs entered office expenses.** Payroll tax, WorkCover and superannuation are *computed from wages*, not typed in. `office_expense_line.is_derived` plus a rule reference makes this visible, so a wage change automatically flows to seven downstream lines instead of requiring seven edits.
- **Contract value vs invoiceable value.** These currently diverge (see `PDNSW - SOC`: $518,400 PO against $259,200 FY27). Contract value lives on the project; the FY split is derived from claim lines. One number, one place.
- **Multiple POs per project.** The model supports it; the sheets assume one.

### 5.6 Documents & attachments

The prior Supabase evaluation correctly identified a requirement this document had omitted: drawings, quotes, contracts, commissioning documents, supplier quotes, receipt photos and PO paperwork all need to attach to projects, POs and claim lines.

**Files live on the filesystem, metadata lives in Postgres.** Never blobs in the database — it destroys backup and restore times for no benefit.

```
document ( id, entity_type, entity_id, filename, content_type,
           size_bytes, sha256, storage_path, uploaded_by, uploaded_at )
```

- `(entity_type, entity_id)` is a soft polymorphic reference — a document attaches to a project, a `supplier_po`, a `claim_line` or a `client_po`. Indexed on that pair.
- `storage_path` is content-addressed by `sha256` (`/data/documents/ab/cd/abcd…`), which deduplicates the same quote attached to three POs and makes the store immutable.
- Volume mounted at `/data/documents`, on its own disk, **backed up separately from the database** on a different schedule — the database is small and dumps in seconds, the document store is large and mostly cold.
- Authorisation is checked in the application before the file is served. Files are never served directly by the reverse proxy.

This is roughly 150 lines of Go and a mounted volume. It is the entirety of what Supabase Storage would have been used for here (ADR-02); S3 compatibility, image transforms and object-level RLS are not requirements for internal document attachment.

Restore ordering matters: the database references documents by hash, so **restore the document volume before the database** to avoid a window of dangling references.

### 5.7 Reporting

All rollups are **plain SQL views** to begin with — `v_project_financials`, `v_monthly_pl`, `v_dashboard`, `v_by_type`, `v_by_client`. At 250k rows on a warm cache these run in single-digit milliseconds. Materialised views are a measured optimisation, not a starting position; we add one only when a p95 measurement says so.

---

## 6. Job Number Authority

Job Numbers are currently issued by iTrade. Everything joins on them — Xero projects, supplier POs, invoices, correspondence, and every tab in every workbook. They are also, today, the weakest link in the data (§2.2).

**Recommendation: this platform becomes the issuing authority for Job Numbers.**

- Job Numbers are allocated by the platform against a created project. Format enforced (`JN-nnnn`), uniqueness guaranteed by a database constraint, no `TBA` states persisting past project creation.
- A project cannot be created without a client, type and lead. This eliminates the orphan rows.
- Historical codes (`P-3655`, `JN 5108`) are captured in an `alias` table so old references and existing Xero data still resolve.
- iTrade continues to be used for timesheets and existing works; new Job Numbers stop originating there.

This is a small feature with outsized leverage — it is the difference between integrations that reconcile automatically and integrations that need a human to match rows.

Supplier PO issuance follows the same logic and should move at the same time: sequential `PO-nnnn` numbers, per-project, generated here.

---

## 7. Integration Strategy

### 7.1 Posture: reconcile, don't sync

Two-way sync between an internal tool and an accounting ledger produces conflicts that nobody can adjudicate at month end. Instead:

- **Xero owns actuals** — issued invoices, payments received, AP bills, spend money.
- **This platform owns forward-looking and operational data** — project structure, job numbers, forecast claim schedule, customer POs, supplier POs, expense estimates.
- Where both hold a fact, we **pull from Xero, match on `job_code` + `invoice_number`, and surface the variance.** Unmatched rows on either side are a work queue, not an error.

Writing back to Xero is deliberately deferred. The one candidate worth revisiting later is pushing the planned invoice schedule as Xero draft invoices — but only once matching is proven reliable in the read direction.

### 7.2 Adapter design

Every integration is a self-contained module behind a narrow interface, with three properties:

1. **The application runs fully without it.** Disabling Xero degrades the platform to manual actuals entry; nothing breaks.
2. **It imports to a staging table first.** Raw payload retained, then transformed. When a mapping is wrong we re-transform rather than re-fetch.
3. **It is idempotent.** Re-running an import produces the same result.

| Adapter | Introduced | Mechanism |
|---|---|---|
| `csv/` | STP-1 | Generic CSV import — the fallback for everything, and the migration path from the workbooks |
| `sheets/` | STP-1 | One-time migration in; optional read-only export back during transition so the dashboard audience isn't stranded |
| `itrade/` | STP-3 | CSV export ingest. iTrade exposes no documented API — do not architect around one appearing |
| `xero/` | STP-6 | OAuth 2.0, restricted API user, endpoints per CS-OP-SOW-001 §Summary API List |

### 7.3 Xero specifics (deferred, but design for it now)

Per the SOW, the restricted-user + scoped-app approach is correct and should be kept: `projects`, `accounting.transactions`, `accounting.contacts`, no payroll scope. Two operational notes that affect the design:

- Access tokens expire in 30 minutes; refresh tokens must be persisted and rotated. This needs a small scheduled job and encrypted-at-rest token storage — not an afterthought.
- If the refresh token is ever revoked or lapses, re-authorisation requires an interactive login. The runbook needs to name who can do that.

Xero's `projectNumber` maps to `project.job_code`; the customer PO arrives as `Invoice.Reference`. Both mappings should be asserted in tests against real data as soon as access is granted.

**One Xero organisation per entity.** Three ABNs means three Xero orgs, each with its own tenant connection, its own token pair and its own refresh lifecycle. The adapter is therefore written as *per-tenant from the outset* — a single-tenant implementation retrofitted to three is exactly the kind of rework ADR-07 exists to avoid. `entity.xero_tenant_id` is the join, and the token store is keyed by entity. A failure or revocation on one entity must not stall the others.

---

## 8. Technology Decisions

Recorded ADR-style. ADR-01 through ADR-03a were confirmed 19 Aug 2026; the remainder stand as proposed.

### ADR-01 — PostgreSQL as the sole data store
**Status:** Accepted · **Decision:** PostgreSQL 16, single instance.

Relational, transactional, with the reporting SQL this problem is made of. One service to run, patch and back up. SQLite was considered and is genuinely viable at this scale, but concurrent editing of the invoicing grid by several people at month end is the exact workload where Postgres' MVCC earns its keep.

### ADR-02 — Plain PostgreSQL, not the Supabase stack
**Status:** Accepted · Revisited 19 Aug 2026 against the prior self-hosting evaluation.

**Context.** A prior evaluation recommended self-hosted Supabase, primarily on grounds of existing familiarity, breadth of included infrastructure, and skills carry-over to the RAVEN / smart-building platform. That evaluation also recommended the layering `Frontend → Application/API → Supabase → PostgreSQL` — explicitly *not* letting the browser drive Supabase directly.

**Those two recommendations pull against each other.** With an application tier in front, PostgREST is bypassed (we write SQL), RLS-as-authorisation is bypassed (the app authorises), and Realtime is bypassed (the app renders). Three of the five reasons to run the stack are cancelled by the recommended topology. Edge Functions compound it: Deno runtime, so business logic would straddle two languages.

**The asymmetry decides it.** Supabase *is* Postgres. Starting plain and adopting Supabase later costs nothing — identical schema, migrations and SQL, and the stack points at the existing database. Starting with Supabase and reducing later means unwinding RLS policies and GoTrue's user tables out from under live financial data. One direction is free, the other is not. Where options are close, take the reversible one.

**On resources.** The prior evaluation lands at 16–32 GB RAM for a comfortable stack; §10 of this document budgets under 1 GB for the entire system. Same 49 projects, same 11 users. That gap is the §3.5 principle in numbers.

**RAVEN is a different question and should be decided separately.** For a stack deployed into customer networks — multi-tenant, realtime dashboards, offline operation, BACnet/Modbus/MQTT ingest, file-heavy — RLS, Realtime and Storage genuinely earn their place, and Supabase may well be correct there. It does not follow for this platform, because the two do not share a deployment: RAVEN runs at customer sites, this runs on the internal VM. There is no shared instance and no shared operational burden. The only genuine carry-over is skills — and SQL, schema design, migrations, Docker and RLS concepts all transfer from plain Postgres. What does not transfer is `supabase-js`, which ADR-03 means we would not use.

**The path not taken.** The coherent Supabase-maximal alternative is to drop the application tier entirely: a JS frontend against Supabase directly, RLS as the authorisation model, Studio as admin. That is what Supabase is designed for and is a fast route to working software. It is rejected here because it puts financial authorisation rules into RLS policies rather than into tested application code (see ADR-03a on pinning rollups to known-good numbers), and because it contradicts the layering the prior evaluation itself recommended.

Self-hosted Supabase is Postgres plus GoTrue, PostgREST, Realtime, Storage, imgproxy, Kong and Studio — realistically 8–10 containers and a few GB of RAM at idle, all of which we own the patching and upgrade path for. What it buys is auto-generated REST, row-level-security-driven multi-tenancy, realtime subscriptions and a hosted-style admin UI.

For 11 internal users behind the corporate network, none of those are problems we have. Multi-tenancy is a single tenant. Realtime is a page refresh. The auto-generated API is a liability rather than an asset when the reporting logic wants hand-written SQL. And the self-hosted distribution lacks the managed backup/PITR story that makes the hosted product attractive in the first place — the piece we'd most want is the piece we'd still have to build.

**The familiarity argument is real and is preserved.** What is genuinely valuable in the current Supabase workflow is the *shape* of it: declare services locally, pull, run, deploy the same definition to the internal server. That is Docker Compose, and it is unchanged here — the only difference is that the Compose file has two services instead of ten. The parts worth keeping individually:

| What Supabase gave you | Replacement |
|---|---|
| Postgres | `postgres:16` — the same database, same client tools, same SQL |
| Studio (table browser) | One optional container (`pgweb` or pgAdmin), dev-only, not deployed to production |
| GoTrue (auth) | Google Workspace OIDC (ADR-04) — fewer moving parts and no password storage |
| PostgREST (auto API) | Hand-written SQL in the reporting layer, which this problem wants anyway |
| Realtime, Storage, Kong, imgproxy | Not required |

**Decision: `postgres:16` in Compose, everything else dropped.** Nothing about the local-pull / deploy-to-internal workflow changes. If a genuine need for one of the discarded components appears later, each can be added back independently — that is the advantage of not taking the bundle.

### ADR-03 — Go, single binary, server-rendered
**Status:** Accepted · **Decision:** Go + `templ` + a small amount of htmx, compiled to one static binary, shipped in a `scratch`-based container.

A single artifact with no runtime to install, no `node_modules`, no interpreter version to manage. Startup in the tens of milliseconds, resident memory in the tens of megabytes.

Go pairs unusually well with the Docker target: a multi-stage build produces a `FROM scratch` image of **15–25 MB total**, versus 150–400 MB for a Node or Python base image. The container is the binary plus a CA bundle. Nothing to patch inside it, and a CVE scan surface of approximately zero.

**Where TypeScript and Python still earn a place** — at the edges, never in the runtime path:

- **Python** for the one-time Sheets → Postgres migration. `pandas` plus the Sheets API is the right tool for a job that runs a handful of times and then is archived. It does not ship to the server.
- **TypeScript** only if a specific screen outgrows htmx — realistically only the invoicing grid, if inline multi-row editing gets ambitious. Scoped to that one component, bundled as a static asset, still served by the Go binary. Not a second deployable.

The default stays: if a page can be a template and a query, it is a template and a query.

**Ruled out:** SPA + separate API (two deployables, two auth paths, JSON round-trips for data that is already HTML-shaped); Electron or embedded-browser packaging; any ORM that hides the SQL from the reporting layer.

### ADR-03a — The codebase is optimised for AI-assisted development
**Status:** Accepted.

This is being built primarily by you working with Claude, with a dev team available for larger pieces. That is an architectural input, not just a staffing note, and it pushes in the same direction as the principles in §3 rather than against them:

- **Small files, explicit boundaries.** A package should fit in a context window. Cross-cutting cleverness that requires holding six files in your head is expensive for a human reviewer and worse for a model.
- **Plain SQL in `.sql` files, not query builders.** SQL is unambiguous, reviewable, and testable in isolation. Generated query-builder chains are where subtle financial bugs hide.
- **Tests are the review mechanism.** AI-written code that passes a test asserting *March invoicing equals $254,610.55 against the migrated dataset* is verified. Code that merely looks right is not. Every financial rollup gets a test pinned to known-good numbers from the existing workbooks.
- **No generated abstraction layers.** Models default to median patterns — repository wrappers around repository wrappers, DTOs mapping to near-identical DTOs. Each layer must be justified out loud before it is written.

Practically: a `CLAUDE.md` at the repo root stating the stack, the budgets from §10, the naming conventions (`amount_ex_gst`, never `amount`) and the explicit ruled-out list, so those constraints are enforced on every session rather than re-litigated.

### ADR-07 — Multi-entity schema from day one, single-entity interface
**Status:** Accepted.

Three legal entities (§5.2) with shared people and shared overheads. The schema carries `entity_id NOT NULL` on every fact table from migration `001`; the interface hides the selector until a second entity is loaded.

**Why not defer the schema too.** The cost of the column now is a foreign key. The cost later is every table, every view, every query, plus backfilling entity attribution onto historical rows — and for shared overheads like Justin's and Richard's wages, the correct historical split may not be recoverable from the workbooks at all. Deferring the *interface* is free; deferring the *schema* is not. Same reversibility logic as ADR-02.

**Why not separate databases or separate deployments per entity.** Both are superficially tidier and both defeat the purpose: consolidated reporting becomes a cross-database join or an export-and-merge, shared master data is duplicated three ways, and one deployment becomes three to patch and back up. Entity is a scoping dimension, not an isolation boundary (§5.2).

**What this does not decide.** The allocation model for shared overheads and the treatment of inter-entity recharge are accounting questions, deferred to STP-4 and gated on advice (§13).

### ADR-04 — Authentication via Google Workspace SSO
**Status:** Proposed · **Decision:** OIDC against Google Workspace. Roles held locally.

The business already runs Google Workspace. SSO means no password storage, no reset flow, no separate offboarding step — revoking the Workspace account revokes platform access. All three entities share the `commsecurity.com.au` domain, so one OIDC client covers everyone.

Authorisation is a local `user_entity_role` table — **per entity, not global** (ADR-07). Someone may be operations in Smart Buildings and read-only in CommSecurity Pty Ltd, and Workspace groups will not model that or project leadership. A `consolidated` role grants the cross-entity view.

### ADR-05 — Schema migrations are versioned SQL files
**Status:** Proposed.

Plain, numbered, forward-only `.sql` files applied by a tiny runner at startup. No migration DSL, no ORM-generated diffs. The schema is readable by anyone who knows SQL, which for a decade-lived internal financial system matters more than convenience.

### ADR-06 — Backups
**Status:** Proposed.

Nightly `pg_dump` to a location on a different machine, plus a **monthly documented restore test.** An untested backup is not a backup. This is not optional for a system holding the financial forecast.

The document volume (§5.6) backs up on its own schedule — weekly full plus incremental, since the store is append-mostly and content-addressed. Restore order is documents first, then database.

Self-hosting means we are the cloud provider: backups, disaster recovery, OS and Docker patching, TLS renewal, monitoring and uptime are ours. That is an accepted cost of avoiding cloud compute charges, but it is a real one and belongs in a named runbook with a named owner — not assumed.

---

## 9. Deployment

Target: **Linux VM, Docker containers.** The same Compose definition runs locally and on the internal server, differing only by env file.

### 9.1 Topology

```
docker compose  ─┬─  app          Go binary, FROM scratch      ~20 MB image
                 ├─  postgres     postgres:16                  ~150 MB image
                 ├─  caddy        TLS, HTTP/3, compression      ~50 MB image
                 └─  pgweb        table browser — DEV ONLY, never in prod

volumes ─┬─  pgdata        database
         ├─  documents     /data/documents, separate disk, separate backup
         └─  caddy_data    certificates + ACME state (must persist)
```

Three services in production: `app`, `postgres`, `caddy`. No orchestrator, no service mesh, no sidecars — those answer scaling problems we do not have.

### 9.2 Caddy in production

Caddy runs in **both** environments, not just development. Under §3.5 ("every dependency pays rent") it earns its place by removing work from the Go binary rather than adding a layer on top of it:

| What Caddy does | What it saves |
|---|---|
| **Automatic TLS + renewal** | Certificate lifecycle management, and the annual outage when someone forgets a renewal |
| **HTTP/2 and HTTP/3** | Zero configuration; meaningful on the dashboard, which is one large HTML response |
| **zstd / gzip compression** | The dashboard response compresses roughly 8:1. Not implementing this in the app |
| **Static asset serving with cache headers** | Immutable, fingerprinted CSS/JS served without touching the app process |
| **Security headers** (HSTS, CSP, `X-Frame-Options`, `X-Content-Type-Options`) | One config block instead of middleware |
| **Structured access logs** | JSON access logging for free |

Cost is roughly 50 MB of image and 20–30 MB resident — comfortably inside the §10 budget, and less than the code it displaces.

**Certificates on an internal-only host.** The server has internet access but is not publicly reachable, so ACME HTTP-01 will not validate. Two workable options, in order of preference:

1. **ACME DNS-01** against a real name (`ops.commsecurity.com.au`) whose A record points to the internal address. This yields a publicly trusted certificate with nothing exposed to the internet, renewed automatically. Requires a Caddy build with the DNS provider module for whoever hosts CommSecurity DNS. **Recommended** — no CA distribution, no browser warnings, no expiry surprises.
2. **`tls internal`** — Caddy issues from its own local CA. Zero external dependency, but the Caddy root certificate must then be distributed to every staff machine and browser. Workable via Workspace device management; more moving parts on the client side.

Either way, `caddy_data` **must be a persistent volume.** If it is ephemeral, every container restart re-issues certificates and will hit ACME rate limits.

The Caddyfile is roughly fifteen lines and belongs in the repository. Staging and production differ only by hostname.

### 9.3 Local development

`docker compose up` gives Postgres, Caddy, pgweb and a seeded database. The Go app runs **outside** the container during development, directly from source with hot reload, pointed at the containerised Postgres. This keeps the edit-compile-test loop at a second or two rather than a container rebuild. `make dev` should take a new machine from clone to running in under ten minutes.

Development uses `tls internal` against `ops.localhost`, so the local environment exercises the same TLS path as production rather than running plain HTTP and discovering protocol differences at deploy time.

### 9.4 Environments

Production plus a staging stack restored nightly from the production dump. Staging is where migrations and Xero mapping changes are proven before they touch real numbers. Identical Compose file; different `.env`.

### 9.5 Deploy

Build image → push to internal registry (or `docker save` / `scp` / `load` if no registry exists yet) → `docker compose up -d app` → migrations run at startup. Only the `app` service restarts; Caddy and Postgres stay up, so connections drain rather than drop. Downtime is a container restart, measured in seconds.

Rollback is repointing the tag at the previous image. If a migration was destructive, rollback additionally requires the pre-migration dump — which is why **every deploy takes a dump first**, automatically, as part of the deploy script rather than as a remembered step.

### 9.6 Secrets

Xero client secret, OIDC client secret and the database password come from an env file on the server with restricted permissions, injected at container start. They are never in the image and never in the repository. `.env.example` is committed with the keys and no values.

---

## 10. Performance & Resource Budgets

Hard numbers. Enforced by benchmarks in CI alongside the test suite; a build that regresses these fails.

| Budget | Limit | Rationale |
|---|---|---|
| Application cold start | **< 200 ms** | Restart during business hours must be unremarkable |
| Dashboard render, p95, server-side | **< 150 ms** | The heaviest page in the system |
| Any list/grid view, p95 | **< 100 ms** | |
| Application resident memory, steady state | **< 128 MB** | |
| Postgres `shared_buffers` | **256 MB** | Entire working set fits; disk should be idle |
| Total server memory footprint | **< 1 GB** | Runs on modest internal hardware alongside other services |
| Application container image | **< 30 MB** | `FROM scratch` + static binary; a Node or Python base would be 10× this |
| Total production stack, images on disk | **< 250 MB** | app + postgres + caddy |
| JS shipped to the browser, per page | **< 50 KB** uncompressed | Interaction budget, not a framework budget |
| Full database restore, verified | **< 5 min** | Recovery time objective |

**New-feature rule:** a change may not increase cold start by more than 20 ms, steady-state memory by more than 8 MB, or dashboard p95 by more than 15 ms without an explicit, recorded trade-off decision.

---

## 11. Delivery Phases

Each phase is stated as an **STP — Situation → Target → Proposal**, with explicit exit criteria. The Situation is drawn from the current workbooks and is deliberately specific: a phase that cannot describe what is wrong today cannot demonstrate it has been fixed.

Phases ship to the internal server and are used in anger before the next begins. On completion of each phase, the corresponding workbook tab is made **read-only** — this is the control against a shadow system (§12) and is not optional.

---

### STP-0 — Foundation

**Situation.** The architecture exists on paper. There is no repository, no environment and nowhere to deploy. Every subsequent phase is blocked on infrastructure that does not yet exist, and there is no mechanism enforcing the budgets in §10.

**Target.** A running, authenticated, empty application on the internal VM, reachable over TLS, with a repeatable deploy path and a backup that has been proven by restoring it.

**Proposal.**
- Repository, `CLAUDE.md` carrying the stack, budgets and ruled-out list (ADR-03a)
- Compose stack: `app`, `postgres`, `caddy`, plus `pgweb` in dev (§9.1)
- Caddy with DNS-01 certificates and a persistent `caddy_data` volume (§9.2)
- Migration runner — numbered forward-only SQL applied at startup (ADR-05)
- Google Workspace OIDC and the local role table (ADR-04)
- CI asserting the §10 budgets alongside the test suite
- Deploy script that dumps before migrating; nightly backup job

**Exit criteria.** A staff member signs in with their Workspace account at the internal URL over TLS and sees an empty project list. One deploy and one **documented restore** have been performed end to end.

---

### STP-1 — Project register & Job Number authority

**Situation.** 49 active projects live in the Project List tab, carrying $7,299,574 of contract value with the FY26/FY27 split maintained by hand. Job codes are unreliable — `TBA`, `Various`, `na`, `JN 5108` (space instead of hyphen), `P-3655` against the `JN-` convention, and both `JN-676` and `JN-5416` appearing against two unrelated projects each. Job numbers are issued by iTrade. Because every other system joins on this key, every downstream integration inherits the ambiguity. The workbooks are also entity-blind: they do not record which of the three legal entities owns a project, and at least one line (`COMMSecurity Labour`) is already an internal cross-entity charge.

**Target.** This platform is authoritative for projects and job numbers. Every active project carries exactly one valid, unique job code **and one owning entity**. A project cannot exist without an entity, client, type and lead.

**Proposal.**
- Migration `001`: `entity`, `period` (seeded FY24–FY35, month 1 = July), `client`, `project_type`, `project_status`, `project`, `job_code_alias`, `user_entity_role`, `number_sequence`
- All three entities seeded from company records (§5.2); RAVEN BOX pending its ABN
- `entity_id NOT NULL` on every fact table from the outset; entity selector hidden in the UI (ADR-07)
- Uniqueness and format of job codes enforced by database constraint, not application code
- Python importer from the Project List tab, emitting **a worklist of every ambiguous code** and **every project whose owning entity is unclear**
- Project CRUD and the job number allocator (global sequence)

**Exit criteria.** All 49 projects migrated with zero unresolved job codes and every project attributed to an entity. The next new job number is issued by the platform rather than iTrade. Project List tab read-only.

---

### STP-2 — Customer invoicing

**Situation.** Invoicing and Future Invoicing are separate tabs. Each month, rows are reviewed and manually copied forward, then a frozen snapshot tab is created and issued to Accounts. FY27 planned invoicing is $3,527,733 across roughly 1,200 claim lines. Customer POs exist as a text column rather than as entities, so a project with several POs cannot be represented. No status change leaves an audit trail, and the reconciliation back against issued invoices is retrospective and manual.

**Target.** One fact table with a status lifecycle. Month-end is a status transition with a user and timestamp, not a copy-paste. The frozen monthly tab becomes a query against status history. Multiple POs per project are first-class.

**Proposal.**
- Migration `002`: `client_po`, `claim_line`, `claim_line_status_history`
- Status lifecycle `forecast → planned → approved → invoiced → paid`, plus `void` (§5.1)
- Invoicing grid with inline editing — the one screen that may justify TypeScript (ADR-03)
- Month-end review flow and per-period snapshot view
- **Rollup tests pinned to known-good numbers:** FY27 total $3,527,733; March FY27 $254,610.55; December FY27 $576,305.20

**Exit criteria.** A complete month-end run performed in the platform with no spreadsheet involvement, and the output accepted by Accounts. Both tabs read-only.

---

### STP-3 — Procurement & project expenses

**Situation.** The Procurement Register tracks supplier POs, but the POs themselves are issued from iTrade. The AUD/USD rate sits loose in a sheet header (currently 0.70732) and is applied globally, so historical USD costs silently restate whenever the rate is updated. Project Expenses is a project × month matrix maintained by hand, mixing procurement roll-up with forward estimates in the same cells. Supplier quotes and PO paperwork live in email.

**Target.** The platform issues supplier PO numbers. FX is frozen at PO issue, so history does not move. Project expenses are stored long and pivoted at read, with roll-up and estimate clearly separated.

**Proposal.**
- Migration `003`: `supplier`, `supplier_po`, `supplier_po_line`, `supplier_invoice`, `fx_rate`, `project_expense_estimate`
- Supplier PO issuance and sequential numbering, per project
- `fx_rate_used` captured per line at issue (§5.5)
- Document attachment (§5.6) for quotes, PO PDFs and delivery paperwork
- Estimate entry by project × period, distinguishable from actual roll-up

**Exit criteria.** A supplier PO raised end-to-end in the platform with quote attached and sent to a real supplier. Procurement Register and PE tabs read-only.

---

### STP-4 — Office expenses

**Situation.** The Office Expenses workbook drives roughly $148,202 per month and $1,778,428 for FY27. Its summary block is almost entirely `#REF!` — total invoiceable, total project costs and both financial summaries all fail. Payroll on-costs (WorkCover VIC 1.785%, NSW iCare 0.39%, Payroll Tax VIC 4.85%, NSW 5.45%, superannuation) are spreadsheet formulas, so a single wage change requires around seven manual edits across categories. Corporate tax appears as 25% in one place and 30% in another.

**Target.** Category / subject / period model with on-costs derived from a dated rate table. One wage change propagates automatically. Tax rate is a single dated parameter with one value per FY.

**Proposal.**
- Migration `004`: `office_expense_line`, `payroll_rate`, `tax_rate`
- Derivation engine driven by `payroll_rate`, with `is_derived` making computed lines visible (§5.5)
- Importer for both the FY26/27 and FY27/28 grids
- Rates carry `effective_from`, so a mid-year change does not restate prior months

**Exit criteria.** Monthly office expense totals reconcile to the workbook within rounding for at least three months. The `#REF!` chain no longer exists. Workbook read-only.

**Sequencing note.** Office expenses are independent of project data, so STP-4 can run in parallel with STP-2 and STP-3 if the dev team is working alongside.

---

### STP-5 — Operations Dashboard

**Situation.** The Financial Summary depends on both upstream workbooks and inherits their faults. Its second monthly block shows `#N/A` for every Office Expenses month, so Total Expenses silently equals Project Expenses alone and Net Profit is overstated by roughly $150k per month. Total Project Expenses reads $1,673,985 in one place and $1,683,036 in another on the same workbook. There is no way to trace a headline figure back to the rows that produced it.

**Target.** One dashboard over one source, where every figure is traceable to its underlying rows and no cell can be `#REF!` or `#N/A` by construction.

**Proposal.**
- Views: `v_project_financials`, `v_monthly_pl`, `v_dashboard`, `v_by_type`, `v_by_client`
- Operations Summary, monthly P&L, actual vs plan vs forecast, by type, by client, by project
- **Drill-through from every figure** to the rows behind it
- FY filter; dashboard p95 under 150 ms (§10)

**Exit criteria.** Dashboard figures match independently computed totals with no unexplained variance. Financial Summary workbook retired.

---

### STP-6 — Xero reconciliation *(gated on API access)*

**Situation.** No API access yet. Actuals are keyed by hand from Xero into the Invoicing tab, and invoice numbers are captured inconsistently — some as `Inv No. 7250`, some bare, some absent. Nothing detects a divergence between what the platform forecasts and what Xero actually issued.

**Target.** Actuals pulled automatically, matched on `job_code` + `invoice_number`, with unmatched rows on either side presented as a work queue rather than an error.

**Proposal.**
- OAuth 2.0 with the restricted API user and scoped app per CS-OP-SOW-001
- Staging tables retaining raw payload; transform separately (§7.2)
- Token refresh job with encrypted-at-rest storage (§7.3)
- Matcher and variance report; **read-only, no write-back**

**Exit criteria.** A month closed by reviewing a variance report rather than re-keying invoices.

---

### Later — Operational project management

Task trackers, IBP system categories, commissioning checklists — the "120 Balmain Road" style content. The data model reserves room and nothing in STP-0 through STP-6 forecloses it. Deliberately deferred: the financial picture is the one that is currently broken.

---

## 12. Risks

| Risk | Mitigation |
|---|---|
| Xero API access delayed indefinitely | Architecture is standalone-first; Xero is STP-6 and gates nothing before it |
| iTrade cannot export cleanly | Manual entry is an acceptable fallback at 49 projects; Job Number authority moves regardless |
| Historical data quality blocks migration | STP-1 forces resolution of ambiguous codes as a deliberate, scoped exercise rather than a surprise |
| Spreadsheets persist in parallel ("shadow system") | Each phase makes the corresponding tab read-only on completion. Ambiguity about which is authoritative is the failure mode |
| Single maintainer / bus factor | Boring stack, plain SQL, versioned migrations, documented runbook. This is a stated reason for ADR-03 and ADR-05 |
| Backup exists but does not restore | Monthly documented restore test (ADR-06) |
| Entity attribution not recoverable for historical rows | `entity_id` exists from migration `001`; STP-1 surfaces unclear attribution as a worklist while the people who know are still available (ADR-07) |
| RAVEN BOX forced into a project-shaped revenue model | Entity seeded early, revenue model deliberately undecided until its shape is known (§5.2) |
| Consolidated figures double-count inter-entity trade | `is_intercompany` flag captured at STP-2/STP-3, elimination applied at STP-5 (§5.2) |

---

## 13. Open Questions

*Resolved 19 Aug 2026: runtime (Go — ADR-03), host environment (Linux VM + Docker — §9), Supabase (familiarity only, no external constraint — ADR-02).*

1. **Corporate tax rate.** The Office Expenses workbook uses 25% in one place and 30% in another. Which applies, and does it vary by FY?
2. **Google Workspace SSO** — confirm the platform can be registered as an OIDC client in the CommSecurity tenant, and who administers that.
3. **Historical scope.** Does the platform start from FY27, or do we migrate FY26 actuals for year-on-year comparison?
4. **Job code resolution.** STP-1 forces a decision on every ambiguous code (`TBA`, `Various`, `na`, duplicate `JN-676` / `JN-5416`). Who is authoritative for those calls?
5. **Internal registry.** Does one exist on the Linux VM estate, or is `docker save` / `scp` the deploy path for now?
6. **RAVEN BOX details** — ABN, ACN, registered address, and whether it currently has its own Xero organisation.
7. **RAVEN BOX revenue model.** Unit sales, licence/recurring, R&D cost centre, or project work? This decides whether it needs a revenue fact table beyond `claim_line`, and whether inventory returns to scope (§5.2).
8. **Entity ownership of the existing portfolio.** Do all 49 active projects belong to Smart Buildings, or is FY26/FY27 history split across both CommSecurity entities?
9. **Shared overhead allocation.** How should Justin's and Richard's costs be split across entities — fixed percentage, per-project, or borne wholly by one? **Requires accountant input** before STP-4 (§5.2).
10. **Inter-entity trade.** Does one entity currently invoice another, and if so is it recorded in both Xero organisations?
11. **Numbering continuity.** Do the second and third entities need invoice and supplier PO sequences that continue existing series, or start fresh?

---

*End of document.*
