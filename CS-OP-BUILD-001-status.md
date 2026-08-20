# CS-OP-BUILD-001 — Build status

- **As at:** 20 August 2026
- **Repo:** `C:\Dev\operations` → `git@github-roberts:CommsecurityAU/operations.git`
- **Spec:** CS-OP-ARCH-002 (locked; changes require an ADR in §16)
- **Phase:** STP-0 Foundation, in progress

---

## Where things stand

| Piece | State |
|---|---|
| `ops/migrations/001_foundation.sql` | Done — entities, 144 periods, identity, project register, job-code worklist, job-number sequence |
| `ops/db.py` | Done — connection split, pragmas, migration runner, health check, first write methods |
| `tools/import_register.py` | Done — validates and imports the FY27 register, one-shot |
| `tests/` | 48 tests, ~0.25 s on Linux, ~1 s on Windows |
| `ops/secrets.py` | **Next** |
| `ops/auth.py`, `ops/http_util.py`, `ops/main.py` | Not started |
| `ops/static/`, `ops/modules/` | Not started |
| `Dockerfile`, `Makefile`, `.github/workflows/ci.yml` | Not started |

Run the suite:

```
py -W error::ResourceWarning -m unittest discover -s tests     # Windows
python3 -W error::ResourceWarning -m unittest discover -s tests # container / CI
```

---

## Source data — validated, reconciles to zero

The FY27 register was cleaned at source during the 20 Aug review. Final state:

| | |
|---|---:|
| Projects | 59 |
| Purchase Order | $7,231,907.00 |
| Invoiced Prior (29 opening rows) | $3,711,865.27 |
| **Orders in Hand, FY27 start** | **$3,520,041.73** |
| Residual | $0.00 |

`Purchase Order == Invoiced Prior + Contract Value FY27` holds on every row.
The importer **asserts** this rather than deriving it; one bad row aborts the
whole import before anything is written.

Pinned in `tests/test_import_register.py` as cents: `723190700`,
`371186527`, `352004173`. A source change that moves these fails the build
and requires editing the pins deliberately.

### What was fixed at source

- `JN-676` and `JN-5416` were genuine collisions — one code across unrelated
  sites, so their financial history was merged. Brennan Pl reissued to
  `JN-6694`; 88 Robertson St set to `TBA`. **Zero collisions remain.**
- `Invoiced FY26` renamed to `Invoiced Prior` and merged with pre-FY26
  billing. This was the important one: five DLP projects had FY25 billing the
  old column never reached, so sourcing opening balances from it would have
  understated them by **$858,354** and shown that much in phantom orders in
  hand.
- Two phantom `$22,689` rows deleted (copy-paste of 36 Wellington's value),
  `200 Vic` PO corrected to $400, two PDNSW double-counts cleared,
  `JN 5108` → `JN-5108`, `Adhoc Service Calls` removed.

### Worklist carried into the platform — 8 rows, no merged history

- **Class B, 6:** `TBA` ×5 (PDNSW SOC, 88 Robertson St, Dover House,
  130 Little Collins, Maitland storage cage), `na` ×1 (CommSecurity Office –
  KODE OS). Each needs a job number issued or a not-project-work decision.
  Issuance moves to this platform, so most self-resolve.
- **Class C, 2 codes:** `JN-4335`, `JN-4407`. **Not defects** — one customer
  job number covering a site tracked as two projects by work type. This is
  why `job_code_alias` is one-to-many.
- **Leave alone:** `P-3655`, `P-3707`, `JN-CommS` are valid codes that fail a
  `JN-\d+` pattern. A clever normaliser corrupts them.

Resolution gates **STP-5** (the dashboard), not STP-1.

---

## Open items

1. **Register the OIDC client.** Cloud project inside the Workspace org,
   consent screen **Internal**, redirect URIs
   `https://ops.commsecurity.com.au/auth/callback` and
   `http://localhost:8080/auth/callback`. Blocks STP-0's first exit criterion
   and nothing earlier. *Only outstanding action; all decisions are made.*
2. **Confirm the corporate tax rate with the accountant.** 25% (2500 bp) is
   recorded as an estimate. The 25%/30% split in the source may be a real
   difference between entities rather than an error — eligibility is assessed
   annually and per legal entity.
3. **Before STP-1:** recompute the three pinned FY27 totals under both
   rounding modes and record any divergence (ADR-15).
4. **Minor, source data:** 50 Queens Rd shows *Live, 50%* on the Project tab
   and *DLP* on the register. One is stale.

---

## Things that cost time — don't rediscover them

- **`sqlite3.executescript()` does not roll back on failure.** It leaves the
  transaction open and completed statements in place. The runner's explicit
  `rollback()` is why a failed migration doesn't leave a half-applied schema.
- **It also commits any pending transaction first**, so the runner must wrap
  the script text in `BEGIN`/`COMMIT` — a `BEGIN` issued beforehand is
  discarded.
- **Concurrency tests built from single SQL statements are theatre.** The
  first pair here passed with the write lock deleted, because SQLite's own
  mutex makes single statements atomic. Mutation-test every safeguard: remove
  it, confirm a test fails.
- **Run the suite on Windows periodically.** It caught a read-connection leak
  that Linux hides — open files unlink fine there, so CI would have shipped
  it. Keep test teardown strict; that failure is the leak detector.
- **SQLite has no `%y` in `strftime`**, only `%Y`.
- On Windows use `py`, not `python3`. Inside the container and in CI,
  `python3` is correct — don't change it there.

---

## Resume point

`ops/secrets.py` — `secret://` resolver, 0600 local store written from stdin
only, `list` printing names only, explicit provider selection with no
fallback chain, and fail-loud boot so a missing `OIDC_CLIENT_SECRET` reaches
the health gate instead of starting with a blank credential.

Note for that build: `0600` is POSIX and does nothing on Windows. The store
works for local dev either way, but the permission guarantee exists only in
the container — the code should assert the mode where it can and say so
where it can't, rather than silently skipping the check.
