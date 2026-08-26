-- 007_contract_value.sql — the contract and the orders are different things.
--
-- EXPAND ONLY.
--
-- Migration 003 turned the register's `Purchase Order` column into a
-- `customer_po` row. That column was never a customer order: it was the
-- CONTRACT VALUE, what the job is worth. So on a project where orders
-- arrive progressively, adding the real POs alongside the migrated row
-- double-counted -- `200 Victoria - IBP` read $422,833.33 against a
-- $295,000 contract, because four orders that were portions OF the contract
-- were summed WITH it.
--
-- The correction:
--
--   project.contract_value_cents   what the job is worth. Exists before any
--                                  PO does, and is updated when a variation
--                                  raises it.
--   customer_po                    what the customer has actually ordered.
--                                  Zero, one, or a dozen. May total less
--                                  than the contract (orders still coming),
--                                  or match it.
--
-- The migrated rows cannot simply be deleted: 204 claims reference them,
-- and the retention terms sit on them. They are flagged `is_placeholder`
-- instead -- they exist to carry claims and retention, and are excluded
-- from what has been ORDERED.

ALTER TABLE project ADD COLUMN contract_value_cents INTEGER NOT NULL DEFAULT 0;

-- Rows that exist to hold claims rather than to record an order: the ones
-- migration 003 made from the register, and the zero-value ones the claims
-- importer created for projects billing with no order recorded.
ALTER TABLE customer_po ADD COLUMN is_placeholder INTEGER NOT NULL DEFAULT 0
    CHECK (is_placeholder IN (0,1));

UPDATE customer_po SET is_placeholder = 1
WHERE note LIKE 'migrated from the FY27 register%'
   OR note LIKE 'placeholder:%';

-- The contract is what the register said, which is what those rows hold.
UPDATE project SET contract_value_cents = COALESCE(
    (SELECT SUM(po.amount_cents) FROM customer_po po
     WHERE po.project_id = project.id AND po.is_placeholder = 1),
    (SELECT SUM(po.amount_cents) FROM customer_po po
     WHERE po.project_id = project.id),
    0);

-- ------------------------------------------------------------------ views
DROP VIEW v_project_orders_in_hand;

-- Orders in hand keeps the meaning it has always had in the register:
-- `contract - invoiced`. Everything pinned reconciles to that, and the
-- PO-sum version quietly redefined it.
--
-- `ordered_cents` is the genuinely new figure: how much of the contract the
-- customer has actually raised an order for. On a job where POs arrive
-- progressively that gap is the difference between what we expect to bill
-- and what we are currently entitled to bill.
CREATE VIEW v_project_orders_in_hand AS
SELECT
    p.id                    AS project_id,
    p.entity_id             AS entity_id,
    p.name                  AS project_name,
    p.job_code              AS job_code,
    p.status                AS status,
    p.contract_value_cents  AS purchase_order_cents,   -- name kept for N-1
    p.contract_value_cents  AS contract_value_cents,
    COALESCE((SELECT SUM(po.amount_cents) FROM customer_po po
              WHERE po.project_id = p.id AND po.is_placeholder = 0), 0)
                            AS ordered_cents,
    COALESCE((SELECT SUM(cl.amount_cents) FROM claim_line cl
              WHERE cl.project_id = p.id
                AND cl.status IN ('invoiced','paid')), 0)
                            AS invoiced_prior_cents,
    p.contract_value_cents
    - COALESCE((SELECT SUM(cl.amount_cents) FROM claim_line cl
                WHERE cl.project_id = p.id
                  AND cl.status IN ('invoiced','paid')), 0)
                            AS orders_in_hand_cents,
    -- What we hold an order for and have not billed. Can go negative where
    -- invoicing has run ahead of the orders, which is worth seeing.
    COALESCE((SELECT SUM(po.amount_cents) FROM customer_po po
              WHERE po.project_id = p.id AND po.is_placeholder = 0), 0)
    - COALESCE((SELECT SUM(cl.amount_cents) FROM claim_line cl
                WHERE cl.project_id = p.id
                  AND cl.status IN ('invoiced','paid')), 0)
                            AS ordered_unbilled_cents
FROM project p;
