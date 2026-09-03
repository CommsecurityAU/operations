-- 024_project_status.nofk.sql — a vocabulary that can grow.
--
-- REBUILDS A REFERENCED TABLE. Runs with foreign keys off and
-- `foreign_key_check` before the commit; see `Db._rebuild_migration`.
--
-- The register now uses a status the platform refuses: `Lost`, for work
-- tendered and not won. `status` was a CHECK over four values, and widening
-- a CHECK means recreating the table -- which, with eight tables
-- referencing `project`, is not something to do twice.
--
-- So it is done ONCE and properly: the statuses become a LOOKUP TABLE.
-- Adding one is an INSERT from now on, not a schema change and not a
-- rebuild. `project_type` has worked this way since migration 001 and has
-- needed nothing since.
--
-- `is_open` is what the dashboard counts as an active project. `Lost` and
-- `Complete` are both finished, and neither should inflate a count of work
-- in progress -- but they are finished in very different ways, which is
-- exactly why one status could not stand for both.

-- The old index is called `project_status` too, and it goes when the table
-- does -- but not before this runs, so it is dropped explicitly to leave
-- the name free for the lookup.
DROP INDEX project_status;

CREATE TABLE project_status (
    code      TEXT PRIMARY KEY,
    label     TEXT NOT NULL,
    -- Work still going on. Counted as active; anything else is not.
    is_open   INTEGER NOT NULL DEFAULT 1 CHECK (is_open IN (0,1)),
    sequence  INTEGER NOT NULL DEFAULT 0
) STRICT;

INSERT INTO project_status (code, label, is_open, sequence) VALUES
    ('Active',   'Active',                  1, 1),
    ('DLP',      'Defects liability',       1, 2),
    ('SLA',      'Under service agreement', 1, 3),
    ('Complete', 'Complete',                0, 4),
    -- Tendered and not won. It has to exist as its own thing: a lost job
    -- is not a completed one, and calling it complete would put work in
    -- the finished pile that was never done.
    ('Lost',     'Lost',                    0, 5);

-- Anything already in use that this list does not name: kept, so a rebuild
-- never silently drops a project's status.
INSERT OR IGNORE INTO project_status (code, label, is_open, sequence)
SELECT DISTINCT status, status, 1, 99 FROM project;

-- EVERY VIEW IN THE DEPENDENCY CLOSURE, dropped and recreated HERE.
--
-- The runner also preserves views around a rebuild, and for a while this
-- file relied on that. The N-1 gate refused it: that gate runs the PREVIOUS
-- release's code against the new migrations, and the previous release's
-- runner has no such feature -- `error in view v_project_pipeline: no such
-- table: main.project`.
--
-- The gate was right about something bigger than itself. **A migration that
-- only works with one version of the runner is a hidden coupling.** A
-- migration is a historical record: it recreates the views AS THEY WERE AT
-- THIS MOMENT, and a later migration is free to redefine them. That is not
-- duplication to be kept in step -- it is what a migration is.
--
-- NINE, not eight. `v_upcoming_renewals` never mentions `project` and broke
-- anyway, because it reads `v_schedule_coverage` which does. Dropping a
-- view breaks every view built on it, so the list is the transitive
-- closure and the order is dependency order -- dropped innermost-last,
-- recreated innermost-first.
--
DROP VIEW IF EXISTS v_upcoming_renewals;
DROP VIEW IF EXISTS v_project_procurement;
DROP VIEW IF EXISTS v_schedule_coverage;
DROP VIEW IF EXISTS v_project_retention;
DROP VIEW IF EXISTS v_project_pipeline;
DROP VIEW IF EXISTS v_project_orders_in_hand;
DROP VIEW IF EXISTS v_project_claim_plan;
DROP VIEW IF EXISTS v_procurement_line;
DROP VIEW IF EXISTS v_month_revenue;

CREATE TABLE project_new (
    id                    INTEGER PRIMARY KEY,
    entity_id             INTEGER NOT NULL REFERENCES entity(id),
    name                  TEXT    NOT NULL,
    job_code              TEXT    NOT NULL,
    project_no            TEXT,
    client_id             INTEGER REFERENCES client(id),
    type_id               INTEGER REFERENCES project_type(id),
    -- A reference now, not a CHECK. Adding a status is an INSERT.
    status                TEXT    NOT NULL REFERENCES project_status(code),
    project_lead          TEXT,
    purchase_order_cents  INTEGER NOT NULL DEFAULT 0,
    invoiced_prior_cents  INTEGER NOT NULL DEFAULT 0,
    needs_resolution      INTEGER NOT NULL DEFAULT 0
                          CHECK (needs_resolution IN (0,1)),
    notes                 TEXT,
    source_row            INTEGER,
    created_ts            INTEGER NOT NULL,
    practical_completion_date TEXT,
    dlp_end_date          TEXT,
    contract_value_cents  INTEGER NOT NULL DEFAULT 0,
    CHECK (purchase_order_cents >= 0),
    CHECK (invoiced_prior_cents >= 0),
    -- The register's own assertion, enforced per row: a project can never
    -- have been invoiced more than its contract value (ADR-22). Carried
    -- across deliberately -- a rebuild that quietly drops a constraint is
    -- a rebuild that removes a rule nobody decided to remove.
    CHECK (invoiced_prior_cents <= purchase_order_cents),
    UNIQUE (entity_id, name)
) STRICT;

INSERT INTO project_new
SELECT id, entity_id, name, job_code, project_no, client_id, type_id,
       status, project_lead, purchase_order_cents, invoiced_prior_cents,
       needs_resolution, notes, source_row, created_ts,
       practical_completion_date, dlp_end_date, contract_value_cents
FROM project;

DROP TABLE project;
ALTER TABLE project_new RENAME TO project;

-- Exactly as they were. NOT unique on the job code: two projects sharing
-- one is a real and deliberate case -- `Brennan Pl` has an implementation
-- and a licence on `JN-6980` -- and the worklist tracks it rather than the
-- schema forbidding it. A rebuild that tightened a constraint would refuse
-- data the platform already holds.
CREATE INDEX project_job_code ON project (job_code);
CREATE INDEX project_status_idx ON project (status);

-- The triggers that keep the two contract columns in step (migration 018)
-- went with the table.
CREATE TRIGGER project_contract_default_insert
AFTER INSERT ON project
WHEN NEW.contract_value_cents = 0 AND NEW.purchase_order_cents <> 0
BEGIN
    UPDATE project SET contract_value_cents = NEW.purchase_order_cents
    WHERE id = NEW.id;
END;

CREATE TRIGGER project_contract_default_update
AFTER UPDATE OF purchase_order_cents ON project
WHEN NEW.contract_value_cents = 0 AND NEW.purchase_order_cents <> 0
BEGIN
    UPDATE project SET contract_value_cents = NEW.purchase_order_cents
    WHERE id = NEW.id;
END;


-- ------------------------------------------------------------------ views
-- Recreated exactly as they were, in dependency order.
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

CREATE VIEW v_project_pipeline AS
SELECT
    p.id        AS project_id,
    p.entity_id AS entity_id,
    p.name      AS project_name,
    COALESCE(SUM(CASE WHEN cl.status = 'forecast' THEN cl.amount_cents END), 0)
        AS forecast_cents,
    COALESCE(SUM(CASE WHEN cl.status IN ('due','approved') THEN cl.amount_cents END), 0)
        AS due_cents,
    COALESCE(SUM(CASE WHEN cl.status = 'invoiced' THEN cl.amount_cents END), 0)
        AS invoiced_cents,
    COALESCE(SUM(CASE WHEN cl.status = 'paid' THEN cl.amount_cents END), 0)
        AS paid_cents
FROM project p
LEFT JOIN claim_line cl ON cl.project_id = p.id
GROUP BY p.id, p.entity_id, p.name;

CREATE VIEW v_project_retention AS
SELECT
    p.id                                    AS project_id,
    p.entity_id                             AS entity_id,
    p.name                                  AS project_name,
    p.status                                AS status,
    p.practical_completion_date             AS practical_completion_date,
    p.dlp_end_date                          AS dlp_end_date,
    COALESCE(SUM(r.cap_cents), 0)           AS cap_cents,
    COALESCE(SUM(r.withheld_cents), 0)      AS withheld_cents,
    COALESCE(SUM(r.released_cents), 0)      AS released_cents,
    COALESCE(SUM(r.withheld_cents - r.released_cents), 0) AS held_cents
FROM project p
LEFT JOIN v_po_retention r ON r.project_id = p.id
GROUP BY p.id, p.entity_id, p.name, p.status,
         p.practical_completion_date, p.dlp_end_date;

CREATE VIEW v_schedule_coverage AS
SELECT
    s.id                                    AS schedule_id,
    s.entity_id                             AS entity_id,
    s.project_id                            AS project_id,
    p.name                                  AS project_name,
    s.description                           AS description,
    s.amount_cents                          AS amount_cents,
    s.frequency                             AS frequency,
    s.is_active                             AS is_active,
    s.renewal_date                          AS renewal_date,
    s.renewal_notice_days                   AS renewal_notice_days,
    sp.label                                AS start_label,
    ep.label                                AS end_label,
    sp.month_start                          AS start_month,
    ep.month_end                            AS end_month,
    (SELECT COUNT(*) FROM claim_line cl WHERE cl.schedule_id = s.id)
                                            AS generated_count,
    COALESCE((SELECT SUM(cl.amount_cents) FROM claim_line cl
              WHERE cl.schedule_id = s.id), 0)
                                            AS generated_cents
FROM claim_schedule s
JOIN project p ON p.id = s.project_id
JOIN period sp ON sp.id = s.start_period_id
JOIN period ep ON ep.id = s.end_period_id;

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

CREATE VIEW v_upcoming_renewals AS
SELECT
    c.*,
    CAST(julianday(c.renewal_date) - julianday('now') AS INTEGER) AS days_until,
    CASE
        WHEN c.renewal_date IS NULL THEN 'no date set'
        WHEN julianday(c.renewal_date) < julianday('now') THEN 'overdue'
        WHEN julianday(c.renewal_date) - julianday('now')
             <= c.renewal_notice_days THEN 'due'
        ELSE 'future'
    END                                                          AS renewal_state
FROM v_schedule_coverage c
WHERE c.is_active = 1;
