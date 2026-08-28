-- 015_stated_state.sql — a state asserted without a date (STP-3).
--
-- EXPAND ONLY.
--
-- `register_state` held what the procurement sheet said, because the sheet
-- records a state and mostly no date. The same column now serves a second
-- purpose: someone choosing `Delivered` from the grid without stopping to
-- find out exactly when.
--
-- Both are the same fact -- a state with nothing dated behind it -- and
-- `register_state` describes only where the first one came from. A name
-- that describes the source rather than the thing is how
-- `purchase_order_cents` came to mean contract value (ADR-34), so it is
-- renamed now rather than explained forever.
--
-- The view's flag follows: `state_from_register` becomes `state_undated`,
-- which is what it has always actually meant and what the amber marking in
-- the grid is telling you.

ALTER TABLE procurement_line RENAME COLUMN register_state TO stated_state;

DROP VIEW v_procurement_line;

CREATE VIEW v_procurement_line AS
SELECT
    l.*,
    p.name                                  AS project_name,
    p.job_code                              AS job_code,
    s.name                                  AS supplier_name,
    po.po_number                            AS po_number,
    q.quote_ref                             AS quote_ref,
    q.fx_rate_bp                            AS fx_rate_bp,
    i.invoice_ref                           AS invoice_ref,
    pe.label                                AS period_label,
    pe.month_start                          AS month_start,
    CASE
        WHEN l.cancelled_date IS NOT NULL THEN 'cancelled'
        WHEN l.delivered_date IS NOT NULL AND l.paid_date IS NOT NULL
             THEN 'complete'
        WHEN l.delivered_date IS NOT NULL THEN 'delivered, unpaid'
        WHEN l.paid_date IS NOT NULL THEN 'paid, pending delivery'
        WHEN l.invoiced_date IS NOT NULL THEN 'invoiced'
        -- A stated state outranks an ordered date: knowing an order was
        -- placed on the 12th does not unsay that it has since arrived.
        WHEN l.stated_state IS NOT NULL THEN l.stated_state
        WHEN l.ordered_date IS NOT NULL THEN 'ordered'
        WHEN l.supplier_po_id IS NOT NULL THEN 'PO raised'
        ELSE 'to be ordered'
    END                                     AS state,
    -- Nothing dated behind it. The grid marks these, because they are the
    -- work still to do -- and a figure whose provenance is invisible is a
    -- figure nobody can question.
    CASE WHEN l.cancelled_date IS NULL
              AND l.delivered_date IS NULL AND l.paid_date IS NULL
              AND l.invoiced_date IS NULL
              AND l.stated_state IS NOT NULL
         THEN 1 ELSE 0 END                  AS state_undated
FROM procurement_line l
JOIN project p ON p.id = l.project_id
LEFT JOIN supplier s ON s.id = l.supplier_id
LEFT JOIN supplier_po po ON po.id = l.supplier_po_id
LEFT JOIN supplier_quote q ON q.id = l.supplier_quote_id
LEFT JOIN supplier_invoice i ON i.id = l.supplier_invoice_id
LEFT JOIN period pe ON pe.id = l.period_id;
