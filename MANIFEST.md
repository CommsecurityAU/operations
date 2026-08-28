# MANIFEST — where every file goes

**As at:** 28 August 2026 · **809 tests** · pyright --strict clean

The `repo/` folder mirrors `C:\Dev\operations` exactly. Copy it over the top
and the paths land correctly — no guessing which `main` is which.

Four filenames repeat across directories, which is where the confusion has
been coming from:

| Name | Correct path | What it is |
|---|---|---|
| `main.py` | `ops/main.py` | app entrypoint, routes, boot |
| `main.js` | `ops/static/main.js` | browser shell wiring |
| `test_main.py` | `tests/test_main.py` | tests for `ops/main.py` |
| `app.js` | `ops/static/app.js` | the `h` / `api` / `fmt` primitives |

---

## Full tree

```
C:\Dev\operations\
├── .dockerignore
├── .gitattributes                     * text=auto eol=lf, *.ps1 crlf
├── .gitignore                         (yours, unchanged)
├── Dockerfile                         base pinned by digest
├── Makefile                           Linux/CI dev loop
├── dev.ps1                            Windows dev loop
├── pyrightconfig.json                 strict, 4 named exclusions (ADR-26)
│
├── .github/workflows/
│   └── ci.yml                         suite, gates, types, image, smoke, size
│
├── ops/                               THE APPLICATION
│   ├── __init__.py                    empty, but required
│   ├── auth.py                        OIDC, sessions, roles
│   ├── backup.py                      snapshots, prune, scheduler
│   ├── config.py                      env → Config, holds secret:// refs
│   ├── db.py                          connections, migrations, health
│   ├── http_util.py                   hardening, router, headers, CSRF
│   ├── main.py                        boot order, routes, fingerprint
│   ├── secrets.py                     secret:// resolver + 0600 store
│   ├── money.py                       the ONE rounding function (ADR-15)
│   ├── migrations/
│   │   ├── 001_foundation.sql         STP-0 + STP-1 schema
│   │   ├── 002_job_number_range.sql   reserved block (ADR-29)
│   │   ├── 003_invoicing.sql          customer_po, claim_line
│   │   ├── 004_retention.sql          retention per PO, milestone dates
│   │   ├── 005_schedules.sql          recurring claims, renewals
│   │   ├── 006_po_revisions.sql       variation vs correction
│   │   ├── 007_contract_value.sql     contract on the project (ADR-34)
│   │   ├── 008_claim_plan.sql         items and allocations (ADR-37)
│   │   ├── 009_plannable.sql          plan what is left to claim (ADR-39)
│   │   ├── 010_allocation_claim.sql   an allocation owns its claim (ADR-38)
│   │   ├── 011_suppliers.sql          who we buy from
│   │   ├── 012_procurement.sql        quotes, orders, lines, invoices (ADR-40)
│   │   ├── 013_supplier_alias.sql     resolved names, never guessed (ADR-41)
│   │   ├── 014_register_state.sql     what the sheet said
│   │   └── 015_stated_state.sql       a state with no date behind it
│   ├── modules/                       §6 FEATURE MODULES
│   │   ├── __init__.py
│   │   ├── projects.py                register CRUD, validation, routes
│   │   ├── worklist.py                job-code resolution
│   │   ├── claims.py                  invoicing lifecycle, EOM axis
│   │   ├── schedules.py               recurring claims, renewals
│   │   ├── claimplan.py               items, allocations, generation
│   │   ├── access.py                  roles by entity
│   │   └── procurement.py             the buying register
│   └── static/                        THE BROWSER CODE
│       ├── index.html                 shell
│       ├── tokens.css                 the ONLY colour/type/spacing literals
│       ├── base.css                   layout, table, controls
│       ├── app.js                     h / api / fmt / mount
│       ├── datatable.js               sort, multi-select filter, paging
│       ├── projects.js                the register screen
│       └── main.js                    shell wiring, screen switch
│
├── tools/                             ONE-SHOT, never shipped in the image
│   ├── import_register.py             workbook → database
│   ├── restore.py                     verify + restore a snapshot
│   ├── offbox_sync.sh                 host cron, backups/ + documents/ only
│   └── dev_session.py                 local session cookie, dev only
│
└── tests/                             245 tests, ~4 s
    ├── fixtures/
    │   ├── project_register_fy27.csv  the validated 63-row register
    │   ├── invoicing_fy27.csv         issued invoices, Jul-26 + Aug-26
    │   ├── future_invoicing_fy27.csv  the forward plan
    │   └── invoicing_by_month_fy27.csv  the pivot, as a cross-check
    ├── secret_allowlist.txt           exact literals only, no wildcards
    ├── test_auth.py                   claim checks, sessions, roles
    ├── test_db.py                     connection split, runner, health
    ├── test_gates.py                  deps, secrets, pin, migrations
    ├── test_http_util.py              hardening over real sockets
    ├── test_import_register.py        pinned FY27 figures
    ├── test_js_guardrails.py          frontend rules
    ├── test_main.py                   boot, static, STP-0 exit criteria
    ├── test_projects.py               CRUD, validation, delete guard
    ├── test_worklist.py               resolution actions, cascade
    ├── test_money.py                  rounding mode, integer-only
    ├── test_drift_check.py            what it reports and what it does not
    ├── test_sync_register.py          opening-balance corrections
    ├── test_claims.py                 lifecycle, slippage, retention
    ├── test_invoicing.py              migration 003, the figure did not move
    ├── test_retention.py              withholding, caps, release
    ├── test_schedules.py              generation, idempotence, renewals
    ├── test_import_claims.py          two sources, pivot reconciliation
    ├── test_schedules_api.py          adoption, generation, renewals
    ├── test_customer_pos.py           add, edit, revise, move, delete
    ├── test_claim_plan.py             items, allocations, generation
    ├── test_backfill_task.py          matching claims to workbook rows
    ├── test_access.py                 roles, and the last admin
    ├── test_procurement.py            dates, states, FX at the extended total
    ├── test_procurement_api.py        editing everything over HTTP
    ├── test_import_suppliers.py       re-runnable, never destructive
    ├── test_restore.py                pre-flight and restore ordering
    └── test_secrets.py                store, CLI, no-fallback
```

**Fixtures are CSV exports from Sheets, never the Drive markdown
conversion** (ADR-31). The conversion drops rows and merges tabs.

Not in the repo and not backed up: `data/` — database, snapshots, documents,
secrets, TLS. `.gitignore` covers it.

---

## After copying

```powershell
cd C:\Dev\operations
py -W error::ResourceWarning -m unittest discover -s tests   # expect 809 OK
.\dev.ps1 -Stale                                             # running == disk?
```

If the count is below 809, a test file did not land. If `-Stale` says STALE,
restart the server — Python loads a module once, so a running process can be
several edits behind the working tree while every test passes.

---

## Everyday commands

```powershell
.\dev.ps1              # serve on 5173
.\dev.ps1 -Seed        # migrate + import the register
.\dev.ps1 -Session     # mint a dev cookie
.\dev.ps1 -Stale       # is the server current?
```

```
make test      make check      make image      make session
```
