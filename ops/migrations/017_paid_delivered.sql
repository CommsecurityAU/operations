-- 017_paid_delivered.sql — one definition of paid, and of delivered.
--
-- EXPAND ONLY.
--
-- The screen said twenty lines were `complete` or `paid - pending delivery`
-- and then reported $0.00 paid, because the figures counted `paid_date`
-- while the state column read the stated state as well. A screen that
-- disagrees with itself is worse than either answer alone: whichever number
-- someone believes, they have been given a reason to distrust the other.
--
-- So `is_paid` and `is_delivered` are computed once, HERE, from the same
-- source the state column uses -- a date where there is one, and what
-- somebody stated where there is not. Everything downstream reads these
-- rather than deciding for itself.
--
-- A stated state is still weaker evidence than a date, and
-- `state_undated` continues to say which is which. This is about the two
-- agreeing, not about pretending an assertion is a receipt.

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
        WHEN l.stated_state IS NOT NULL THEN l.stated_state
        WHEN l.ordered_date IS NOT NULL THEN 'ordered'
        WHEN l.supplier_po_id IS NOT NULL THEN 'PO raised'
        ELSE 'to be ordered'
    END                                     AS state,
    CASE WHEN l.cancelled_date IS NULL
              AND l.delivered_date IS NULL AND l.paid_date IS NULL
              AND l.invoiced_date IS NULL
              AND l.stated_state IS NOT NULL
         THEN 1 ELSE 0 END                  AS state_undated,
    -- Paid: a date, or a state that says so. The register writes
    -- `paid - pending delivery`; the derived state writes
    -- `paid, pending delivery`. Both mean paid, and a definition that
    -- caught only one would be a definition nobody could rely on.
    CASE WHEN l.cancelled_date IS NOT NULL THEN 0
         WHEN l.paid_date IS NOT NULL THEN 1
         WHEN l.stated_state IN ('complete', 'paid - pending delivery',
                                 'paid, pending delivery') THEN 1
         ELSE 0 END                         AS is_paid,
    CASE WHEN l.cancelled_date IS NOT NULL THEN 0
         WHEN l.delivered_date IS NOT NULL THEN 1
         WHEN l.stated_state IN ('complete', 'delivered',
                                 'delivered, unpaid') THEN 1
         ELSE 0 END                         AS is_delivered
FROM procurement_line l
JOIN project p ON p.id = l.project_id
LEFT JOIN supplier s ON s.id = l.supplier_id
LEFT JOIN supplier_po po ON po.id = l.supplier_po_id
LEFT JOIN supplier_quote q ON q.id = l.supplier_quote_id
LEFT JOIN supplier_invoice i ON i.id = l.supplier_invoice_id
LEFT JOIN period pe ON pe.id = l.period_id;

DROP VIEW v_project_procurement;

CREATE VIEW v_project_procurement AS
SELECT
    p.id                                    AS project_id,
    p.entity_id                             AS entity_id,
    COALESCE(SUM(CASE WHEN v.cancelled_date IS NULL AND v.is_estimate = 0
                      THEN v.total_cents END), 0)        AS committed_cents,
    COALESCE(SUM(CASE WHEN v.cancelled_date IS NULL AND v.is_estimate = 1
                      THEN v.total_cents END), 0)        AS estimated_cents,
    COALESCE(SUM(CASE WHEN v.cancelled_date IS NULL
                      THEN v.total_cents END), 0)        AS forecast_cents,
    COALESCE(SUM(CASE WHEN v.is_paid = 1 AND v.is_estimate = 0
                      THEN v.total_cents END), 0)        AS paid_cents,
    COALESCE(SUM(CASE WHEN v.cancelled_date IS NULL AND v.is_estimate = 0
                           AND v.is_paid = 0
                      THEN v.total_cents END), 0)        AS outstanding_cents,
    COALESCE(SUM(CASE WHEN v.cancelled_date IS NULL AND v.is_estimate = 0
                           AND v.is_delivered = 0
                      THEN v.total_cents END), 0)        AS undelivered_cents,
    COUNT(v.id)                                          AS line_count,
    COALESCE(SUM(CASE WHEN v.is_estimate = 1 THEN 1 ELSE 0 END), 0)
                                                         AS estimate_count
FROM project p
LEFT JOIN v_procurement_line v ON v.project_id = p.id
GROUP BY p.id, p.entity_id;
