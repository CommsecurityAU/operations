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
                    ├─ db.py — sqlite3 (stdlib), ONE serialized connection
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
| DB | stdlib `sqlite3`, WAL, one serialized connection | Postgres, Supabase, any ORM |
| Frontend | static vanilla ES modules + `fetch`; no build | htmx, React/Vue/Svelte, bundlers, npm |
| Auth | hand-rolled OIDC (Google Workspace) + HMAC identity-only session tokens (§9) | Authelia, OIDC libraries, passwords, server-side session table |
| Secrets | `secret://` references + 0600 volume store (§10) | secret values in env files, git, images, or release manifests |
| Packaging | one image on `python:3.12-alpine`, one `/data` volume | multi-service compose, scratch binaries |
| Deploy | fleet-manager signed release (§12) | ssh+scp, systemd, k8s |
| Server HTML | f-string render helpers | Jinja2, template engines |

- Runtime pip deps: **0**. Dev-only `pyright` in CI allowed. A new dependency is an ADR, not an import.

## 2. Repository layout

```
ops/
├── CLAUDE.md                  # §15, verbatim
├── Dockerfile                 # FROM python:3.12-alpine · COPY ops · VOLUME /data · CMD python -m ops.main
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
- Handlers are thin: parse → auth → call db/module → respond. Logic in a handler is a review reject.
- Security headers on every response: HSTS, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, CSP `default-src 'self'`.
- Access log: one JSON line per request via `logging`.
- Static files: `Cache-Control: no-cache` (tiny, internal; no fingerprinting until measured).
- `/healthz` → 200 only if: DB opens, `PRAGMA quick_check` passes, schema version == binary's expected version. **The deploy health gate trusts this — it must be honest.**

## 4. Database

Rules (also the docstring of `db.py`):

- One shared `sqlite3` connection + one `threading.Lock`. Every mutation is a `Db` method; every method body runs in `with self._tx() as c:` (lock held, commit on clean exit). Handlers never write SQL. Never hold the lock across anything slow.
- Open with `PRAGMA journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON` (per-connection!), `busy_timeout=5000`. Every table `STRICT`.
- Money: integer cents, columns `*_cents`, never `amount`. Rates: basis points. One rounding function (banker's, per line), one place.
- Dates ISO-8601 `TEXT`; event timestamps unix-seconds `INTEGER`.
- `entity_id NOT NULL` on every fact table from `001`; `entity_id` means legal entity everywhere; attachments use `owner_type`/`owner_id`.
- `period` seeded FY24–FY35, month 1 = July. Global job-number sequence; per-entity invoice/PO sequences via `UPDATE … RETURNING` inside the issuing transaction.
- `claim_line_revision`: every money-bearing edit → (who, when, field, old, new). Snapshots are queries over history, so history includes amounts.
- Rollups are `CREATE VIEW`s in migrations. Read SQL may live as strings in modules; writes only in `Db`.
- One process on the file. Local disk only, never NFS/SMB. Ad-hoc queries hit a backup snapshot.

Migrations:

- Numbered forward-only `.sql`, applied in a transaction (SQLite DDL is transactional — failure leaves the file untouched), recorded in `schema_migrations`.
- Entrypoint order: `backup.snapshot()` → `migrate()` → serve. Migrate failure = non-zero exit = unhealthy = **agent auto-rolls back**.
- **N-1 rule:** because rollback is automatic, a migration must not break the previous release's code. Expand-and-contract; destructive contractions ship one release after the expand has been stable.

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
  - `h(tag, attrs, …children)` — element builder. **Interpolated data never goes through `innerHTML`** (XSS rule; children are nodes/strings).
  - `api(method, path, body?)` — the only `fetch` call in the codebase; attaches JSON headers, throws on non-2xx with the server's error message.
  - `fmt` — `money(cents)`, `date(iso)`, `num(n, dp)`.
- Design tokens (`tokens.css`, everything reads custom properties, no literals in components):
  - Graphite/ISA-101-style palette: near-black surfaces in light-grey text, ONE muted-amber accent; saturated colour reserved for exceptions (errors, negative variance).
  - Three type layers: `--font-display`, `--font-ui` (one system-grotesk sans), `--font-data` (one mono). **All money/quantity cells render in `--font-data`** with `font-variant-numeric: tabular-nums`.
  - Flat, square, hairline: 0 radius, 1 px borders, no shadows. Control height and spacing on a single scale variable.
- `datatable.js` — one generic component seeding every read-only list view. Contract:
  - input: a model `{columns:[{key,label,align,fmt}], rows, filters?, searchKeys?, pageSize?}`
  - behaviour: client-side column sort (click header, toggle asc/desc), per-column select filters, substring search, paging with row-count footer
  - controls build once and keep DOM identity across re-renders (focus/open state survives); rows re-render from the model.
- Interactive screens (invoicing grid): server-rendered `<table>`; click cell → input; Enter/Tab commits `PATCH /api/…`; server responds with recomputed row/totals JSON; JS patches the DOM. **No optimistic UI — the server's response is the truth painted back.**
- Server-rendered report/dashboard pages come from `render.py` (a query + a loop); drill-through is plain `<a href>`.
- Guardrails (`tests/js_guardrails.py`, pure-Python static checks, CI-gated):
  - per-page JS byte budget (< 50 KB uncompressed)
  - no external URL / CDN import anywhere in `static/`
  - `fetch(` appears only inside `api()` in `app.js`
  - no `innerHTML` assignment outside `h()`'s implementation

## 8. OIDC (login)

Hand-rolled authorization-code flow, confidential client, ~200 lines using `urllib.request`:

1. `GET /login` → redirect to Google's auth endpoint with `client_id`, `redirect_uri`, `scope=openid email`, fresh single-use `state`.
2. Callback: verify `state` (single-use, then burned) → POST code + client secret to Google's token endpoint over TLS.
3. Parse the ID token **payload only** (base64 JSON). Signature verification is deliberately out of scope: the token is accepted *only* from our own token-endpoint response over TLS — never from the browser or any other path. Enforce that in code and review.
4. Require `aud == client_id` and `hd == commsecurity.com.au` (blocks arbitrary Gmail accounts). Look up/create the user row → mint session token (§9).

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

## 11. Documents

- `documents.py`: store file at `/data/documents/<aa>/<sha256>` (content-addressed, dedup free); metadata row `(owner_type, owner_id, filename, content_type, size, sha256, uploaded_by, ts)`.
- Served only through an authorised handler; never a direct static path.
- Soft-delete the metadata row; blobs are immutable (GC is a later, measured decision).

## 12. State & backup

- `/data` is **all** state; back up one volume, restore on any docker host.
- `ops backup`: `VACUUM INTO /data/backups/ops-<utc>.db` (atomic, consistent, sub-second) + prune retention set. Hourly via busybox crond in-container → RPO 1 h. Host job rsyncs `/data` off-box.
- Nightly `PRAGMA integrity_check`, logged loudly.
- Restore: place snapshot at `/data/ops.db`, start container; documents before DB. Monthly rehearsal, documented.
- Entrypoint snapshots before migrating (§4) so every rollback has a matching pre-migration file.

## 13. Build & deploy

CI (GitHub Actions, push to `main`; `v*` tags → versions):

1. `python3 -W error::ResourceWarning -m unittest discover -s tests -v`
2. Gates: JS guardrails · no-secret-values grep · "0 pip deps in image" inspection
3. `docker build` (retry ×3 on transient base-image pulls — red CI must mean *our* code broke) → tag `ghcr.io/commsecurityau/cs-ops:latest` + `:<sha7>`
4. Size gate: image **< 120 MB** hard fail (expect ~60)
5. Push to ghcr (`packages: write`)

Deploy — via the company fleet manager (the internal VM is an enrolled device). Its contract, which this app must satisfy:

- A release is a compose file; every image ref on **its own `image:` line** (the manager's line-based pinner rewrites `repo:tag` → `@sha256:…` digest at release creation; a release means exactly those bytes forever).
- Release env carries **non-secret config + `secret://` refs only** — manifests are signed, persisted and shipped, so a value there would live in three new places.
- Named volume `ops-data:/data`. Staging dirs are wiped on supersede; volumes persist.
- The device agent verifies the signed manifest, pulls over the tunnel, stages, then **health-gates on `/healthz`**; on failure it rolls back locally — no network, no operator. (Hence §4’s N-1 rule and §10’s fail-loud boot.)
- First deploy per host: `docker exec ops python -m ops.secrets set OIDC_CLIENT_SECRET` once; later releases find it on the volume.

Dev loop:

- `make dev` = `OPS_TLS=off OPS_DATA=./data python3 -m ops.main` with seeded DB. Clone → running < 1 min.
- `make test` < 10 s, no docker.

## 14. Budgets

| Budget | Limit | Enforcement |
|---|---|---|
| Image size | < 120 MB | CI hard fail |
| Runtime pip deps | 0 | CI hard fail |
| JS per page (uncompressed) | < 50 KB | CI hard fail |
| Secret values on file | 0 | CI grep, hard fail |
| Test suite wall time | < 10 s | CI hard fail |
| Cold start → serving | < 2 s | CI in-process timer |
| Container RSS steady | < 128 MB | trend, staging |
| Dashboard p95 @ 250 k-row fixture | < 150 ms | trend, staging |
| List/grid p95 | < 100 ms | trend, staging |
| Restore snapshot → serving | < 60 s | monthly rehearsal |
| New feature | ≤ +200 ms cold start · ≤ +8 MB RSS · ≤ +15 ms dashboard p95, else a recorded trade-off | review |

## 15. CLAUDE.md (repo root, verbatim)

```markdown
# CLAUDE.md — cs-ops

Internal financial operations platform. Read CS-OP-ARCH-002 first; the stack
is locked — implement, don't re-litigate.

## Stack
Python 3.12 stdlib ONLY. ThreadingHTTPServer + ssl. sqlite3, WAL, one
serialized connection (db.py owns all writes). Vanilla ES modules + fetch,
no framework, no build step. Hand-rolled OIDC (Google Workspace) + HMAC
identity-only session tokens. One docker image (python:3.12-alpine), one
/data volume, deployed by the fleet manager, health-gated on /healthz,
auto-rollback.

## Hard rules
- ZERO pip runtime deps. ZERO npm. New dependency = ADR, not an import.
- Money: integer cents, columns *_cents, never `amount`. Rates: basis
  points. One rounding function, one place.
- entity_id means legal entity everywhere; attachments use owner_type/owner_id.
- Handlers thin: parse, auth, call, respond. Writes only via Db methods.
  Reads may be SQL strings in modules. Rollups are views in migrations.
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
- SQLite: one process, local disk, never NFS. Pragmas set per-connection in
  db.py only.
- OIDC: state single-use; require aud == client_id and
  hd == commsecurity.com.au; ID tokens accepted ONLY from our own
  token-endpoint response over TLS. No other token path.
- Sessions: HMAC identity-only tokens {kind, sub, tv, exp}. Tokens NEVER
  carry permissions — roles re-resolve from DB every request. Revocation =
  bump users.token_version. Signing key auto-generated 0600 on the volume.
- Secrets: secret://NAME references ONLY in config/env/manifests/logs.
  Resolve once at startup; unresolved = loud boot failure; never log a
  value; provider selection explicit, never a fallback chain. No secret
  value in git, env files, the image, or a release manifest.
- Frontend: DOM via h(), never innerHTML with data. fetch() only inside
  app.js api(). All colour/type via tokens.css custom properties; money in
  --font-data with tabular-nums. Guardrails suite enforces all of this.
- Tests: stdlib unittest, fresh temp DB through REAL migrations, no docker,
  suite < 10 s, run with -W error::ResourceWarning.
- Budgets are CI gates: image < 120 MB, JS < 50 KB/page, 0 pip deps, 0
  secret values, cold start < 2 s. A regression is a failing build.
- No wrapper layers, no DTO↔DTO mapping, no speculative abstraction. Each
  new layer is justified in the PR description.
```

## 16. Decision log

- **ADR-08 — Python stdlib, not Go.** Container deployment is mandated (kills Go's static-binary edge); team fluency is Python/JS; stdlib covers HTTP+TLS+SQLite+outbound → **zero** runtime deps vs Go's two. Accepted: no compile-time types (hints + optional pyright), ~300 ms cold start, GIL (irrelevant at ~1 write/s).
- **ADR-09 — No htmx.** It's declarative fetch+swap for teams avoiding JS; this team writes JS, and the grid needs bespoke JS regardless. Identical performance, one less dependency.
- **ADR-10 — Deploy via the fleet manager.** Signed digest-pinned releases, health-gate, local auto-rollback. Consequences: honest `/healthz`, N-1 migrations, fail-loud secrets.
- **ADR-11 — In-process TLS from the internal CA.** stdlib ssl; root distributed via Workspace device management. Revisit only on a public-trust requirement.
- **ADR-12 — Secrets as `secret://` references; local store, not a secrets service.** Values in one 0600 volume store, set via stdin; remote provider dormant behind a paired env switch (no fallback chain). A secrets service was rejected on the bootstrap regress: its own master key and this app's bearer token would need delivery via env or the release manifest — exactly the banned locations — so the service *increases* secrets-at-rest (1 → ≥2) while adding a container in the boot path. Flip conditions (both explicit, neither near): a company-central secrets service with a bootstrap channel that isn't the signed manifest, or ≥3 operator-entered secrets across hosts. STP-6 note: Xero **refresh tokens rotate on use — they are mutable app state, stored AES-encrypted in the DB** (key file auto-generated 0600 on the volume, session-key pattern), not in any secrets store; only the Xero *client* secret is an `ops.secrets set` value.
- **ADR-13 — HMAC identity-only sessions.** No session table/GC; instant revocation via `token_version`; roles re-resolved per request so edits apply live. Rejected: server-side session rows, permission-bearing tokens, passwords.
- **ADR-14 — Design system & datatable specified in §7, reactive kernel rejected.** Token-driven CSS, one accent, mono data layer, generic datatable, mechanically-enforced guardrails. Rejected: any reactive/subscription kernel, component registry, WebSocket streaming, multi-theme system — this is request/response CRUD. Rejected: capability×scope×time grant algebra (4 roles × 3 entities doesn't need it; a path-prefix scope predicate is the pattern if per-project scoping ever appears).
- Carried from ARCH-001 + review: data model §5 (with `claim_line_revision`, `owner_type`/`owner_id`, roles enumerated, intercompany flags deferred until Q10 answered), phases §11 (+ grid prototype in STP-0; answer historical-scope Q3 before the STP-1 importer; batch bulk imports), risks §12, open questions §13.

## 17. STP-0 exit criteria

- Staff sign-in via Workspace SSO at `https://ops.commsecurity.com.au` (internal CA) → empty project list rendered through the module system.
- CI green with every §14 gate live.
- One release deployed end-to-end through the fleet manager, **including a forced health-gate failure proving auto-rollback**.
- Boot without `OIDC_CLIENT_SECRET` observed to fail loudly and roll back; secret then set via `ops.secrets set` from stdin.
- A `token_version` bump observed to kill a live session.
- One backup snapshot restored and served.
- Invoicing-grid interaction prototype working against seeded data, styled from `tokens.css`.

*End — hand this file back with "implement STP-0" to begin.*
