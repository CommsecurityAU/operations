-- 008_claim_plan.sql — how a contract becomes a forecast (STP-2).
--
-- EXPAND ONLY.
--
-- Until now a claim line was the atom and there was no way to make one:
-- every claim in the platform arrived from the importer, so a project
-- created here could not have a dollar of its invoicing planned. The
-- progress-claim workbooks did that work, and this is the layer that
-- replaces them.
--
-- The shape, from `720 Bourke St` and `25-35 River Boulevard`:
--
--   A contract splits into ITEMS with values.
--   Each item is spread across months by PERCENTAGE.
--   A month's claim is the SUM of that month's contributions.
--
--     Equipment                $185,000    Jun-27 60%, Aug-27 40%
--     Project Management        $33,500    10% a month, fourteen times
--     Design / Engineering      $30,050    Aug-26 50%, Dec-26 25%, Jan-27 25%
--
--     Dec-26 = PM 10% $3,350 + Design 25% $7,512.50 = $10,862.50
--
-- `720 Bourke` looks different only because its percentages fall one per
-- month, so each contribution became its own row. Same structure.
--
-- Three properties the workbooks check by hand and this can check
-- continuously: items sum to the contract, each item's allocations reach
-- 100%, and the cumulative claim lands on exactly 100%.
--
-- AT MOST one claim per project per month. A month with no allocations
-- produces nothing -- `Sep-26`, `Feb-27` and `Oct-27` are all $0.00 on
-- River Boulevard, and the cumulative percentage simply holds flat.

CREATE TABLE claim_item (
    id            INTEGER PRIMARY KEY,
    entity_id     INTEGER NOT NULL REFERENCES entity(id),
    project_id    INTEGER NOT NULL REFERENCES project(id),
    name          TEXT    NOT NULL,
    value_cents   INTEGER NOT NULL,
    -- Variations sit outside the contract total: the workbooks keep them in
    -- a separate block for exactly that reason.
    is_variation  INTEGER NOT NULL DEFAULT 0 CHECK (is_variation IN (0,1)),
    sequence      INTEGER NOT NULL DEFAULT 0,
    note          TEXT,
    created_by    INTEGER REFERENCES users(id),
    created_ts    INTEGER NOT NULL,
    CHECK (value_cents >= 0),
    CHECK (length(trim(name)) > 0)
) STRICT;

CREATE INDEX claim_item_project ON claim_item (project_id);

-- One item's share of one month. The AMOUNT is the fact and the percentage
-- is how it was expressed: `33.33%` of $79,444 is $26,478.69, but the
-- agreed figure was $26,481.33 -- a third, displayed rounded. Deriving the
-- amount from the percentage at read time would move money.
CREATE TABLE claim_allocation (
    id             INTEGER PRIMARY KEY,
    claim_item_id  INTEGER NOT NULL REFERENCES claim_item(id),
    period_id      INTEGER NOT NULL REFERENCES period(id),
    percent_bp     INTEGER NOT NULL,
    amount_cents   INTEGER NOT NULL,
    note           TEXT,
    -- Set when the claim it fed has been invoiced. Fixed from then on: the
    -- figure has left the building. Amendable only through the deliberate
    -- path, which records what the invoice actually said.
    locked_claim_id INTEGER REFERENCES claim_line(id),
    created_by     INTEGER REFERENCES users(id),
    created_ts     INTEGER NOT NULL,
    CHECK (amount_cents >= 0),
    -- One row per item per month: two shares of the same item in the same
    -- month is one share, and keeping them apart invites them to disagree.
    UNIQUE (claim_item_id, period_id)
) STRICT;

CREATE INDEX claim_allocation_period ON claim_allocation (period_id);

-- What an amendment to an already-invoiced claim changed, and what the
-- invoice actually said. Reconciling to Xero later means matching against
-- the figure that was issued, not the one it was corrected to.
CREATE TABLE claim_amendment (
    id             INTEGER PRIMARY KEY,
    claim_line_id  INTEGER NOT NULL REFERENCES claim_line(id),
    invoice_number TEXT,
    invoiced_cents INTEGER NOT NULL,
    amended_cents  INTEGER NOT NULL,
    reason         TEXT    NOT NULL,
    amended_by     INTEGER REFERENCES users(id),
    amended_ts     INTEGER NOT NULL,
    CHECK (length(trim(reason)) > 0)
) STRICT;

CREATE INDEX claim_amendment_line ON claim_amendment (claim_line_id);

-- Which plan produced a claim. NULL for everything imported from the
-- workbook, which had no plan behind it in this system.
ALTER TABLE claim_line ADD COLUMN from_plan INTEGER NOT NULL DEFAULT 0
    CHECK (from_plan IN (0,1));

-- ------------------------------------------------------------------ views
-- Does the plan add up? The three questions the workbooks answer by hand.
CREATE VIEW v_claim_item_coverage AS
SELECT
    i.id                                    AS claim_item_id,
    i.project_id                            AS project_id,
    i.entity_id                             AS entity_id,
    i.name                                  AS name,
    i.value_cents                           AS value_cents,
    i.is_variation                          AS is_variation,
    i.sequence                              AS sequence,
    COALESCE((SELECT SUM(a.amount_cents) FROM claim_allocation a
              WHERE a.claim_item_id = i.id), 0)      AS allocated_cents,
    i.value_cents
    - COALESCE((SELECT SUM(a.amount_cents) FROM claim_allocation a
                WHERE a.claim_item_id = i.id), 0)    AS unallocated_cents,
    COALESCE((SELECT SUM(a.percent_bp) FROM claim_allocation a
              WHERE a.claim_item_id = i.id), 0)      AS allocated_bp,
    COALESCE((SELECT COUNT(*) FROM claim_allocation a
              WHERE a.claim_item_id = i.id), 0)      AS allocation_count,
    COALESCE((SELECT SUM(a.amount_cents) FROM claim_allocation a
              WHERE a.claim_item_id = i.id
                AND a.locked_claim_id IS NOT NULL), 0) AS locked_cents
FROM claim_item i;

CREATE VIEW v_project_claim_plan AS
SELECT
    p.id                                    AS project_id,
    p.entity_id                             AS entity_id,
    p.contract_value_cents                  AS contract_value_cents,
    COALESCE((SELECT SUM(c.value_cents) FROM v_claim_item_coverage c
              WHERE c.project_id = p.id AND c.is_variation = 0), 0)
                                            AS item_value_cents,
    COALESCE((SELECT SUM(c.value_cents) FROM v_claim_item_coverage c
              WHERE c.project_id = p.id AND c.is_variation = 1), 0)
                                            AS variation_value_cents,
    COALESCE((SELECT SUM(c.allocated_cents) FROM v_claim_item_coverage c
              WHERE c.project_id = p.id), 0)
                                            AS allocated_cents,
    -- The gap the plan has to close: items that do not sum to the contract
    -- mean work either unplanned or over-committed.
    p.contract_value_cents
    - COALESCE((SELECT SUM(c.value_cents) FROM v_claim_item_coverage c
                WHERE c.project_id = p.id AND c.is_variation = 0), 0)
                                            AS unitemised_cents
FROM project p;

-- A month's planned claim: the sum of that month's contributions.
CREATE VIEW v_planned_month AS
SELECT
    i.project_id                            AS project_id,
    i.entity_id                             AS entity_id,
    a.period_id                             AS period_id,
    SUM(a.amount_cents)                     AS amount_cents,
    COUNT(*)                                AS contribution_count,
    SUM(CASE WHEN a.locked_claim_id IS NOT NULL THEN 1 ELSE 0 END)
                                            AS locked_count
FROM claim_allocation a
JOIN claim_item i ON i.id = a.claim_item_id
GROUP BY i.project_id, i.entity_id, a.period_id;
