-- 023_fy_settings.sql — the few numbers a year needs (STP-5).
--
-- EXPAND ONLY.
--
-- Two figures the dashboard needs that are not derivable from anything the
-- platform already holds:
--
-- CORPORATE TAX RATE. 25% today. It belongs per financial year because it
-- changes per financial year, and a rate baked into a formula is a rate
-- somebody has to find and edit under pressure.
--
-- FURTHER SALES. Revenue expected but not yet attached to any project --
-- $500,000 in the sheet, feeding a `Planned Revenue` figure of $4,073,186.
-- It is a judgement rather than a record, so it is stored where a judgement
-- can be seen and changed, not buried in a cell.
--
-- TAX IS ASSESSED ON THE YEAR. The sheet computed it two ways and showed
-- both: taxing each profitable month with no credit for loss months gave
-- $267,227 and a headline net profit of -$162,580, while taxing the year's
-- profit once gave $26,162 and +$78,485. A quarter of a million apart, and
-- the second is how company tax works -- a loss in May offsets a profit in
-- December. The platform does it annually, and a year that loses money pays
-- nothing.

CREATE TABLE fy_settings (
    id                   INTEGER PRIMARY KEY,
    entity_id            INTEGER NOT NULL REFERENCES entity(id),
    fy                   INTEGER NOT NULL,
    -- Hundredths of a basis point, as everywhere else: 25% is 250000.
    tax_rate_bp          INTEGER NOT NULL DEFAULT 250000,
    -- Expected, not yet a project. A judgement, held where it can be seen.
    further_sales_cents  INTEGER NOT NULL DEFAULT 0,
    note                 TEXT,
    updated_by           INTEGER REFERENCES users(id),
    updated_ts           INTEGER NOT NULL,
    CHECK (tax_rate_bp >= 0 AND tax_rate_bp <= 1000000),
    CHECK (further_sales_cents >= 0),
    UNIQUE (entity_id, fy)
) STRICT;

-- ------------------------------------------------------------------ views
-- Revenue by month: what has been billed and what is forecast, kept apart
-- because one is a fact and the other is a plan.
CREATE VIEW v_month_revenue AS
SELECT
    pe.id                                   AS period_id,
    pe.label                                AS period_label,
    pe.fy                                   AS fy,
    pe.fy_label                             AS fy_label,
    pe.month_start                          AS month_start,
    p.entity_id                             AS entity_id,
    COALESCE(SUM(CASE WHEN cl.status IN ('invoiced','paid')
                      THEN cl.amount_cents END), 0)      AS invoiced_cents,
    COALESCE(SUM(CASE WHEN cl.status NOT IN ('invoiced','paid')
                      THEN cl.amount_cents END), 0)      AS forecast_cents,
    COALESCE(SUM(cl.amount_cents), 0)                    AS total_cents
FROM period pe
LEFT JOIN claim_line cl ON cl.period_id = pe.id AND cl.is_opening_balance = 0
LEFT JOIN project p ON p.id = cl.project_id
GROUP BY pe.id, p.entity_id;

-- Project cost by month: what has been bought for jobs. Estimates included,
-- because a forecast that leaves out the expected cost of the work is not a
-- forecast.
CREATE VIEW v_month_project_cost AS
SELECT
    pe.id                                   AS period_id,
    pe.label                                AS period_label,
    pe.fy                                   AS fy,
    pe.fy_label                             AS fy_label,
    pe.month_start                          AS month_start,
    l.entity_id                             AS entity_id,
    COALESCE(SUM(CASE WHEN l.is_estimate = 0
                      THEN l.total_cents END), 0)        AS committed_cents,
    COALESCE(SUM(CASE WHEN l.is_estimate = 1
                      THEN l.total_cents END), 0)        AS estimated_cents,
    COALESCE(SUM(l.total_cents), 0)                      AS total_cents
FROM period pe
LEFT JOIN procurement_line l ON l.period_id = pe.id
                            AND l.cancelled_date IS NULL
GROUP BY pe.id, l.entity_id;

-- Office cost by month. It does NOT attach to a project: rent and payroll
-- tax are not bought for a job, and allocating them across jobs would
-- invent a margin nobody agreed to. It comes off the bottom line.
CREATE VIEW v_month_office_cost AS
SELECT
    pe.id                                   AS period_id,
    pe.label                                AS period_label,
    pe.fy                                   AS fy,
    pe.fy_label                             AS fy_label,
    pe.month_start                          AS month_start,
    l.entity_id                             AS entity_id,
    COALESCE(SUM(a.amount_cents), 0)        AS total_cents
FROM period pe
LEFT JOIN expense_amount a ON a.period_id = pe.id
LEFT JOIN expense_line l ON l.id = a.expense_line_id
GROUP BY pe.id, l.entity_id;
