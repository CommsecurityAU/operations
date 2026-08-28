-- 014_register_state.sql — what the sheet said (STP-3).
--
-- EXPAND ONLY.
--
-- The procurement register records a STATE and, mostly, no date to go with
-- it: fourteen rows say `Delivered` and twelve say `Paid - Pending
-- Delivery`, while only twenty-seven carry a PO date and three a payment
-- date.
--
-- The platform models those events as DATES, because payment and delivery
-- are independent. Converting one to the other loses information in
-- whichever direction it goes -- dating an event from the PO date would
-- invent a fact, and dropping the state would forget that a thing has
-- arrived.
--
-- So the imported state is kept verbatim, and the derived state is used
-- once real dates exist. A line reading `delivered (per register)` is
-- honest about where that came from, and stops being needed the moment
-- someone records the date.

ALTER TABLE procurement_line ADD COLUMN register_state TEXT;

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
        -- The register said something FURTHER ALONG than the dates can
        -- show. Knowing an order was placed on the 12th does not unsay that
        -- it has since arrived -- and the ordered date is the only one the
        -- sheet reliably carries, so preferring it would lose every
        -- delivery.
        WHEN l.register_state IS NOT NULL THEN l.register_state
        WHEN l.ordered_date IS NOT NULL THEN 'ordered'
        WHEN l.supplier_po_id IS NOT NULL THEN 'PO raised'
        ELSE 'to be ordered'
    END                                     AS state,
    -- Whether that state came from a date or from the sheet. A figure whose
    -- provenance is invisible is a figure nobody can question.
    CASE WHEN l.cancelled_date IS NULL
              AND l.delivered_date IS NULL AND l.paid_date IS NULL
              AND l.invoiced_date IS NULL
              AND l.register_state IS NOT NULL
         THEN 1 ELSE 0 END                  AS state_from_register
FROM procurement_line l
JOIN project p ON p.id = l.project_id
LEFT JOIN supplier s ON s.id = l.supplier_id
LEFT JOIN supplier_po po ON po.id = l.supplier_po_id
LEFT JOIN supplier_quote q ON q.id = l.supplier_quote_id
LEFT JOIN supplier_invoice i ON i.id = l.supplier_invoice_id
LEFT JOIN period pe ON pe.id = l.period_id;
