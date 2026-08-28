-- 016_estimates.sql — an estimate is not a commitment (STP-3).
--
-- EXPAND ONLY.
--
-- The Project Expenses matrix carries early estimates of future
-- procurement, flagged in orange by whoever entered them: 31 cells,
-- $1,576,928.29. They belong in the platform because they are the forecast
-- -- but they are not orders, and nothing has been quoted, approved or
-- committed.
--
-- $1.58m of estimates sitting in the same total as $160k of real orders
-- would make committed cost wrong by a factor of ten, so the line carries
-- a flag and every view separates them.
--
-- The lifecycle is the point: an estimate is REPLACED, not deleted. When
-- the work is quoted it becomes an ordinary line -- same row, flag
-- cleared, real supplier and cost -- so the forecast it was holding does
-- not disappear from the month while somebody types the real one.

ALTER TABLE procurement_line ADD COLUMN is_estimate INTEGER NOT NULL DEFAULT 0
    CHECK (is_estimate IN (0,1));

CREATE INDEX procurement_line_estimate ON procurement_line (is_estimate);

DROP VIEW v_project_procurement;

CREATE VIEW v_project_procurement AS
SELECT
    p.id                                    AS project_id,
    p.entity_id                             AS entity_id,
    -- COMMITTED is what has actually been ordered. An estimate is not.
    COALESCE(SUM(CASE WHEN l.cancelled_date IS NULL AND l.is_estimate = 0
                      THEN l.total_cents END), 0)        AS committed_cents,
    COALESCE(SUM(CASE WHEN l.cancelled_date IS NULL AND l.is_estimate = 1
                      THEN l.total_cents END), 0)        AS estimated_cents,
    -- What the project is expected to cost in total: both, because the
    -- forecast needs the estimate and the ledger needs the commitment.
    COALESCE(SUM(CASE WHEN l.cancelled_date IS NULL
                      THEN l.total_cents END), 0)        AS forecast_cents,
    COALESCE(SUM(CASE WHEN l.cancelled_date IS NULL AND l.paid_date IS NOT NULL
                      THEN l.total_cents END), 0)        AS paid_cents,
    COALESCE(SUM(CASE WHEN l.cancelled_date IS NULL AND l.is_estimate = 0
                           AND l.paid_date IS NULL
                      THEN l.total_cents END), 0)        AS outstanding_cents,
    COALESCE(SUM(CASE WHEN l.cancelled_date IS NULL AND l.is_estimate = 0
                           AND l.delivered_date IS NULL
                      THEN l.total_cents END), 0)        AS undelivered_cents,
    COUNT(l.id)                                          AS line_count,
    COALESCE(SUM(CASE WHEN l.is_estimate = 1 THEN 1 ELSE 0 END), 0)
                                                         AS estimate_count
FROM project p
LEFT JOIN procurement_line l ON l.project_id = p.id
GROUP BY p.id, p.entity_id;
