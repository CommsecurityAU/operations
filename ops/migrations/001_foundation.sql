-- 001_foundation.sql — entities, periods, identity, project register.
-- Forward-only. Applied in one transaction by the migration runner (CS-OP-ARCH-002 §4).
-- Every table STRICT. Money is integer cents in *_cents columns, never `amount`.
-- Dates are ISO-8601 TEXT. Event timestamps are unix seconds INTEGER.

-- ---------------------------------------------------------------- entities
CREATE TABLE entity (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    abn         TEXT,
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1))
) STRICT;

INSERT INTO entity (id, code, name, abn) VALUES
    (1, 'CSSB',  'CommSecurity Smart Buildings Pty Ltd', '19 677 520 339'),
    (2, 'CSPL',  'CommSecurity Pty Ltd',                 '42 636 706 146'),
    (3, 'RAVEN', 'RAVEN BOX',                            NULL);

-- ----------------------------------------------------------------- periods
-- FY label is the calendar year the financial year ENDS in: FY27 = Jul 2026 - Jun 2027.
-- Month 1 = July. Seeded FY24..FY35 inclusive (144 months).
CREATE TABLE period (
    id           INTEGER PRIMARY KEY,
    fy           INTEGER NOT NULL,
    fy_label     TEXT    NOT NULL,
    month_no     INTEGER NOT NULL CHECK (month_no BETWEEN 1 AND 12),
    month_start  TEXT    NOT NULL UNIQUE,
    month_end    TEXT    NOT NULL,
    label        TEXT    NOT NULL,
    UNIQUE (fy, month_no)
) STRICT;

INSERT INTO period (fy, fy_label, month_no, month_start, month_end, label)
WITH RECURSIVE n(i) AS (
    SELECT 0 UNION ALL SELECT i + 1 FROM n WHERE i < 143
)
SELECT
    2024 + (i / 12),
    'FY' || substr(CAST(2024 + (i / 12) AS TEXT), 3, 2),
    (i % 12) + 1,
    date('2023-07-01', '+' || i || ' months'),
    date('2023-07-01', '+' || (i + 1) || ' months', '-1 day'),
    CASE (i % 12)
        WHEN 0 THEN 'Jul' WHEN 1 THEN 'Aug' WHEN 2  THEN 'Sep' WHEN 3  THEN 'Oct'
        WHEN 4 THEN 'Nov' WHEN 5 THEN 'Dec' WHEN 6  THEN 'Jan' WHEN 7  THEN 'Feb'
        WHEN 8 THEN 'Mar' WHEN 9 THEN 'Apr' WHEN 10 THEN 'May' ELSE 'Jun'
    END || '-' || substr(strftime('%Y', date('2023-07-01', '+' || i || ' months')), 3, 2)
FROM n;

-- ---------------------------------------------------------------- identity
-- Keyed on the OIDC `sub`, never email: Workspace addresses get reassigned,
-- aliased and renamed, and an email-keyed row hands a departed employee's
-- grants to their replacement (ADR-18).
CREATE TABLE users (
    id             INTEGER PRIMARY KEY,
    oidc_sub       TEXT    NOT NULL UNIQUE,
    email          TEXT    NOT NULL,
    display_name   TEXT    NOT NULL,
    token_version  INTEGER NOT NULL DEFAULT 1,
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_ts     INTEGER NOT NULL,
    last_seen_ts   INTEGER
) STRICT;

-- Roles are enumerated and NO ROLE IMPLIES ANOTHER (§9). A user with no row
-- here sees nothing: first sign-in provisions `viewer` on ZERO entities.
CREATE TABLE user_entity_role (
    user_id     INTEGER NOT NULL REFERENCES users(id),
    entity_id   INTEGER NOT NULL REFERENCES entity(id),
    role        TEXT    NOT NULL CHECK (role IN ('viewer','operations','approver','admin')),
    granted_by  INTEGER REFERENCES users(id),
    granted_ts  INTEGER NOT NULL,
    PRIMARY KEY (user_id, entity_id, role)
) STRICT;

-- ---------------------------------------------------------------- audit log
-- Append-only in the SCHEMA, not by convention (§4).
CREATE TABLE audit_log (
    id             INTEGER PRIMARY KEY,
    ts             INTEGER NOT NULL,
    actor_user_id  INTEGER REFERENCES users(id),
    action         TEXT    NOT NULL,
    target_type    TEXT    NOT NULL,
    target_id      TEXT,
    detail         TEXT
) STRICT;

CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

CREATE INDEX audit_log_ts ON audit_log (ts);

-- ----------------------------------------------------------------- clients
CREATE TABLE client (
    id          INTEGER PRIMARY KEY,
    entity_id   INTEGER NOT NULL REFERENCES entity(id),
    name        TEXT    NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    UNIQUE (entity_id, name)
) STRICT;

-- ------------------------------------------------------------ project type
-- The live taxonomy. `Security` and `Service` here are WORK CATEGORIES; the
-- legal entity is entity_id and the two must never be conflated.
CREATE TABLE project_type (
    id      INTEGER PRIMARY KEY,
    code    TEXT NOT NULL UNIQUE,
    name    TEXT NOT NULL
) STRICT;

INSERT INTO project_type (code, name) VALUES
    ('ICN','ICN'), ('IBP','IBP'), ('EMS','EMS'), ('NSW','NSW'),
    ('Consulting','Consulting'), ('Service','Service'), ('Security','Security'),
    ('Q-Access','Q-Access'), ('R&D','R&D');

-- ---------------------------------------------------------------- projects
-- purchase_order_cents and invoiced_prior_cents are MIGRATION INPUTS captured
-- from the validated FY27 register. Migration 002 expands them into
-- customer_po and a synthetic opening claim_line, then contracts these two
-- columns away (expand-and-contract, §4). They are not the long-term model.
CREATE TABLE project (
    id                    INTEGER PRIMARY KEY,
    entity_id             INTEGER NOT NULL REFERENCES entity(id),
    name                  TEXT    NOT NULL,
    job_code              TEXT    NOT NULL,
    project_no            TEXT,
    client_id             INTEGER REFERENCES client(id),
    type_id               INTEGER REFERENCES project_type(id),
    status                TEXT    NOT NULL CHECK (status IN ('Active','DLP','SLA','Complete')),
    project_lead          TEXT,
    purchase_order_cents  INTEGER NOT NULL DEFAULT 0,
    invoiced_prior_cents  INTEGER NOT NULL DEFAULT 0,
    needs_resolution      INTEGER NOT NULL DEFAULT 0 CHECK (needs_resolution IN (0,1)),
    notes                 TEXT,
    source_row            INTEGER,
    created_ts            INTEGER NOT NULL,
    CHECK (purchase_order_cents >= 0),
    CHECK (invoiced_prior_cents >= 0),
    -- The register's own assertion, enforced per row: a project can never have
    -- been invoiced more than its contract value (ADR-22).
    CHECK (invoiced_prior_cents <= purchase_order_cents),
    UNIQUE (entity_id, name)
) STRICT;

CREATE INDEX project_job_code ON project (job_code);
CREATE INDEX project_status   ON project (status);

-- ------------------------------------------------------- job code handling
-- ONE-TO-MANY BY DESIGN. One customer job number legitimately covers a site
-- that this platform tracks as several projects by work type (JN-4335,
-- JN-4407). A unique constraint on legacy_code would make those projects
-- fight over their own history (ADR-23).
CREATE TABLE job_code_alias (
    id           INTEGER PRIMARY KEY,
    legacy_code  TEXT    NOT NULL,
    project_id   INTEGER NOT NULL REFERENCES project(id),
    note         TEXT,
    created_ts   INTEGER NOT NULL,
    UNIQUE (legacy_code, project_id)
) STRICT;

CREATE INDEX job_code_alias_legacy ON job_code_alias (legacy_code);

-- Worklist. Ambiguous codes import FLAGGED, never blocked (ADR-23).
-- `reason` is mandatory for class C at resolution time: with a sole
-- resolution authority there is no second reader, so the written rationale
-- is the only check that exists.
CREATE TABLE job_code_issue (
    id            INTEGER PRIMARY KEY,
    raw_code      TEXT    NOT NULL,
    class         TEXT    NOT NULL CHECK (class IN ('A','B','C')),
    project_id    INTEGER REFERENCES project(id),
    status        TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved','dismissed')),
    resolved_by   INTEGER REFERENCES users(id),
    resolved_at   INTEGER,
    reason        TEXT,
    created_ts    INTEGER NOT NULL,
    CHECK (status = 'open' OR resolved_by IS NOT NULL),
    CHECK (class <> 'C' OR status = 'open' OR (reason IS NOT NULL AND length(trim(reason)) > 0))
) STRICT;

CREATE INDEX job_code_issue_status ON job_code_issue (status);

-- ------------------------------------------------------ job number issuance
-- Global sequence (§4). Issued via UPDATE ... RETURNING inside the caller's
-- transaction. Seeded above the highest legacy JN- code in the register.
CREATE TABLE job_number_sequence (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    next_value  INTEGER NOT NULL
) STRICT;

INSERT INTO job_number_sequence (id, next_value) VALUES (1, 7000);

-- ------------------------------------------------------------------- views
-- Orders in hand, interim. NO FINANCIAL YEAR APPEARS IN THIS FORMULA (§4).
-- Migration 002 replaces this with sum(customer_po) - sum(claims up to X)
-- once claim_line exists; the definition stays year-free either way.
CREATE VIEW v_project_orders_in_hand AS
SELECT
    p.id                                              AS project_id,
    p.entity_id                                       AS entity_id,
    p.name                                            AS project_name,
    p.job_code                                        AS job_code,
    p.status                                          AS status,
    p.purchase_order_cents                            AS purchase_order_cents,
    p.invoiced_prior_cents                            AS invoiced_prior_cents,
    p.purchase_order_cents - p.invoiced_prior_cents   AS orders_in_hand_cents
FROM project p;
