-- 003_invoicing.sql — customer POs and claim lines (STP-2).
--
-- Replaces the Invoicing and Future Invoicing tabs with ONE fact table
-- carrying a status lifecycle. The monthly copy-forward ritual disappears
-- because a forecast row does not move between tabs -- it changes status.
--
-- EXPAND ONLY. project.purchase_order_cents and project.invoiced_prior_cents
-- stay, populated, and become read-only from this release: the app writes
-- customer_po and claim_line instead. They are contracted in a later
-- migration, one release after this one has been stable (§4). Rollback is
-- automatic and nothing rolls the schema back, so the previous release must
-- still find the columns it reads.

-- ------------------------------------------------------------ customer POs
-- A PO is a fact, not a state machine. Genuine new scope is a NEW row;
-- a correction is a customer_po_revision. That is what stops an adjustment
-- retrospectively moving every orders-in-hand figure ever derived from it.
CREATE TABLE customer_po (
    id            INTEGER PRIMARY KEY,
    entity_id     INTEGER NOT NULL REFERENCES entity(id),
    project_id    INTEGER NOT NULL REFERENCES project(id),
    po_number     TEXT,
    amount_cents  INTEGER NOT NULL,
    issued_date   TEXT,
    note          TEXT,
    created_by    INTEGER REFERENCES users(id),
    created_ts    INTEGER NOT NULL,
    CHECK (amount_cents >= 0)
) STRICT;

CREATE INDEX customer_po_project ON customer_po (project_id);

CREATE TABLE customer_po_revision (
    id             INTEGER PRIMARY KEY,
    customer_po_id INTEGER NOT NULL REFERENCES customer_po(id),
    field          TEXT    NOT NULL,
    old_value      TEXT,
    new_value      TEXT,
    reason         TEXT    NOT NULL,
    changed_by     INTEGER REFERENCES users(id),
    changed_ts     INTEGER NOT NULL
) STRICT;

CREATE INDEX customer_po_revision_po ON customer_po_revision (customer_po_id);

-- ------------------------------------------------------------ claim lines
-- ONE table, replacing both tabs. The status carries what the tab used to:
--
--   forecast   planned, no date agreed          (was: Future Invoicing)
--   due        this period, ready to claim      (was: Invoicing)
--   approved   customer has approved it
--   invoiced   an invoice number exists
--   paid       payment receipted
--   cancelled  will not be billed
--
-- Money is the fact; percent_bp is how the claim was EXPRESSED and is kept
-- for provenance only. Deriving the amount from a percentage at read time
-- would make every historical figure move when a contract value is
-- corrected.
CREATE TABLE claim_line (
    id                  INTEGER PRIMARY KEY,
    entity_id           INTEGER NOT NULL REFERENCES entity(id),
    project_id          INTEGER NOT NULL REFERENCES project(id),
    customer_po_id      INTEGER REFERENCES customer_po(id),
    period_id           INTEGER REFERENCES period(id),
    status              TEXT    NOT NULL CHECK (status IN
                            ('forecast','due','approved','invoiced','paid','cancelled')),
    amount_cents        INTEGER NOT NULL,
    percent_bp          INTEGER,
    phase               TEXT,
    task                TEXT,
    detail              TEXT,
    reference           TEXT,
    claim_date          TEXT,
    approved_date       TEXT,
    invoice_number      TEXT,
    invoiced_date       TEXT,
    paid_date           TEXT,
    is_opening_balance  INTEGER NOT NULL DEFAULT 0
                            CHECK (is_opening_balance IN (0,1)),
    created_by          INTEGER REFERENCES users(id),
    created_ts          INTEGER NOT NULL,

    -- Exactly one kind of claim line may float free of a PO, and it is the
    -- one representing history this platform did not witness (ADR-22).
    CHECK (customer_po_id IS NOT NULL OR is_opening_balance = 1),

    -- An opening balance is a migration artifact: it is invoiced by
    -- definition, and it carries no invoice number because none was ours.
    CHECK (is_opening_balance = 0 OR status = 'invoiced'),
    CHECK (is_opening_balance = 0 OR invoice_number IS NULL)
) STRICT;

CREATE INDEX claim_line_project ON claim_line (project_id);
CREATE INDEX claim_line_status  ON claim_line (status);
CREATE INDEX claim_line_period  ON claim_line (period_id);

-- Every money-bearing edit. Snapshots are queries over history, so history
-- has to include the amounts.
CREATE TABLE claim_line_revision (
    id             INTEGER PRIMARY KEY,
    claim_line_id  INTEGER NOT NULL REFERENCES claim_line(id),
    field          TEXT    NOT NULL,
    old_value      TEXT,
    new_value      TEXT,
    reason         TEXT,
    changed_by     INTEGER REFERENCES users(id),
    changed_ts     INTEGER NOT NULL
) STRICT;

CREATE INDEX claim_line_revision_line ON claim_line_revision (claim_line_id);

-- An opening balance is immutable: it is not a claim anyone made, it is the
-- boundary of what this platform knows. Enforced in the schema rather than
-- by convention.
CREATE TRIGGER claim_line_opening_no_update
BEFORE UPDATE ON claim_line
WHEN OLD.is_opening_balance = 1
BEGIN SELECT RAISE(ABORT, 'opening balance rows are immutable'); END;

CREATE TRIGGER claim_line_opening_no_delete
BEFORE DELETE ON claim_line
WHEN OLD.is_opening_balance = 1
BEGIN SELECT RAISE(ABORT, 'opening balance rows are immutable'); END;

-- ------------------------------------------------------- migrate the data
-- One PO per project, from the validated register. Where a project has
-- several real POs they are added later as further rows; this is the
-- opening position, not a claim about how many POs exist.
INSERT INTO customer_po
    (entity_id, project_id, po_number, amount_cents, note, created_ts)
SELECT entity_id, id, NULL, purchase_order_cents,
       'migrated from the FY27 register', strftime('%s','now')
FROM project
WHERE purchase_order_cents > 0;

-- The 29 opening rows (ADR-22): everything invoiced before the platform's
-- window, dated the last day of FY26.
INSERT INTO claim_line
    (entity_id, project_id, customer_po_id, period_id, status, amount_cents,
     detail, claim_date, invoiced_date, is_opening_balance, created_ts)
SELECT p.entity_id, p.id, NULL,
       (SELECT id FROM period WHERE month_start = '2026-06-01'),
       'invoiced', p.invoiced_prior_cents,
       'opening balance: invoiced before FY27', '2026-06-30', '2026-06-30',
       1, strftime('%s','now')
FROM project p
WHERE p.invoiced_prior_cents > 0;

-- ------------------------------------------------------------------ views
-- Orders in hand, from the new source. NO FINANCIAL YEAR APPEARS HERE:
-- `contract - claims up to X` answers FY27 opening, FY28 opening and today
-- with one definition. Anything shaped like "claims since <a date>" is the
-- workbook's July ritual reimplemented in SQL.
DROP VIEW v_project_orders_in_hand;

CREATE VIEW v_project_orders_in_hand AS
SELECT
    p.id        AS project_id,
    p.entity_id AS entity_id,
    p.name      AS project_name,
    p.job_code  AS job_code,
    p.status    AS status,
    COALESCE((SELECT SUM(po.amount_cents) FROM customer_po po
              WHERE po.project_id = p.id), 0)          AS purchase_order_cents,
    COALESCE((SELECT SUM(cl.amount_cents) FROM claim_line cl
              WHERE cl.project_id = p.id
                AND cl.status IN ('invoiced','paid')), 0) AS invoiced_prior_cents,
    COALESCE((SELECT SUM(po.amount_cents) FROM customer_po po
              WHERE po.project_id = p.id), 0)
    - COALESCE((SELECT SUM(cl.amount_cents) FROM claim_line cl
                WHERE cl.project_id = p.id
                  AND cl.status IN ('invoiced','paid')), 0)
                                                        AS orders_in_hand_cents
FROM project p;

-- Forecast position: what is expected but not yet invoiced.
CREATE VIEW v_project_pipeline AS
SELECT
    p.id        AS project_id,
    p.entity_id AS entity_id,
    p.name      AS project_name,
    COALESCE(SUM(CASE WHEN cl.status = 'forecast' THEN cl.amount_cents END), 0)
        AS forecast_cents,
    COALESCE(SUM(CASE WHEN cl.status IN ('due','approved') THEN cl.amount_cents END), 0)
        AS due_cents,
    COALESCE(SUM(CASE WHEN cl.status = 'invoiced' THEN cl.amount_cents END), 0)
        AS invoiced_cents,
    COALESCE(SUM(CASE WHEN cl.status = 'paid' THEN cl.amount_cents END), 0)
        AS paid_cents
FROM project p
LEFT JOIN claim_line cl ON cl.project_id = p.id
GROUP BY p.id, p.entity_id, p.name;
