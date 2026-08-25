-- 004_retention.sql — retention, milestone dates, and EOM as a first-class
-- axis (STP-2).
--
-- EXPAND ONLY: every column added is nullable or defaulted, so the previous
-- release still finds what it reads.
--
-- Three ideas here.
--
-- 1. RETENTION IS A PROPERTY OF THE PO, NOT THE PROJECT. A variation that
--    raises the PO raises its cap with it; scope run as a separate PO
--    carries its own terms, or none at all. A project can therefore have
--    retention on one PO and not another, which putting the settings on
--    `project` would have made impossible to express.
--
-- 2. RETENTION IS WITHHELD FROM A CLAIM, not held as a separate contract
--    line. `claim_line.retention_cents` records what the customer kept back
--    on that claim, so the withholding is attached to the event that caused
--    it.
--
-- 3. A RELEASE IS ITSELF A CLAIM LINE. Flagged, but otherwise ordinary --
--    so it forecasts, ages, gets assigned an EOM and gets invoiced through
--    machinery that already exists rather than needing its own.
--
-- All amounts are EX-GST. GST is applied at invoice, on the net of
-- retention, and is not stored on the claim.

-- ------------------------------------------------------- milestone dates
-- Retention release cannot be FORECAST without these, and forecasting is
-- the point. Nullable: not every project has a DLP.
ALTER TABLE project ADD COLUMN practical_completion_date TEXT;
ALTER TABLE project ADD COLUMN dlp_end_date              TEXT;

-- ------------------------------------------------------- retention terms
-- Basis points throughout: 2.5% is 250, exactly, rather than a decimal that
-- has to be rounded before it is even used.
ALTER TABLE customer_po ADD COLUMN retention_applies   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE customer_po ADD COLUMN retention_rate_bp   INTEGER;   -- 1000 = 10% per claim
ALTER TABLE customer_po ADD COLUMN retention_cap_bp    INTEGER;   -- 250  = 2.5% of the PO
ALTER TABLE customer_po ADD COLUMN release_policy      TEXT;      -- 'dlp' | 'split'
ALTER TABLE customer_po ADD COLUMN release_split_bp    INTEGER;   -- share released at PC

-- ------------------------------------------------------- on a claim line
ALTER TABLE claim_line ADD COLUMN retention_cents      INTEGER NOT NULL DEFAULT 0;
ALTER TABLE claim_line ADD COLUMN is_retention_release INTEGER NOT NULL DEFAULT 0;

-- ------------------------------------------------------------------ views
-- Retention position per PO. `remaining_to_withhold` is what makes the cap
-- work: once cumulative withholding reaches it, later claims withhold
-- nothing, and a variation that raises the PO reopens capacity.
CREATE VIEW v_po_retention AS
SELECT
    po.id                                   AS customer_po_id,
    po.project_id                           AS project_id,
    po.entity_id                            AS entity_id,
    po.amount_cents                         AS contract_cents,
    po.retention_applies                    AS retention_applies,
    COALESCE(po.retention_rate_bp, 0)       AS rate_bp,
    COALESCE(po.retention_cap_bp, 0)        AS cap_bp,
    po.release_policy                       AS release_policy,
    COALESCE(po.release_split_bp, 0)        AS release_split_bp,
    -- Zero unless retention actually applies. Computing 2.5% of a PO that
    -- has no retention reports a cap for money nobody is holding, which is
    -- the same species of lie as a dashboard cell reading #N/A.
    CASE WHEN po.retention_applies = 1
         THEN (po.amount_cents * COALESCE(po.retention_cap_bp, 0)) / 10000
         ELSE 0 END                         AS cap_cents,
    COALESCE((SELECT SUM(cl.retention_cents) FROM claim_line cl
              WHERE cl.customer_po_id = po.id
                AND cl.status IN ('invoiced','paid')), 0)
                                            AS withheld_cents,
    COALESCE((SELECT SUM(cl.amount_cents) FROM claim_line cl
              WHERE cl.customer_po_id = po.id
                AND cl.is_retention_release = 1
                AND cl.status IN ('invoiced','paid')), 0)
                                            AS released_cents
FROM customer_po po;

-- What is still held, and what may still be withheld.
CREATE VIEW v_po_retention_position AS
SELECT
    r.*,
    MAX(r.cap_cents - r.withheld_cents, 0)  AS remaining_to_withhold_cents,
    r.withheld_cents - r.released_cents     AS held_cents
FROM v_po_retention r;

-- Project-level roll-up, with the dates a release can be forecast against.
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
