-- 006_po_revisions.sql — telling a variation from a correction (STP-2).
--
-- EXPAND ONLY.
--
-- `customer_po_revision` recorded that a value changed. It could not record
-- WHY, and the two reasons answer a question differently:
--
--   VARIATION   the contract genuinely became bigger, on a date. The June
--               figure was right; the order grew in July.
--   CORRECTION  the recorded value was wrong. The contract never changed;
--               we mistyped it, so the June figure was always wrong.
--
-- In the data both look like `amount_cents: X -> Y`. The difference only
-- shows when someone asks **what was orders in hand at 30 June** — and
-- reproducing a past position is the thing this platform exists to do.
--
-- `effective_date` belongs to a variation: the day the contract changed,
-- which is rarely the day someone typed it in.

ALTER TABLE customer_po_revision ADD COLUMN kind TEXT;
ALTER TABLE customer_po_revision ADD COLUMN effective_date TEXT;

-- Existing rows were written by the expand-window dual write in
-- `update_project`, which had no way to ask. Calling them corrections would
-- assert something nobody checked, so they are left NULL: unknown is a
-- truthful value and 'correction' would not be.
--
-- New rows must say. Enforced in `Db.revise_customer_po` rather than by a
-- CHECK, because the column has to stay nullable for the rows above.

CREATE INDEX customer_po_revision_kind ON customer_po_revision (kind);

-- What a PO was worth on a given date: its value now, less every variation
-- that took effect after that date. Corrections are NOT subtracted --
-- correcting a typo means the figure was always the corrected one.
CREATE VIEW v_customer_po_history AS
SELECT
    po.id                                   AS customer_po_id,
    po.project_id                           AS project_id,
    po.entity_id                            AS entity_id,
    po.po_number                            AS po_number,
    po.amount_cents                         AS amount_cents,
    po.issued_date                          AS issued_date,
    COALESCE((SELECT COUNT(*) FROM customer_po_revision r
              WHERE r.customer_po_id = po.id AND r.kind = 'variation'), 0)
                                            AS variation_count,
    COALESCE((SELECT COUNT(*) FROM customer_po_revision r
              WHERE r.customer_po_id = po.id AND r.kind = 'correction'), 0)
                                            AS correction_count,
    COALESCE((SELECT SUM(cl.amount_cents) FROM claim_line cl
              WHERE cl.customer_po_id = po.id
                AND cl.status IN ('invoiced','paid')), 0)
                                            AS claimed_cents,
    COALESCE((SELECT COUNT(*) FROM claim_line cl
              WHERE cl.customer_po_id = po.id), 0)
                                            AS claim_count
FROM customer_po po;
