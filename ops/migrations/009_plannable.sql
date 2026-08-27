-- 009_plannable.sql — a plan describes what is LEFT to claim (STP-2).
--
-- EXPAND ONLY.
--
-- `v_project_claim_plan` compared the items against the whole contract,
-- which reported an under-itemised plan on every project that was already
-- part-invoiced when the platform's window opened.
--
-- `720 Bourke - IBP`: a $198,610 contract with $112,545.67 claimed before
-- FY27. The plan adopted $86,064.34 of items and the panel called
-- $112,545.66 unitemised -- but that money is not unplanned, it is BILLED,
-- and no plan in this system could ever describe it. The Verification of
-- Design phase was invoiced in FY26 and the platform has it only as an
-- opening balance.
--
--     contract              $198,610.00
--   - opening balance       $112,545.67   claimed before the window
--   = plannable              $86,064.33   what a plan can describe
--     items                  $86,064.34   what it does describe
--
-- So the comparison is against CONTRACT LESS THE OPENING BALANCE. Claims
-- invoiced inside the window stay in the plan: they were planned here, and
-- removing them would make a completed project look unplanned.

DROP VIEW v_project_claim_plan;

CREATE VIEW v_project_claim_plan AS
SELECT
    p.id                                    AS project_id,
    p.entity_id                             AS entity_id,
    p.contract_value_cents                  AS contract_value_cents,
    -- Invoiced before this platform's window. Not unplanned, just not ours.
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
