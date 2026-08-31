-- 019_invoiced_fallback.sql — invoiced, when there are no claims yet.
--
-- EXPAND ONLY.
--
-- The second half of what the N-1 gate found, and the same shape as
-- migration `018`.
--
-- Migration `003` turned `project.invoiced_prior_cents` into opening-balance
-- `claim_line` rows, and `007`'s view derives what has been invoiced from
-- those rows. Both were one-shot: they ran over the data that existed then.
--
-- The PREVIOUS RELEASE writes `invoiced_prior_cents` on the project and
-- creates no claims. Against this schema its projects therefore read as
-- never invoiced, and orders in hand comes out as the WHOLE CONTRACT --
-- $7,231,907.00 where $3,520,041.73 was expected.
--
-- Overstating what is left to bill is the worse direction for this figure
-- to be wrong in.
--
-- So the view falls back: claims where a project HAS claims, and the
-- project's own column where it has none. Once a single claim exists the
-- claims win, which keeps `003`'s intent -- the claim rows are the record
-- and the column is what preceded them.
--
-- `EXISTS` rather than a sum, because a project whose claims total zero has
-- claims and should read zero, not fall back to a figure it has superseded.

DROP VIEW v_project_orders_in_hand;

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
    CASE WHEN EXISTS (SELECT 1 FROM claim_line cl
                      WHERE cl.project_id = p.id
                        AND cl.status IN ('invoiced','paid'))
         THEN (SELECT SUM(cl.amount_cents) FROM claim_line cl
               WHERE cl.project_id = p.id
                 AND cl.status IN ('invoiced','paid'))
         ELSE p.invoiced_prior_cents END
                            AS invoiced_prior_cents,
    p.contract_value_cents
    - CASE WHEN EXISTS (SELECT 1 FROM claim_line cl
                        WHERE cl.project_id = p.id
                          AND cl.status IN ('invoiced','paid'))
           THEN (SELECT SUM(cl.amount_cents) FROM claim_line cl
                 WHERE cl.project_id = p.id
                   AND cl.status IN ('invoiced','paid'))
           ELSE p.invoiced_prior_cents END
                            AS orders_in_hand_cents,
    -- What we hold an order for and have not billed. Can go negative where
    -- invoicing has run ahead of the orders, which is worth seeing.
    COALESCE((SELECT SUM(po.amount_cents) FROM customer_po po
              WHERE po.project_id = p.id AND po.is_placeholder = 0), 0)
    - CASE WHEN EXISTS (SELECT 1 FROM claim_line cl
                        WHERE cl.project_id = p.id
                          AND cl.status IN ('invoiced','paid'))
           THEN (SELECT SUM(cl.amount_cents) FROM claim_line cl
                 WHERE cl.project_id = p.id
                   AND cl.status IN ('invoiced','paid'))
           ELSE p.invoiced_prior_cents END
                            AS ordered_unbilled_cents
FROM project p;
