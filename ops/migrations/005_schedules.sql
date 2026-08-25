-- 005_schedules.sql — recurring claims, and when they come up for renewal.
--
-- EXPAND ONLY.
--
-- Maintenance and SLA work is not twelve claims someone typed; it is one
-- agreement spread over a year. `36 Wellington` is $22,689 as twelve
-- payments of $1,890.75; `200 Victoria - ICN Maintenance` is $66,504 as
-- twelve of $5,542. Entering those by hand is the monthly copy-forward
-- ritual wearing a different hat: next year is twelve more rows, and a price
-- change is twelve edits.
--
-- So a schedule GENERATES claim lines. They are ordinary claims once made --
-- individually editable, able to slip, invoiced like anything else -- but
-- they carry `schedule_id` so their origin is known and regeneration cannot
-- duplicate them.
--
-- The renewal date is the point of the whole thing. A maintenance contract
-- that lapses unnoticed is revenue that simply stops, and nothing in a
-- spreadsheet of twelve rows tells you it is about to.

CREATE TABLE claim_schedule (
    id                  INTEGER PRIMARY KEY,
    entity_id           INTEGER NOT NULL REFERENCES entity(id),
    project_id          INTEGER NOT NULL REFERENCES project(id),
    customer_po_id      INTEGER NOT NULL REFERENCES customer_po(id),
    description         TEXT    NOT NULL,
    amount_cents        INTEGER NOT NULL,
    frequency           TEXT    NOT NULL CHECK (frequency IN
                            ('monthly','quarterly','annual')),
    start_period_id     INTEGER NOT NULL REFERENCES period(id),
    end_period_id       INTEGER NOT NULL REFERENCES period(id),

    -- When the agreement itself is up. Distinct from end_period_id: the
    -- schedule may run to June while the contract is renegotiated in April.
    renewal_date        TEXT,
    renewal_notice_days INTEGER NOT NULL DEFAULT 60,
    renewal_note        TEXT,

    is_active           INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_by          INTEGER REFERENCES users(id),
    created_ts          INTEGER NOT NULL,
    CHECK (amount_cents > 0)
) STRICT;

CREATE INDEX claim_schedule_project ON claim_schedule (project_id);
CREATE INDEX claim_schedule_renewal ON claim_schedule (renewal_date);

-- Origin of a generated claim. NULL for anything entered by hand.
ALTER TABLE claim_line ADD COLUMN schedule_id INTEGER REFERENCES claim_schedule(id);

-- One claim per schedule per period. This is what makes generation
-- idempotent: running it twice cannot produce a second November.
CREATE UNIQUE INDEX claim_line_schedule_period
    ON claim_line (schedule_id, period_id) WHERE schedule_id IS NOT NULL;

-- ------------------------------------------------------------------ views
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

-- What is coming up. `days_until` is negative once a renewal has passed,
-- which is deliberate: a lapsed agreement should get louder, not disappear
-- off the end of a list.
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
