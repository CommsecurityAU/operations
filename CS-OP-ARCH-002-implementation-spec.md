# CommSecurity Operations Platform — Locked Implementation Spec

- **Document ID:** CS-OP-ARCH-002 · **Status:** Locked (changes require an ADR in §16)
- **Purpose:** hand this file to Claude with "implement STP-n" — zero decisions left open
- **Carries over from CS-OP-ARCH-001:** data model (§5), delivery phases (§11), risks (§12), open questions (§13)
- **Date:** 20 August 2026

---

## 0. Shape

One Python process. One SQLite file. One documents directory. One Docker image
(~60 MB), one volume holding all state. Zero pip deps, zero npm, no frontend
build step. CI builds on every push to `main` → ghcr → deployed to the
internal VM by the company fleet manager as a signed, digest-pinned,
health-gated release with automatic local rollback.

```
Browser ──HTTPS──▶ ops container (python:3.12-alpine)
                    ├─ http.server.ThreadingHTTPServer + ssl (stdlib)
                    ├─ static/  vanilla HTML/CSS/JS, one JS file per module
                    ├─ JSON API (/api/*) + server-rendered report pages
                    ├─ db.py — sqlite3 (stdlib), 1 write conn + RO read conns
                    ├─ auth.py — OIDC vs Google Workspace + HMAC session tokens
                    ├─ secrets.py — secret:// reference resolver
                    └─ /data (the volume)
                         ops.db          SQLite, WAL
                         documents/      content-addressed by sha256
                         backups/        VACUUM INTO snapshots
                         secrets/        0600 store + auto-generated keys
                         tls/            cert + key (internal CA)

git push → Actions (test → gates → build → ghcr) → fleet manager release
         → device agent: verify signature · pull · stage · health-gate on
           /healthz · auto-rollback on failure (no network, no operator)
```

---

## 1. Stack — locked

| Layer | Choice | Ruled out — do not re-litigate |
|---|---|---|
| Language | Python 3.12, **stdlib only**, type hints | Go, Node |
| HTTP | `ThreadingHTTPServer` + `ssl.SSLContext`, in-process TLS | any proxy, gunicorn, Flask, FastAPI, aiohttp |
| DB | stdlib `sqlite3`, WAL, one write connection + thread-local read-only connections | Postgres, Supabase, any ORM |
| Frontend | static vanilla ES modules + `fetch`; no build | htmx, React/Vue/Svelte, bundlers, npm |
| Auth | hand-rolled OIDC (Google Workspace) + HMAC identity-only session tokens (§9) | Authelia, OIDC libraries, passwords, server-side session table |
| Secrets | `secret://` references + 0600 volume store (§10) | secret values in env files, git, images, or release manifests |
| Packaging | one image on `python:3.12-alpine@sha256:…` (digest-pinned), one `/data` volume | multi-service compose, scratch binaries, floating base tags |
| Deploy | fleet-manager signed release (§12) | ssh+scp, systemd, k8s |
| Server HTML | f-string render helpers | Jinja2, template engines |

- Runtime pip deps: **0**. A new dependency is an ADR, not an import.
- **`pyright --strict` is a hard CI gate, zero errors** (dev-only tool, not shipped). ADR-08 accepted "no compile-time types" as a cost on a system whose entire job is arithmetic on money; a strict gate is the cheapest available mitigation, and unenforced type hints decay into decoration within a quarter.

## 2. Repository layout

```
ops/
├── CLAUDE.md                  # §15, verbatim
├── Dockerfile                 # FROM python:3.12-alpine@sha256:… (pinned) · COPY ops · VOLUME /data · CMD python -m ops.main
├── Makefile                   # dev / test / seed / clean
├── .github/workflows/ci.yml   # §13 pipeline
├── ops/
│   ├── main.py                # boot order: config → secrets resolve → db+migrate → server. MODULES list lives here.
│   ├── config.py              # env-driven dataclass; values may be secret:// refs
│   ├── db.py                  # ALL writes; connection, pragmas, migration runner (§4)
│   ├── http_util.py           # routing, auth decorator, JSON/session helpers, security headers, access log
│   ├── auth.py                # OIDC flow + token mint/verify (§9)
│   ├── secrets.py             # resolver + `set`/`list` CLI (§10)
│   ├── render.py              # page(), table(), money() — server-rendered reports
│   ├── documents.py           # content-addressed file store
│   ├── backup.py              # snapshot / prune / integrity_check (§12)
│   ├── migrations/            # 001_….sql … forward-only
│   ├── static/                # index.html · tokens.css · base.css · app.js (h, api, fmt) · datatable.js
│   └── modules/               # §6 — projects/ invoicing/ procurement/ office_expenses/ dashboard/
├── tools/                     # one-shot Sheets→SQLite importers; never shipped
└── tests/                     # stdlib unittest, no docker, < 10 s
```

## 3. HTTP layer

- One server on `:8443`; TLS cert+key loaded from `/data/tls/` (internal CA, root pushed via Workspace device management). `OPS_TLS=off` → plain `:8080`, dev only.
- Cert expiry checked at boot and hourly; under 30 days logs a loud warning naming the expiry date. Renewal owner named in the runbook — an internal CA cert dying takes the whole app down and nothing else will notice.
- **`ThreadingHTTPServer` hardening.** The stdlib defaults are not production defaults; all four are set explicitly in `main.py` and are review-blocking if removed:
  - `daemon_threads = True`
  - read timeout on the handler socket — a half-open connection must not hold a thread forever
  - request body cap enforced *before* reading (1 MB JSON, 25 MB multipart); over-limit → 413 without buffering
  - concurrent-connection cap; beyond it 503, never unbounded thread creation
- Handlers are thin: parse → auth → call db/module → respond. Logic in a handler is a review reject.
- Security headers on every response: HSTS, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, CSP `default-src 'self'`.
- CSRF: every mutation is non-GET and the session cookie is `SameSite=Lax`, which already blocks cross-site form posts. Belt and braces — the auth decorator rejects any non-GET whose `Origin` / `Sec-Fetch-Site` isn't same-origin. No CSRF tokens, no per-form state.
- Access log: one JSON line per request via `logging`.
- Static files: `Cache-Control: no-cache` (tiny, internal; no fingerprinting until measured).
- `/healthz` → 200 only if: DB opens, `PRAGMA quick_check` passes, and **every migration this binary ships has been applied** — `applied ⊇ expected`, *not* equality. A newer schema is healthy; a missing migration is not. Equality would brick the deploy loop: a release that migrates and then fails the gate for any other reason gets rolled back onto a binary that now sees a schema ahead of it and declares *itself* unhealthy, forever. **The deploy health gate trusts this — it must be honest.**

## 4. Database

Rules (also the docstring of `db.py`):

- **Connections.** One *write* connection guarded by one `threading.Lock`: every mutation is a `Db` method whose body runs in `with self._tx() as c:` (lock held, commit on clean exit). Handlers never write SQL. Never hold the lock across anything slow. **Reads use thread-local read-only connections** (`file:{OPS_DATA}/ops.db?mode=ro`, `uri=True`) and take no lock — this is the whole point of WAL, and it is what makes the §14 read budgets reachable. A single shared connection would serialise every read behind every other read, so a 150 ms dashboard query would stall the entire process.
- Pragmas, set in `db.py` only, per connection. Write: `journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON`, `busy_timeout=5000`. Read: `foreign_keys=ON`, `busy_timeout=5000`, `query_only=ON`. Every table `STRICT`.
- Boot asserts `sqlite3.sqlite_version >= 3.37` (`STRICT` tables, `UPDATE … RETURNING`); CI asserts the same *inside the built image*, since the base image pin is what guarantees it.
- Money: integer cents, columns `*_cents`, never `amount`. Rates: basis points. **Rounding: half away from zero, per line, one function, one place — `ops/money.py`** (ADR-15). `money.divide()` is the only rounding primitive in the codebase; builtin `round()` is banker's and `int()` truncates, so either reached for by habit silently contradicts ADR-15. A test walks the module's AST and fails on any float literal, true division, or call to `float`/`round` — this is what Sheets does, so it is what the pinned workbook figures encode, and it is ordinary GST practice. **Verified 24 Aug 2026:** the three pinned FY27 totals are identical under half-up, banker's and truncation, because every source value in the register is already exact to the cent — nothing in STP-1 rounds at all. The question is answered, not deferred.

  The check found a live defect elsewhere: the importer's inline parser kept the first two decimals and **truncated** the rest, so `$1,234.565` became `$1,234.56`. Harmless on this register, and consistently downward on anything with finer precision — the office-expense grids and Xero. Now routed through `money.parse()`.
- Dates ISO-8601 `TEXT`; event timestamps unix-seconds `INTEGER`.
- `entity_id NOT NULL` on every fact table from `001`; `entity_id` means legal entity everywhere; attachments use `owner_type`/`owner_id`.
- `period` seeded FY24–FY35, month 1 = July. **An FY label is the calendar year the year *ends* in**: FY27 = 1 Jul 2026 – 30 Jun 2027. The source workbooks label that same year "FY26/27", so the importer maps `FY26/27 → FY27` and `FY27/28 → FY28` **explicitly, and asserts the mapping against a known row**. An off-by-one-year import is silent, survives every total-level reconciliation, and is found months later.
- **Rates are dated rows, never configuration.** `tax_rate(entity_id, effective_from, rate_bp)` and `payroll_rate(entity_id, kind, effective_from, rate_bp)` are tables; every computed figure records the `rate_bp` it used. Current value: 2500 bp for CSSB (ADR-20). A rate held in config, env or a settings row is a value someone edits in place, which silently restates every prior year that was computed from it — the same failure mode as the loose AUD/USD cell in the current workbook.
- Global job-number sequence; per-entity invoice/PO sequences via `UPDATE … RETURNING` inside the issuing transaction.
- **Ambiguous legacy job codes import flagged, not blocked** (ADR-23). Rows carry `needs_resolution = 1` and an entry in `job_code_issue (raw_code, class, project_id?, status, resolved_by, resolved_at, reason)`. Resolutions write `job_code_alias` and are data, not a one-off cleanup.
- **`job_code_alias` is one-to-many**: `(legacy_code, project_id)` with no unique constraint on `legacy_code`. One customer job number legitimately covers a site that this platform tracks as several projects by work type — `JN-4335` and `JN-4407` each do. A one-to-one alias would force those projects to fight over their own history.
- The normaliser is **deliberately conservative**: it canonicalises only what it can prove, and demotes anything else to the worklist rather than guessing. `P-3655`, `P-3707` and `JN-CommS` are valid codes that fail a `JN-\d+` pattern; a clever normaliser would corrupt them.
- **A rollup may never silently include an unresolved code.** Every rollup either excludes flagged rows and reports the excluded total alongside, or surfaces them as a distinct line — never absorbs them into a headline figure. An unresolved code that quietly lands in a total is the merged-`JN-676` defect reproduced inside the platform, which is the one outcome migration must not achieve.
- `claim_line_revision`: every money-bearing edit → (who, when, field, old, new). Snapshots are queries over history, so history includes amounts. `customer_po_revision` mirrors it — POs get adjusted, and an in-place edit would retrospectively move every orders-in-hand figure ever derived from that PO. Genuine new scope is a new PO row; a correction is a revision. Both recoverable, and the distinction is the user's to make.
- **No financial year appears in a derived-figure formula.** Orders in hand is `sum(customer_po) − sum(claim_line invoiced/paid where date ≤ X)` — one definition that answers FY27 opening, FY28 opening and today, with no year in it and no annual edit. Anything shaped like `contract − opening − claims_since_<hardcoded date>` is the workbook's yearly ritual reimplemented in SQL, and is a review reject.
- Pre-platform invoicing enters as a **synthetic opening `claim_line`** per affected project: dated 30 Jun 2026, status `invoiced`, `is_opening_balance = 1`, no invoice number, immutable. `customer_po_id` is nullable **for that row only** — `CHECK (customer_po_id IS NOT NULL OR is_opening_balance = 1)`. Exactly one kind of claim line may float free of a PO, and it is the one representing history the platform did not witness. Amount is the register's `Invoiced Prior` column (ADR-22).
- **The register is self-asserting: `Purchase Order == Invoiced Prior + Contract Value FY27`, per row, all 59.** The importer checks this rather than deriving it; a failure is a hard stop, not a warning. Verified balancing to $0.00 on 20 Aug 2026.
- An invoicing row whose project is absent from the register goes to the worklist. **The importer never auto-creates a project from an invoicing row** — that would silently resurrect something a human deliberately deleted.
- `audit_log` is append-only **in the schema, not by convention**: `BEFORE UPDATE` and `BEFORE DELETE` triggers that `RAISE(ABORT)`, in `001`. Four lines; without them "append-only" is a comment.
- Rollups are `CREATE VIEW`s in migrations. Read SQL may live as strings in modules; writes only in `Db`.
- Read SQL stays portable where portability is free — no gratuitous SQLite-only syntax. `Db` plus views is the single chokepoint that keeps a later Postgres move contained; don't leak the engine past it.
- One process on the file. Local disk only, never NFS/SMB. Ad-hoc queries hit a backup snapshot.

Migrations:

- Numbered forward-only `.sql`, applied in a transaction (SQLite DDL is transactional — failure leaves the file untouched), recorded in `schema_migrations`.
- Entrypoint order: `backup.snapshot()` → `migrate()` → serve. Migrate failure = non-zero exit = unhealthy = **agent auto-rolls back**.
- **N-1 rule:** because rollback is automatic, a migration must not break the previous release's code — and `/healthz` is forward-compatible (§3) so that release can actually boot against the newer schema. Expand-and-contract; destructive contractions ship one release after the expand has been stable.
- N-1 is a **gate, not a memory**: CI checks out the previous release tag and runs *its* test suite against a database migrated to the new head.
- **Known limitation, found the first time it fired (24 Aug 2026):** the old tag's tests are frozen, so they can fail for reasons that are not incompatibility — a test that hardcoded the list of migration filenames, or one that was flaky when it was tagged. Both happened on the first real run, and neither indicated a problem: `002` is three nullable `ADD COLUMN`s and the previous release's code runs against it unchanged. The job retries once (for flakiness) and prints a triage note naming the three causes. **The remedy for a false positive is to tag a new release from a commit whose tests are correct**, so the next run has a healthy baseline — not to weaken the gate. A migration that is genuinely expand-only can be confirmed by inspection in under a minute, which is the check that actually matters.
- **The runner supplies `BEGIN`/`COMMIT`; migration files must not contain their own transaction control.** Python's `sqlite3.executescript()` commits any pending transaction before it runs, so a `BEGIN` issued beforehand is discarded — the only way to make a migration atomic is to wrap the script text itself.
- **A failed `executescript()` leaves the transaction OPEN and the completed statements IN PLACE.** Python does not roll back for you. The runner's explicit `rollback()` is therefore load-bearing, not hygiene: without it a failed migration leaves a half-applied schema, and a migration failure is precisely the moment auto-rollback fires. Verified empirically, pinned by a test that ships a deliberately broken migration.
- **Every read connection handed out is registered, and `close()` closes all of them** — not just the calling thread's. Leaving them to the garbage collector ties a file handle's lifetime to when CPython happens to collect a dead `Thread`. That is invisible on Linux, where an open file unlinks fine, and on Windows it is an error.

## 5. Data shaping — rows → dicts → JSON, once

- One representation change: `sqlite3.Row` row_factory + a tiny `rows()` helper in `db.py` → plain dicts → `json.dumps`. No entity classes, no serializers, no schema layer, no ORM.
- **The SELECT clause is the mapping.** SQL column names = JSON field names = JS property names, snake_case end to end. No camelCase translation.
- Wire types (why no encoder is ever needed): money = cents `int`, dates = ISO `str`, timestamps = unix `int`. Never floats, never pre-formatted money strings on the wire.
- **Server computes, JS paints.** Derived figures live in SQL views where expressible; structural shaping SQL can't express (nesting lines under a project, pivoting periods) is a pure `queries.py` function: dicts in, dicts out, unit-tested. The same figure is never computed in both Python and JS — after a grid PATCH the server returns recomputed row/period totals and JS patches the DOM.
- Formatting happens at the last inch only: `fmt.money(cents)` in the browser, `money()` in `render.py` server-side.
- Writes: JSON body → explicit validation in the handler (casts, range/enum checks, plain code) → `Db` method with named parameters. The Db signature is the contract; no request-model class in front of it.
- The DTO test: a function whose body only copies fields between shapes is banned; a function that reshapes, aggregates, or validates earns its lines.

## 6. Modules ("plugins")

A convention, not a framework. No dynamic discovery, no hooks, no registries.

- Vertical slice per feature: `api.py` (`ROUTES = [(method, path, handler), …]`), `logic.py` (pure domain rules), `queries.py` (read SQL + row shaping), `module.js`, tests.
- `main.py` holds the one explicit list `MODULES = […]`; adding a feature = folder + one line. Grep-able.
- Modules never import each other; shared code graduates to a top-level file or isn't shared.
- Migrations stay global/numbered; a module's `Db` write methods live in a commented section of `db.py`.
- The shell renders nav from a server-emitted JSON manifest of registered modules.

## 7. Frontend

- Shell `index.html` + `tokens.css` + `base.css` + `app.js` + one JS file per module. Vanilla ES modules loaded directly; no bundler, no npm, no CDN.
- `app.js` exports three primitives; components use nothing else for DOM/net:
  - `h(tag, attrs, …children)` — element builder built on `createElement` / `textContent` / `setAttribute`. **`innerHTML` appears nowhere in `static/`, including inside `h()` itself.** There is no blessed exception, so the guardrail is a flat grep.
  - `api(method, path, body?)` — the only `fetch` call in the codebase; attaches JSON headers, throws on non-2xx with the server's error message.
  - `fmt` — `money(cents)`, `date(iso)`, `num(n, dp)`.
- Design tokens (`tokens.css`, everything reads custom properties, no literals in components):
  - Graphite/ISA-101-style palette: near-black surfaces in light-grey text, ONE muted-amber accent; saturated colour reserved for exceptions (errors, negative variance).
  - Three type layers: `--font-display`, `--font-ui` (one system-grotesk sans), `--font-data` (one mono). **All money/quantity cells render in `--font-data`** with `font-variant-numeric: tabular-nums`.
  - Flat, square, hairline: 0 radius, 1 px borders, no shadows. Control height and spacing on a single scale variable.
- `datatable.js` — one generic component seeding every read-only list view. Contract:
  - input: a model `{columns:[{key,label,align,fmt}], rows, filters?, searchKeys?, pageSize?}`
  - behaviour: client-side column sort (click header, toggle asc/desc), **multi-select** per-column filters, substring search, paging with row-count footer
  - a `onVisible(rows)` callback reports the FILTERED set (not the page) so page-level totals follow the filters. A total that silently describes a subset of what its label claims is how a dashboard misleads, so the page must also say when it is filtered
  - controls build once and keep DOM identity across re-renders (focus/open state survives); rows re-render from the model.
- Interactive screens (invoicing grid): server-rendered `<table>`; click cell → input; Enter/Tab commits `PATCH /api/…`; server responds with recomputed row/totals JSON; JS patches the DOM. **No optimistic UI — the server's response is the truth painted back.**
- Server-rendered report/dashboard pages come from `render.py` (a query + a loop). **Escaping is the helper's job, not the caller's:** `page()`, `table()`, `money()` and every value-taking helper HTML-escape by default; emitting markup requires an explicit `raw(...)` wrapper, which is grep-able and reviewable; `esc()` is exported for the rare hand-rolled fragment. f-strings *are* the template engine here, so these helpers are the only thing between a supplier name and stored XSS — the server half gets the same rigour as the JS half, not less. Drill-through is plain `<a href>`.
- Guardrails (`tests/js_guardrails.py` + `tests/render_guardrails.py`, pure-Python static checks, CI-gated):
  - per-page JS byte budget (< 50 KB uncompressed)
  - no external URL / CDN import anywhere in `static/`
  - `fetch(` appears only inside `api()` in `app.js`
  - no `innerHTML` assignment anywhere in `static/`
  - **`ops/static/` holds assets only.** Every file in it is reachable at `/static/<name>`; a source file there is always a mistake. Enforced by existence, not just by the MIME allowlist that refuses to serve it
  - in `render.py` and any module emitting HTML: no f-string interpolation inside a returned markup string unless the expression is `esc(…)`, `raw(…)`, or a `render.py` helper

## 8. OIDC (login)

Hand-rolled authorization-code flow, confidential client, ~200 lines using `urllib.request`:

1. `GET /login` → redirect to Google's auth endpoint with `client_id`, `redirect_uri`, `scope=openid email profile`, fresh single-use `state`.
2. Callback: verify `state` (single-use, then burned) → POST code + client secret to Google's token endpoint over TLS. The request uses a **certificate-verifying** `SSLContext` (stdlib default; §13 asserts `ca-certificates` is present in the image). A verification failure is a hard error — there is no retry-without-verification path, because step 3 spends the entire trust budget here.
3. Parse the ID token **payload only** (base64 JSON). Signature verification is deliberately out of scope: OIDC Core §3.1.3.7 permits this precisely when the token arrives over TLS direct from the token endpoint, which is the only path here. Enforce in code and review — a token is never accepted from the browser, a redirect fragment, a header, or any other source.
4. Claim checks, **all mandatory and all fail-closed — an absent claim is a rejection, never an unchecked pass**:
   - `iss` ∈ {`https://accounts.google.com`, `accounts.google.com`}
   - `aud == client_id`
   - `exp` in the future, ±60 s clock skew
   - `hd == commsecurity.com.au`. **A missing `hd` rejects.** This claim is the only thing standing between this system and every Gmail account in the world; "absent, so skip the check" would be a total authentication bypass.
   - `email_verified` true
5. **Identity is keyed on `sub`, never email.** Workspace addresses get reassigned to new staff, aliased, and renamed on marriage; `sub` is stable and unique for the life of the account. Email and `name` (from the `profile` scope) are stored as display attributes and refreshed on each sign-in; `display_name` falls back to email when `name` is absent. Approver fields and `audit_log` render the name, not the address — retrofitting that after audit rows exist is the annoying version. Keying on email means a departed employee's replacement inherits their user row and its grants.
6. **Provisioning on first sign-in: role `viewer`, zero entity grants.** A valid Workspace identity buys the ability to log in and look at an empty application — nothing else. Visibility of any entity's financial data requires an explicit `user_entity_role` grant made by an `admin`. Rationale: the `hd` check establishes that someone is staff; it says nothing whatever about whether they should see money, and shared mailboxes, contractor accounts and service accounts all pass it. Auto-provisioning writes an `audit_log` row, as does every subsequent grant.
7. Mint the session token (§9); roles are resolved per request from `user_entity_role`, so the grant an admin makes applies on the user's next click without re-login.

## 9. Sessions & authorization

Session tokens — identity only, HMAC-signed:

- Format: `base64url(json) + "." + base64url(hmac_sha256(key, body))`. Payload exactly `{kind:"session", sub:<user_id>, tv:<token_version>, exp:<unix>}`. 12 h TTL. Cookie `Secure; HttpOnly; SameSite=Lax`.
- Signing key: 32 random bytes auto-generated on first boot to `/data/secrets/session.key`, `0600` (`os.open` with mode; write via fd). Never entered, never leaves the volume.
- Verify per request: constant-time signature check (`hmac.compare_digest`) → `exp` in future → load user → token's `tv` == `users.token_version` else reject.
- **A token is never a bag of permissions.** Roles resolve from `user_entity_role` on every request → role edits apply next request; revocation is instant via `UPDATE users SET token_version = token_version + 1`. Offboarding = Workspace suspension (no new logins) + version bump (kills live sessions).
- No session table, no cleanup job.

Authorization:

- Roles per entity, enumerated: `viewer`, `operations`, `approver`, `admin`. **No role implies another.**
- `approved → invoiced` requires `approver`.
- Role checks via decorator in `http_util.py`; a route registered without an explicit role declaration fails at boot.
- Append-only `audit_log` (who, action, target, ts): sign-ins, role changes, token-version bumps, claim status transitions. (`claim_line_revision` already covers money edits.)

## 10. Secrets — no values on file

Scheme:

- `secret://NAME` is a **reference**; any other string passes through unchanged. References — never values — are what config, env vars, release manifests, logs, and API responses may carry.
- `main.py` resolves the whole config **once at startup** into a runtime copy; the original config object (which may be logged/republished) keeps the references.
- An unresolvable reference **fails boot loudly** (exception names the secret, never a value) — a service must never start with a blank credential. Boot failure = failed health gate = automatic rollback, so a missing secret self-reports.
- Nothing ever logs a secret value.

Providers — selected explicitly, **never a fallback chain**:

- **local** (default): JSON map at `/data/secrets/store.json`, `0600`, app user. Written only by `docker exec ops python -m ops.secrets set NAME` (value read from **stdin** — never argv/shell history). `ops.secrets list` prints names only.
- **remote** (dormant option): `OPS_SECRETS_URL` + `OPS_SECRETS_TOKEN` set **together** → resolve each ref via `GET {url}/secret/{name}` with `Authorization: Bearer`, TLS pinned to a configured CA file. Setting only one of the pair is a boot error. Moving to a central secrets service later = env change, zero code.

Inventory (keep it this small):

- `OIDC_CLIENT_SECRET` — the one operator-entered secret, set once per host.
- Session signing key — auto-generated (§9), never entered.
- TLS private key — file on the volume.
- Registry PAT — lives in the fleet manager's settings, not in this app.

CI gate: grep repo + compose template + `.env.example`; any known secret name whose value isn't `secret://…` or `CHANGE_ME`, or any high-entropy literal, fails the build.

- The entropy check ships with `tests/secret_allowlist.txt` from day one. It will otherwise fire on sha256 test fixtures, image digests and base64 sample payloads, and a gate that cries wolf in week two is a gate somebody disables in week three. Each allowlist entry is one exact literal with a one-line reason; a wildcard entry is a review reject, and the file is small enough that additions get read.

## 11. Documents

- `documents.py`: store file at `/data/documents/<aa>/<sha256>` (content-addressed, dedup free); metadata row `(owner_type, owner_id, filename, content_type, size, sha256, uploaded_by, ts)`.
- Served only through an authorised handler; never a direct static path.
- Soft-delete the metadata row; blobs are immutable (GC is a later, measured decision).

## 12. State & backup

- `/data` is **all** state; back up one volume, restore on any docker host.
- **Snapshots run in-process, not under crond.** A daemon thread started by `main.py` calls `backup.snapshot()` hourly. §0 promises one Python process and that stays literally true: busybox crond means a second process, a PID-1/supervisor question, and a scheduler that can't see the write lock. A thread is fewer moving parts and coordinates with `Db` directly.
- `backup.snapshot()`: `VACUUM INTO /data/backups/ops-<utc>.db` (atomic, consistent, sub-second) + prune to the retention set. RPO 1 h. **Failure logs loudly and is surfaced on `/healthz` as a warning field** — a backup silently failing for a fortnight is worse than no backup, because it buys false confidence.
- **Only `backups/` and `documents/` may be copied while the app runs.** The host job rsyncs those two directories off-box and **never live `ops.db`**: a WAL database copied mid-transaction yields a `.db` and a `-wal` that disagree, and the copy is unrestorable — which you discover on the day you need it. Blobs are content-addressed and immutable, so they rsync safely by construction.
- **Backups on the volume they protect are not backups.** The off-box copy is the backup; `/data/backups/` is a convenience. The monthly restore rehearsal therefore restores *from off-box*, not from `/data/backups/`, and is documented with the elapsed time against the §14 60 s budget. First rehearsal 21 Aug 2026: full volume loss, restored in **0.03 s**, 59 projects and $3,520,041.73 verified over HTTP. Record in CS-OP-RUN-001.
- Nightly `PRAGMA integrity_check`, logged loudly.
- Restore: `tools/restore.py`, which verifies the snapshot **before** overwriting anything and asserts the register still reconciles afterwards. Documents before DB (a metadata row pointing at a missing blob is a visible 404; a blob with no row is invisible and harmless). Stale `-wal`/`-shm` are removed: they belong to the *old* database and would be replayed over the restored one.
- **The backup deliberately excludes `secrets/` and `tls/`.** Copying a credential store off-box puts live secrets on a second machine with a different threat model. The consequence, found by the first rehearsal rather than by reading: **the app will not boot after a restore** until `OIDC_CLIENT_SECRET` is re-entered and certificates replaced. So `OIDC_CLIENT_SECRET` must be recoverable from somewhere other than this system — if it exists only on the `/data` volume, a volume loss is unrecoverable without re-registering the OIDC client. Procedure in CS-OP-RUN-001.
- Entrypoint snapshots before migrating (§4) so every rollback has a matching pre-migration file. **Note the asymmetry the fleet manager creates:** image rollback is automatic, database rollback is *not* — the pre-migration snapshot exists, but restoring it is a deliberate operator act that discards every write since. Nothing rolls the schema back for you. This is precisely why §4's N-1 rule and §3's forward-compatible `/healthz` are load-bearing rather than tidy.

## 13. Build & deploy

CI (GitHub Actions, push to `main`; `v*` tags → versions):

1. `python3 -W error::ResourceWarning -m unittest discover -s tests -v`
2. Gates: `pyright --strict` (0 errors) · JS guardrails · `render.py` escaping guardrails (§7) · no-secret-values grep with allowlist (§10) · "0 pip deps in image" inspection · N-1 check (previous release tag's suite against the new migration head, §4)
3. `docker build` from a **digest-pinned base** — `FROM python:3.12-alpine@sha256:…`, never the floating tag. Everything downstream of this build is digest-pinned and signed ("a release means exactly those bytes forever"); an unpinned base makes that true only *after* the build, so two CI runs of the same commit can ship different bytes — and specifically different SQLite versions, while `STRICT` tables and `UPDATE … RETURNING` require ≥ 3.37. Retry ×3 on transient pulls — red CI must mean *our* code broke. Tag `ghcr.io/commsecurityau/cs-ops:latest` + `:<sha7>`.
4. In-image assertions, run before push: `sqlite3.sqlite_version >= 3.37` · zero pip packages beyond the base · `ca-certificates` present (§8's "TLS is the trust boundary" argument rests entirely on certificate validation actually working).
5. Size gate: image **< 75 MB** hard fail (measured 47 MB, 21 Aug 2026)
6. Push to ghcr (`packages: write`)

**A gate's exit code must come from the gate.** The first version of the type step read `pyright --outputjson … || pyright …`, intending readable output on failure — which meant the step's exit status came from the *retry*, so it could never fail the build. Any `||`, `|| true`, `continue-on-error`, or second invocation in a gate step is a review reject: it produces a green tick that means nothing, which is worse than having no gate at all because it is trusted.

**Gates are unittest cases, not shell steps.** `tests/test_gates.py` holds the dependency, secret-scanning, base-image-pin and migration checks, so they run on `make test` and on Windows. A gate that exists only in CI is one you discover you have broken after pushing.

Base-image bumps: the pin is only a virtue if it moves on purpose. A bump is an ordinary PR — new digest, full suite, merged like anything else — raised by an automated dependency PR or a diarised quarterly review. A pin nobody moves is an unpatched base.

Deploy — via the company fleet manager (the internal VM is an enrolled device). Its contract, which this app must satisfy:

- A release is a compose file; every image ref on **its own `image:` line** (the manager's line-based pinner rewrites `repo:tag` → `@sha256:…` digest at release creation; a release means exactly those bytes forever).
- Release env carries **non-secret config + `secret://` refs only** — manifests are signed, persisted and shipped, so a value there would live in three new places.
- Named volume `ops-data:/data`. Staging dirs are wiped on supersede; volumes persist.
- The device agent verifies the signed manifest, pulls over the tunnel, stages, then **health-gates on `/healthz`**; on failure it rolls back locally — no network, no operator. (Hence §4's N-1 rule, §3's forward-compatible health check and §10's fail-loud boot.)
- First deploy per host: `docker exec ops python -m ops.secrets set OIDC_CLIENT_SECRET` once; later releases find it on the volume.

Dev loop:

- `make dev` = `OPS_TLS=off OPS_DATA=./data python3 -m ops.main` with seeded DB. Clone → running < 1 min.
- `make test` < 10 s, no docker.

## 14. Budgets

| Budget | Limit | Enforcement |
|---|---|---|
| Image size | < 75 MB | CI hard fail — measured **47 MB** |
| Runtime pip deps | 0 | CI hard fail |
| Type errors (`pyright --strict`) | 0 | CI hard fail |
| JS per page (uncompressed) | < 50 KB | CI hard fail |
| Secret values on file | 0 | CI grep + allowlist, hard fail |
| Test suite wall time | < 10 s | CI hard fail — measured **~4 s**, 189 tests |
| Cold start → serving | < 2 s | CI in-process timer |
| Container RSS steady | < 128 MB | trend, staging |
| Dashboard p95 @ 250 k-row fixture | < 150 ms | trend, staging |
| List/grid p95 | < 100 ms | trend, staging |
| Restore snapshot → serving | < 60 s | monthly rehearsal |
| New feature | ≤ +200 ms cold start · ≤ +8 MB RSS · ≤ +15 ms dashboard p95, else a recorded trade-off | review |

- Image gate is 75 MB against an expected ~60. A 2× headroom gate catches nothing — it only fires after the thing it was meant to prevent has already happened twice over.
- The p95 rows are trends rather than gates because CI runners are too noisy to fail a build on, but a trend is worthless if the input drifts. `tools/fixture.py` generates the 250 k-row dataset **deterministically from a fixed seed and fixed row counts**, is committed, and is versioned — changing it is a PR that resets the trend line explicitly rather than silently. Numbers from different fixture versions are never compared.

## 15. CLAUDE.md (repo root, verbatim)

```markdown
# CLAUDE.md — cs-ops

Internal financial operations platform. Read CS-OP-ARCH-002 first; the stack
is locked — implement, don't re-litigate.

## Stack
Python 3.12 stdlib ONLY. ThreadingHTTPServer + ssl, with explicit timeouts,
body caps and a connection cap. sqlite3, WAL, one write connection under a
lock + thread-local read-only connections (db.py owns all writes). Vanilla
ES modules + fetch, no framework, no build step. Hand-rolled OIDC (Google
Workspace) + HMAC identity-only session tokens. One docker image
(python:3.12-alpine, digest-pinned), one /data volume, deployed by the
fleet manager, health-gated on /healthz, auto-rollback.

## Hard rules
- ZERO pip runtime deps. ZERO npm. New dependency = ADR, not an import.
- Money: integer cents, columns *_cents, never `amount`. Rates: basis
  points. One rounding function (half away from zero), one place.
- entity_id means legal entity everywhere; attachments use owner_type/owner_id.
- Handlers thin: parse, auth, call, respond. Writes only via Db methods on
  the ONE write connection under the lock; reads on thread-local read-only
  connections, no lock. Reads may be SQL strings in modules, kept portable.
  Rollups are views in migrations.
- Data shaping: rows -> dicts -> JSON, once. SELECT column names ARE the
  JSON/JS field names, snake_case end to end. Wire carries cents ints, ISO
  dates, unix ints — never floats or formatted money. Server computes, JS
  paints; format at the last inch. A function that only copies fields
  between shapes is banned; reshape/aggregate/validate earns its lines.
- Migrations: numbered forward-only .sql, transactional, N-1 compatible
  (rollback is automatic — old code will meet new schema).
- Modules never import each other. New feature = folder + one line in
  MODULES. No dynamic discovery, no hooks.
- Every financial rollup test pins known-good workbook numbers (FY27 total
  352773300 cents; Mar FY27 25461055; Dec FY27 57630520).
- FY label = the year the FY ENDS in. FY27 = Jul 2026-Jun 2027. Workbooks
  say "FY26/27" for that same year; the importer maps and ASSERTS it.
  Migration starts at FY27; there are no FY26 actuals.
- Rates (tax, payroll, FX) are dated rows per entity, never config. Every
  computed figure records the rate_bp it used. Tax = 2500 bp (25%).
- NO financial year in a derived-figure formula. Orders in hand is
  sum(customer_po) - sum(claims where date <= X). Anything shaped like
  "claims since <hardcoded date>" is the workbook's July ritual in SQL and
  is a review reject. Pre-platform invoicing is a synthetic opening
  claim_line (is_opening_balance=1, PO nullable ONLY for that row), never a
  column. POs get customer_po_revision like claim lines: new scope is a new
  row, a correction is a revision.
- SQLite: one process, local disk, never NFS. Pragmas set per-connection in
  db.py only.
- Backups: hourly VACUUM INTO from an in-process thread, never crond — one
  process stays one process. Rsync copies backups/ and documents/ ONLY;
  copying a live WAL db yields an unrestorable pair. Restore rehearsals
  restore from off-box, not from /data/backups/.
- OIDC: scope "openid email profile". state single-use. Require iss,
  aud == client_id, exp, email_verified
  and hd == commsecurity.com.au — ALL fail-closed, a missing claim rejects
  (a missing hd that passes is a total auth bypass). ID tokens accepted ONLY
  from our own token-endpoint response over verified TLS. No other path.
- Users are keyed on sub, never email. First sign-in provisions role viewer
  with ZERO entity grants; seeing any money needs an explicit admin grant.
  hd proves staff, not entitlement. Store display_name from `name`; render
  people, not addresses.
- Legacy job codes import with needs_resolution=1 into job_code_issue, they
  do NOT block the importer. A rollup NEVER silently absorbs a flagged row:
  exclude and report the excluded total, or surface it as its own line.
  job_code_alias is ONE-TO-MANY (one customer code, several projects).
- The register asserts itself: Purchase Order == Invoiced Prior +
  Contract Value FY27, per row, hard stop on failure. Opening balances come
  from Invoiced Prior, NEVER from a single-FY column. Never auto-create a
  project from an invoicing row that has none.
- Sessions: HMAC identity-only tokens {kind, sub, tv, exp}. Tokens NEVER
  carry permissions — roles re-resolve from DB every request. Revocation =
  bump users.token_version. Signing key auto-generated 0600 on the volume.
- Secrets: secret://NAME references ONLY in config/env/manifests/logs.
  Resolve once at startup; unresolved = loud boot failure; never log a
  value; provider selection explicit, never a fallback chain. No secret
  value in git, env files, the image, or a release manifest.
- Frontend: DOM via h() (createElement/textContent); innerHTML appears
  NOWHERE in static/, h() included. fetch() only inside app.js api(). All
  colour/type via tokens.css custom properties; money in --font-data with
  tabular-nums. Guardrails suite enforces all of this.
- Server HTML: render.py helpers escape by default; raw() is the only
  opt-out and it is grep-able. f-strings are the template engine, so the
  helper is the whole XSS defence.
- Migrations are N-1 compatible AND /healthz is forward-compatible
  (applied ⊇ expected, never equality) — otherwise auto-rollback loops.
- Tests: stdlib unittest, fresh temp DB through the REAL runner (ops.db
  migrate), never a hand-rolled copy of it, no docker, suite < 10 s, run
  with -W error::ResourceWarning.
- NO PRIVATE STDLIB APIs. `ssl._ssl._test_decode_cert` was load-bearing for
  cert-expiry warnings until pyright found it: private, undocumented, named
  for testing, and free to vanish on a base-image bump — the exact failure
  the digest pin exists to prevent. Walk the DER instead.
- A gate's exit code must come from the GATE. No `||`, no `|| true`, no
  second invocation. A green tick that cannot go red is worse than no gate,
  because it is trusted.
- MUTATION-TEST every concurrency safeguard: delete the lock, confirm a test
  fails. Tests built from SINGLE SQLite statements pass without it -- SQLite's
  own mutex already makes those atomic, so they test SQLite, not us. A lost
  update needs a read AND a write in one transaction.
- Teardown stays strict (no ignore_errors). On Windows an undeleted temp DB
  is a leaked handle; that failure is the only leak detector we have, since
  Linux unlinks open files happily. Run the suite on Windows periodically.
- Budgets are CI gates: image < 75 MB, JS < 50 KB/page, 0 pip deps, 0
  secret values, 0 pyright --strict errors, cold start < 2 s. A regression
  is a failing build.
- No wrapper layers, no DTO↔DTO mapping, no speculative abstraction. Each
  new layer is justified in the PR description.
```

## 16. Decision log

- **ADR-08 — Python stdlib, not Go.** Container deployment is mandated (kills Go's static-binary edge); team fluency is Python/JS; stdlib covers HTTP+TLS+SQLite+outbound → **zero** runtime deps vs Go's two. Accepted: no compile-time types (mitigated by `pyright --strict` as a hard CI gate — see ADR-19), ~300 ms cold start, GIL (irrelevant at ~1 write/s).
- **ADR-09 — No htmx.** It's declarative fetch+swap for teams avoiding JS; this team writes JS, and the grid needs bespoke JS regardless. Identical performance, one less dependency.
- **ADR-10 — Deploy via the fleet manager.** Signed digest-pinned releases, health-gate, local auto-rollback. Consequences: honest `/healthz`, N-1 migrations, fail-loud secrets.
- **ADR-11 — In-process TLS from the internal CA.** stdlib ssl; root distributed via Workspace device management. Revisit only on a public-trust requirement.
- **ADR-12 — Secrets as `secret://` references; local store, not a secrets service.** Values in one 0600 volume store, set via stdin; remote provider dormant behind a paired env switch (no fallback chain). A secrets service was rejected on the bootstrap regress: its own master key and this app's bearer token would need delivery via env or the release manifest — exactly the banned locations — so the service *increases* secrets-at-rest (1 → ≥2) while adding a container in the boot path. Flip conditions (both explicit, neither near): a company-central secrets service with a bootstrap channel that isn't the signed manifest, or ≥3 operator-entered secrets across hosts. STP-6 note: Xero **refresh tokens rotate on use — they are mutable app state, stored AES-encrypted in the DB** (key file auto-generated 0600 on the volume, session-key pattern), not in any secrets store; only the Xero *client* secret is an `ops.secrets set` value.
- **ADR-13 — HMAC identity-only sessions.** No session table/GC; instant revocation via `token_version`; roles re-resolved per request so edits apply live. Rejected: server-side session rows, permission-bearing tokens, passwords.
- **ADR-14 — Design system & datatable specified in §7, reactive kernel rejected.** Token-driven CSS, one accent, mono data layer, generic datatable, mechanically-enforced guardrails. Rejected: any reactive/subscription kernel, component registry, WebSocket streaming, multi-theme system — this is request/response CRUD. Rejected: capability×scope×time grant algebra (4 roles × 3 entities doesn't need it; a path-prefix scope predicate is the pattern if per-project scoping ever appears).
- **ADR-15 — Rounding is half away from zero, not banker's.** Supersedes the original §4 line. Banker's rounding is right for statistical aggregates and wrong here on two counts: the pinned regression figures come from Sheets workbooks, which round half away from zero, so banker's would have made the pins irreconcilable on day one of STP-1; and half-up per line is ordinary GST practice, which is what anyone reconciling against Xero will expect. Consequence: recompute the three pinned FY27 totals both ways before STP-1 and record any divergence — the workbook is authoritative, since reconciling to it is the pins' entire purpose.
- **ADR-16 — One write connection under a lock; thread-local read-only connections.** Supersedes "ONE serialized connection". A single connection serialises reads against each other, which discards the only thing WAL buys and makes the §14 read budgets (150 ms dashboard p95 over 250 k rows, 100 ms lists) unachievable by construction — one slow read would stall the process. The split preserves every existing invariant verbatim: all writes still go through `Db`, one writer, one lock, same pragmas, same chokepoint for a later Postgres move. Cost is a thread-local and a `?mode=ro` URI.
- **ADR-17 — Hourly snapshots from an in-process thread, not busybox crond.** Supersedes §12's crond line. crond buys a second process inside a container whose headline property is that it has one, plus a PID-1/supervisor question and a scheduler with no visibility of the write lock — in exchange for nothing a `threading.Timer` loop doesn't already do. Consequence: snapshot failure must be surfaced (it now appears as a `/healthz` warning field), because an in-process job that dies quietly takes the RPO with it and nothing external notices.
- **ADR-18 — Auto-provision as `viewer` on zero entities; identity keyed on `sub`.** §8 previously said "look up/create the user row" and stopped, which in practice means whatever the first implementer picks. Two things are now fixed. Identity keys on `sub` because Workspace addresses are reassigned, aliased and renamed, so an email-keyed row hands a departing employee's grants to their replacement. Provisioning grants `viewer` with **no entity grants**, because the `hd` check proves someone is staff and says nothing about whether they should see money — shared mailboxes, contractor accounts and service accounts all satisfy it. Consequence: an admin grant is a required step in onboarding, and STP-0 exercises it (§17). Rejected: defaulting to `viewer` across all entities, which makes every Workspace account a reader of group financials on first click.
- **ADR-19 — `pyright --strict` is a hard CI gate.** ADR-08 traded compile-time types for zero dependencies and listed the loss honestly as accepted. A strict gate recovers most of it for a dev-only tool and no runtime cost, on a codebase whose defining risk is silent arithmetic error on money. "Optional" was the wrong strength: unenforced type hints become decoration within a quarter, and the value is concentrated exactly where the hints are hardest to keep honest.
- **ADR-20 — Tax and payroll rates are dated rows per entity, not configuration.** Answers Q1 (corporate tax, resolved 20 Aug 2026: **25%**, i.e. 2500 bp, as the current estimate for CSSB). "Configurable" is satisfied by a table, not a config key. A config value is editable in place, so changing it next year silently restates every figure already computed from it — which is exactly the defect the loose AUD/USD cell causes in the current workbook. A dated row means FY27 keeps computing at 25% forever after the rate moves. Per *entity* as well as per date, because the 25% base-rate-entity rate depends on aggregated turnover and passive-income tests that are assessed each financial year and assessed separately for each of the three legal entities — the 25%/30% split in the source workbook is plausibly a real difference someone recorded, not simply an error. **The rate is an estimate pending confirmation by the company's accountant**, which is a data question, not an architectural one; the schema is correct either way.
- **ADR-21 — Migration starts at FY27, the current financial year.** Answers Q3 (resolved 20 Aug 2026). There are no FY26 actuals in the source material to migrate: the Office Expenses workbook carries an FY26/27 grid and an FY27/28 grid — FY27 and FY28 in this document's labelling — and every pinned regression figure is FY27. FY28 is forward budget, not prior actuals, so year-on-year comparison is unavailable until FY28 closes. Accepted. **Consequence for migration `001`, and the one thing this answer does not settle:** the 49 active projects include work that began before 1 July 2026, so a project's claimed-to-date position may predate the migration window. Without an opening figure, contract-to-date understates and percentage-complete is wrong from day one. the opening position is carried as a synthetic `claim_line`, not a column — see ADR-22, which supersedes this clause and settles where the figure comes from. Each was a stated intent that the mechanism didn't actually deliver:
- **ADR-22 — Pre-platform invoicing is a synthetic opening `claim_line`, not a column on `project`.** Settles the clause left open in ADR-21.

  *Decision.* One row per affected project: dated 30 Jun 2026, status `invoiced`, `is_opening_balance = 1`, no invoice number, immutable, `customer_po_id` NULL. Amount = the register's `Invoiced Prior` column.

  *Validated against source, 20 Aug 2026.* 59 projects · Purchase Order $7,231,907.00 · Invoiced Prior $3,711,865.27 across **29** opening rows · **Orders in Hand at FY27 start $3,520,041.73** · residual **$0.00**.

  *The column was reshaped during this exercise, and that is the load-bearing part.* The register originally held `Invoiced FY26`, which is not the same quantity as "invoiced before the platform's window": five DLP projects had billing in FY25 that the column did not reach. Sourcing the opening balance from it would have understated openings by **$858,354** and given those five projects that much in phantom orders in hand — a defect that reconciles perfectly at every total and is invisible until someone questions a project. The column is now `Invoiced Prior`, merging both years, which is exactly the quantity this ADR needs and turns a derivation into a stated fact.

  *Why not a column.* "Contract Value FY27 (Orders in Hand)" is a stored remainder that exists because a spreadsheet cannot derive it — the same species of artifact as the Future Invoicing copy-forward tab. Migrating it as data buys a "Contract Value FY28" column next July, i.e. the annual ritual this platform exists to delete. An `opening_claimed_cents` column forces every orders-in-hand query into `contract − opening − claims_since_1_Jul_2026`, hardcoding the FY27 boundary into the formula and requiring a rewrite each year. The synthetic row makes it `contract − claims_up_to(X)` — no year in the formula, no annual edit. **This was the deciding factor.**

  *Accepted cost.* Migration writes a fiction into the most audited table in the system, and `claim_line_revision` has no origin story for it beyond the flag. Mitigated, not refuted, by the flag, immutability and a recorded source. Rejected alternative: keep migration artifacts visibly outside the fact table — defensible, costs a formula edit every July.

  *Why `customer_po_id` is nullable.* Some projects have prior invoicing with no PO recorded, and even where POs exist the workbook does not say which one the FY26 invoicing was against. Attaching the row to a PO would be a guess dressed as data; fabricating a placeholder PO would pollute per-PO orders in hand and invent a document that never existed. The `CHECK` constraint documents the single exception rather than leaving the column loosely optional.

  *Consequence — the register asserts itself.* `Purchase Order == Invoiced Prior + Contract Value FY27` holds per row on all 59 and balances to $0.00 in aggregate, so the importer **verifies rather than derives**. A failure is a hard stop. `sum(customer_po) == Purchase Order` remains a separate non-blocking report, since PO records may be incomplete without invalidating the opening figure.

  *Also settled by the data:* zero projects have prior invoicing with no PO recorded, so the nullable `customer_po_id` case does not arise in this dataset. The `CHECK` constraint stays anyway — it costs nothing and documents the intent for data that has not been inspected.

  *Sequencing.* On the STP numbering as it stands, `claim_line` and `customer_po` arrive with migration `002` (STP-2, invoicing), not `001` (STP-1, project register). The opening rows and the reconciliation report therefore belong to **STP-2**, not the first importer, and `001` needs to preserve nothing extra — both source columns are re-read from the workbook when STP-2 runs. Confirm against ARCH-001's migration numbering before writing either.
- **ADR-23 — Ambiguous job codes import flagged; Ops Manager is sole resolution authority.** Answers Q4 (resolved 20 Aug 2026).

  *Authority.* The Operations Manager decides, for all three defect classes. This is consistent with job-number issuance moving to this platform — the person who will own issuance owns the historical cleanup.

  *Classes, with counts measured against source on 20 Aug 2026 (59 projects).* **A, format variants** — now **0**; the single case (`JN 5108`) was corrected at source. The normaliser stays conservative regardless: `P-3655`, `P-3707` and `JN-CommS` are valid codes that fail a `JN-\d+` pattern, and cleverness would corrupt them. **B, placeholders** — **6 rows**: `TBA` ×5 (PDNSW SOC, 88 Robertson St, Dover House, 130 Little Collins, Maitland storage cage), `na` ×1 (CommSecurity Office – KODE OS). Each needs a job number issued or a decision that it is not project work; issuance moves to this platform anyway, so most self-resolve at STP-1. **C, shared codes** — **2 codes**: `JN-4335` (120 Balmain Rd SBP + ICN) and `JN-4407` (The Lindrum ICN + IBP). **These are not defects.** One customer job number covers a site that this platform tracks as two projects by work type, which is why `job_code_alias` is one-to-many.

  *Genuine collisions: zero.* `JN-676` and `JN-5416` were true merged-history cases — one code across unrelated sites — and both were resolved at source during this review (Brennan Pl reissued to `JN-6694`; 88 Robertson St set to `TBA`). The category that would have moved money between projects is empty, which is what makes flagged-and-imported comfortable rather than merely acceptable.

  *Timing — flagged, not blocked.* Rows import with `needs_resolution = 1` into a `job_code_issue` worklist and are resolved in the platform. STP-1 therefore does not block on a cleanup exercise, and resolutions become audit data (who, when, why) rather than unlogged spreadsheet edits. Accepted cost: the database knowingly holds wrong attributions for a period, so §4's rollup rule is load-bearing — a flagged row may never be silently absorbed into a headline figure. Rejected: clean-before-import, which yields reconciling pins from day one but converts STP-1 into a data-cleanup project and loses the resolution audit trail.

  *What this gates.* Resolution gates **STP-5** (the dashboard), not STP-1 — which is where ARCH-001 already argued the gate belongs, since a dashboard over unresolved data is worse than no dashboard.

  *Effect on the pinned figures.* Class C resolution splits history between projects without changing any total, so the FY27 grand-total and monthly pins survive it. Class B does not: reclassifying a `TBA` row as overhead moves money between project expenses and office expenses, so **category-level** figures can legitimately move. When they do it is a finding, not a regression — the pin records what the workbook said, and the workbook was wrong. Any such movement is recorded against the resolution rather than reconciled away.

  *Total worklist: 8 rows* out of 59 projects, none carrying merged history.

  *Bus factor, stated plainly.* Sole authority means class C decisions get no second reader, and class C is where the money actually moves. The `reason` field on `job_code_issue` is mandatory for class C specifically — with no reviewer, the written rationale is the only check that exists.
- **ADR-24 — OIDC scopes include `profile`.** Partially answers Q2 (resolved 20 Aug 2026). `openid email profile` rather than `openid email`, so `name` is available and approver fields, claim history and `audit_log` render a person rather than an address. No additional consent friction on an Internal-type app, and retrofitting display names after audit rows exist is materially worse than adding the scope now. **Q2's remaining part is an action, not a decision:** registering the client in a Cloud project inside the Workspace org, consent screen set to *Internal* (which restricts the flow to `commsecurity.com.au` accounts before §8's `hd` check is even reached), redirect URIs `https://ops.commsecurity.com.au/auth/callback` and `http://localhost:8080/auth/callback`. It blocks STP-0's first exit criterion and nothing earlier.
- **ADR-25 — Migration `001` captures the validated register figures; `002` expands them.** Supersedes ADR-22's sequencing note, which said `001` preserves nothing because STP-2 re-reads the workbook. That held while the workbook was a stable artifact. During the 20 Aug 2026 validation it was edited five times — codes reissued, a column merged and renamed, a phantom row deleted — so "re-read at STP-2" means "re-validate at STP-2 against a moving target", and the reconciliation done once would have to be done again. `project` therefore carries `purchase_order_cents` and `invoiced_prior_cents` as explicitly-labelled migration inputs; `002` expands them into `customer_po` plus the synthetic opening `claim_line` and contracts the columns away. Textbook expand-and-contract, which §4 already mandates. Accepted cost: two columns live in `project` for one release that are not the long-term model, and the comment saying so must survive.
- **ADR-26 — `pyright --strict` with four named exclusions, recorded in `pyrightconfig.json`.** Implements ADR-19. Strict mode on a stdlib-only codebase with no type stubs produces mostly noise about `sqlite3.Row`, so the `reportUnknown*` family is off. `reportUnusedFunction` is off because route handlers are registered by decorator and the checker cannot see that as a use — without it every handler is flagged. Everything that catches real defects stays on: attribute access, optional access, argument types, incompatible overrides, and **`reportUnusedVariable`**, which found two pieces of dead code on first run. The exclusions are in a committed config rather than scattered `# type: ignore` comments, so the holes are countable and reviewable in one place. First clean run 21 Aug 2026; the same run found the private-API dependency described in §15.
- **ADR-27 — The Project List tab stays editable; drift is detected instead of prevented.** Supersedes CS-OP-STP-001's standing rule for this tab only. The rule was that a workbook tab goes read-only when its phase ships, as the control against a shadow system. That is refused here: the platform does not yet do everything the workbook does, and locking it would push work into a third place rather than eliminating a second. **But the control existed for a real reason, so it is replaced rather than dropped.** The risk was never editing the workbook — it was the two diverging in silence, and only discovering it at STP-5 when a dashboard figure fails to match and nobody can say when it stopped. `tools/drift_check.py` compares the exported tab against the platform and exits non-zero on any difference. Three deliberate non-behaviours: it does not judge which side is right (either can legitimately be newer), it does not treat a platform-issued job number as drift (once the worklist turns `TBA` into `JN-6889` the workbook is merely stale on a field the platform now owns, and reporting that would bury real findings under expected ones), and **it never writes** — a checker that repairs what it finds is a second, unreviewed import path. Matching is on project NAME, not job code: the code is the obvious key and the wrong one, because the platform reissues it. Accepted cost: drift is found within a day rather than prevented outright, and someone has to read the report. Revisit when the platform covers everything the tab does, at which point the original read-only rule becomes free.
- **ADR-28 — The platform does not allocate job numbers yet; creation records or defers.** Reverses part of STP-1's claim, and un-ticks one of its exit criteria. iTrade still issues job numbers, so a number allocated here could collide with one iTrade hands out tomorrow — and the collision would not surface until both reached Xero, by which point invoices reference each. Creating a project therefore offers two choices only: **record the code we were given**, or **say plainly that we do not have one yet** (`TBA`, which lands on the worklist so the gap is visible rather than blank). The default is to defer, because a default that allocates is exactly how two projects acquired numbers nobody wanted. The sequence and `_issue_job_number` remain, and the worklist keeps its `issue` action, so allocation is available as a deliberate act by someone who knows the number is ours to give. **This is "not yet", not "never":** STP-1's exit criterion "the next new job number is issued by the platform, not iTrade" is un-ticked and stays open until job-number authority actually moves. Accepted cost: the platform is not authoritative for job numbers, so the register can carry `TBA` rows indefinitely and the worklist will not empty on its own. **Open question this raises:** while iTrade is authoritative, issuing from the worklist carries the same collision risk as issuing at creation — it is a smaller target because it is deliberate and rare, but it is the same risk, and it should be settled before the worklist is used to issue again.
- **ADR-29 — The platform allocates only from a reserved block, and refuses until one is agreed.** Completes ADR-28. The sequence was seeded above the legacy high-water mark (`JN-6889`), which sits *inside* the series iTrade still issues from — two systems drawing on one series will eventually hand out the same number, and the collision surfaces only when both reach Xero with invoices against each. Migration `002` adds `range_start`/`range_end`/`range_note` to `job_number_sequence`, **left NULL**, and `_issue_job_number` raises rather than allocating while they are. The safe state is the default state, not something to remember. Reserving a block refuses if any existing code falls inside it — a range containing a code already in use is not reserved, it is a collision waiting for someone to allocate into it — and exhausting a block refuses rather than running past its end into numbers iTrade owns. `--note` is mandatory: a reserved range with no record of who agreed it is a number nobody can later defend. Worklist `assign` now checks for duplicates, since typing the code iTrade gave us is the normal path and therefore the one door a duplicate could still come through. Set with `tools/job_number_range.py`; the tool records an agreement, it cannot make one.
- **Corrections (defects, not decisions — recorded here because the document is locked).** Each was a stated intent that the mechanism didn't actually deliver:
  - `/healthz` moved from schema-version *equality* to `applied ⊇ expected`. Equality guaranteed that any rolled-back release would declare itself unhealthy against the schema the failed release had already migrated — an unrecoverable rollback loop that silently negated the N-1 rule it sat next to.
  - `render.py` gains escape-by-default helpers with an explicit `raw()` opt-out, closing a stored-XSS path. The JS half had four CI checks guarding `innerHTML`; the server half, which builds HTML from f-strings, had none.
  - `ThreadingHTTPServer` gains explicit timeouts, body caps and a connection cap — the stdlib defaults leak threads on half-open connections and buffer request bodies unbounded.
  - `audit_log` append-only becomes `BEFORE UPDATE`/`BEFORE DELETE` triggers rather than a claim in prose.
  - N-1 becomes a CI gate (previous release tag's suite against the new migration head) rather than a discipline to remember.
  - The base image is pinned by digest. Every layer downstream was digest-pinned and signed while the input to the build floated, so two runs of one commit could ship different bytes — and different SQLite versions, against a schema requiring ≥ 3.37. Bumps are ordinary gated PRs.
  - The host rsync is narrowed to `backups/` and `documents/`. It previously copied all of `/data`, i.e. a live WAL database mid-transaction, producing a `.db`/`-wal` pair that disagree and a copy that fails only at restore. Rehearsals now restore from the off-box copy, since backups on the volume they protect are not backups.
  - Image budget 120 MB → 75 MB against an expected ~60; a 2× headroom gate only fires long after the drift it exists to catch.
  - The p95 fixture becomes a committed, seed-deterministic `tools/fixture.py` — a trend line whose input silently changes is not a trend line.
  - The entropy grep ships with an allowlist from day one, or it fires on sha256 fixtures and image digests and gets disabled by week three.
- **Delivery plan: see CS-OP-STP-001**, which supersedes ARCH-001 §11 in full. That section was written against Go/Postgres/Caddy and quotes pre-cleanup figures; three of its exit criteria are contradicted by ADR-21, ADR-23 and ADR-25. The migration-to-STP mapping lives there rather than being inferred.
- Carried from ARCH-001 + review: data model §5 (with `claim_line_revision`, `owner_type`/`owner_id`, roles enumerated, intercompany flags deferred until Q10 answered), phases §11 (+ grid prototype in STP-0; answer historical-scope Q3 before the STP-1 importer; batch bulk imports), risks §12, open questions §13.

## 17. STP-0 exit criteria

- Staff sign-in via Workspace SSO at `https://ops.commsecurity.com.au` (internal CA) → user auto-provisioned as `viewer` on zero entities → empty project list rendered through the module system.
- An `admin` grants that user a role on one entity; the projects appear **on the next request, without re-login** (proving roles resolve per request, §9). A second account with no grant still sees nothing.
- A token presented with a missing or wrong `hd` claim observed to be rejected.
- CI green with every §14 gate live.
- One release deployed end-to-end through the fleet manager, **including a forced health-gate failure proving auto-rollback**.
- Boot without `OIDC_CLIENT_SECRET` observed to fail loudly and roll back; secret then set via `ops.secrets set` from stdin.
- A `token_version` bump observed to kill a live session.
- One backup snapshot restored and served.
- Invoicing-grid interaction prototype working against seeded data, styled from `tokens.css`.

*End — hand this file back with "implement STP-0" to begin.*
