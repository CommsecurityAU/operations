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

-- The views are dropped and put back by the RUNNER: eight of them mention
-- `project`, they do not change here, and copying their definitions into
-- this file would duplicate two hundred lines that must then stay in step
-- with the originals forever.

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
