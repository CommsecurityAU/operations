-- 010_allocation_claim.sql — an allocation owns exactly one claim (STP-2).
--
-- Generation assumed ONE claim per project per month. The register does not
-- work that way: `200 Victoria - IBP` has five Sep-26 claims -- Commissioning
-- 1/3, 2/3, All Systems, SAT, Client Training -- and two Aug-26 claims that
-- share `Inv No. 6072/5`.
--
-- That is the answer, and it was visible in the data all along: a CLAIM
-- LINE is one contribution, and an INVOICE groups them. `25-35 River
-- Boulevard`'s single monthly invoice is the same shape -- several
-- contributions, one invoice number.
--
-- Two consequences:
--
-- 1. Each allocation carries `claim_line_id`, the claim it produced.
--    Without it, generation could not tell which of five claims an
--    allocation had made: it updated one to the month's whole total and
--    left the other four standing. $88,500 of forecast became $159,300, in
--    silence.
--
-- 2. `UNIQUE (claim_item_id, period_id)` has to go. It said an item
--    contributes to a month at most once, which is false -- five tasks of
--    the Commissioning phase all fall in Sep-26. SQLite cannot drop an
--    inline constraint, so the table is rebuilt.
--
-- The rebuild copies every row before dropping the old table.

-- EVERY dependent view must go first, and they nest: SQLite validates all
-- views when a table is dropped, so one referring to a table mid-rebuild
-- fails the migration with an error naming the view rather than the cause.
-- `v_project_claim_plan` reads `v_claim_item_coverage`, which reads the
-- table, so all three come down and go back up together.
DROP VIEW v_project_claim_plan;
DROP VIEW v_planned_month;
DROP VIEW v_claim_item_coverage;

CREATE TABLE claim_allocation_new (
    id             INTEGER PRIMARY KEY,
    claim_item_id  INTEGER NOT NULL REFERENCES claim_item(id),
    period_id      INTEGER NOT NULL REFERENCES period(id),
    percent_bp     INTEGER NOT NULL,
    amount_cents   INTEGER NOT NULL,
    note           TEXT,
    -- The claim this allocation produced, or was adopted from.
    claim_line_id  INTEGER REFERENCES claim_line(id),
    -- Set once that claim has been invoiced. Fixed from then on.
    locked_claim_id INTEGER REFERENCES claim_line(id),
    created_by     INTEGER REFERENCES users(id),
    created_ts     INTEGER NOT NULL,
    CHECK (amount_cents >= 0)
) STRICT;

INSERT INTO claim_allocation_new
    (id, claim_item_id, period_id, percent_bp, amount_cents, note,
     claim_line_id, locked_claim_id, created_by, created_ts)
SELECT id, claim_item_id, period_id, percent_bp, amount_cents, note,
       locked_claim_id, locked_claim_id, created_by, created_ts
FROM claim_allocation;

DROP TABLE claim_allocation;
ALTER TABLE claim_allocation_new RENAME TO claim_allocation;

CREATE INDEX claim_allocation_period ON claim_allocation (period_id);
CREATE INDEX claim_allocation_item ON claim_allocation (claim_item_id);
CREATE INDEX claim_allocation_claim ON claim_allocation (claim_line_id);

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

-- Unchanged from migration 009; recreated because it sits above the view
-- that reads the rebuilt table.
CREATE VIEW v_project_claim_plan AS
SELECT
    p.id                                    AS project_id,
    p.entity_id                             AS entity_id,
    p.contract_value_cents                  AS contract_value_cents,
    COALESCE((SELECT SUM(cl.amount_cents) FROM claim_line cl
              WHERE cl.project_id = p.id AND cl.is_opening_balance = 1), 0)
                                            AS opening_balance_cents,
    p.contract_value_cents
    - COALESCE((SELECT SUM(cl.amount_cents) FROM claim_line cl
                WHERE cl.project_id = p.id AND cl.is_opening_balance = 1), 0)
                                            AS plannable_cents,
    COALESCE((SELECT SUM(c.value_cents) FROM v_claim_item_coverage c
              WHERE c.project_id = p.id AND c.is_variation = 0), 0)
                                            AS item_value_cents,
    COALESCE((SELECT SUM(c.value_cents) FROM v_claim_item_coverage c
              WHERE c.project_id = p.id AND c.is_variation = 1), 0)
                                            AS variation_value_cents,
    COALESCE((SELECT SUM(c.allocated_cents) FROM v_claim_item_coverage c
              WHERE c.project_id = p.id), 0)
                                            AS allocated_cents,
    p.contract_value_cents
    - COALESCE((SELECT SUM(cl.amount_cents) FROM claim_line cl
                WHERE cl.project_id = p.id AND cl.is_opening_balance = 1), 0)
    - COALESCE((SELECT SUM(c.value_cents) FROM v_claim_item_coverage c
                WHERE c.project_id = p.id AND c.is_variation = 0), 0)
                                            AS unitemised_cents
FROM project p;
