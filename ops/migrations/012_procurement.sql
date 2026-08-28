-- 012_procurement.sql — what a project costs to buy (STP-3).
--
-- EXPAND ONLY.
--
-- Modelled from the Procurement register, which a project engineer fills in
-- and the accounts team works from. The flow, in the order it happens:
--
--   an engineer enters LINE ITEMS against a project
--   they are emailed to accounts, who seek approval from management
--   a PO is generated -- generally one per project
--   the supplier invoices, delivers, and is paid, in whatever order
--
-- Four things that shape the schema, each from how the register is actually
-- used rather than from how procurement usually works:
--
-- 1. PAYMENT AND DELIVERY ARE INDEPENDENT. `Paid - Pending Delivery` sits
--    alongside `Delivered` in the register, on twelve rows of fifty-nine --
--    the normal case, not an edge one. So they are two DATES and the status
--    is derived. A state machine would have to enumerate every combination
--    and would still be wrong the first time something arrives before it is
--    invoiced.
--
-- 2. A QUOTE MAY COVER SEVERAL PROJECTS. `MSI Cubi - RAVEN` is quoted once
--    and lands on six jobs. So the quote sits above the order, and the FX
--    RATE BELONGS TO IT: the rate is agreed with the supplier when the
--    quote is given.
--
-- 3. ONE SUPPLIER INVOICE MAY COVER SEVERAL ORDERS. The register carries
--    `Invoice Ref` per LINE, which is the flexible arrangement: several
--    lines across several POs can share an invoice, and one PO can be
--    invoiced twice for a part shipment.
--
-- 4. THE FOREIGN AMOUNT IS THE FACT. The register converts the extended
--    total, so `$33.00 x 7 x 1.388561` is $320.76 while rounding the unit
--    first gives $320.74. Five rows differ by a cent or two for exactly
--    this reason. Storing the USD figure and the rate, and converting once
--    at the line total, removes the discrepancy rather than reconciling it.

CREATE TABLE supplier_quote (
    id            INTEGER PRIMARY KEY,
    entity_id     INTEGER NOT NULL REFERENCES entity(id),
    supplier_id   INTEGER NOT NULL REFERENCES supplier(id),
    quote_ref     TEXT,
    quote_date    TEXT,
    currency      TEXT    NOT NULL DEFAULT 'AUD'
                  CHECK (currency IN ('AUD','USD')),
    -- Basis points of AUD per unit of foreign currency: 1.388561 is
    -- 13885610. Integer, because `ops/money.py` is the only place a
    -- division happens (ADR-15) and a float here would put rounding in the
    -- schema.
    fx_rate_bp    INTEGER,
    -- Where the conversation lives. The register carries an email subject
    -- for exactly this reason: finding the correspondence months later is
    -- most of the work when something is queried.
    email_subject TEXT,
    email_sent_date TEXT,
    note          TEXT,
    created_by    INTEGER REFERENCES users(id),
    created_ts    INTEGER NOT NULL,
    -- A foreign-currency quote without a rate cannot be costed in AUD, and
    -- a rate on an AUD quote is meaningless.
    CHECK ((currency = 'AUD' AND fx_rate_bp IS NULL)
        OR (currency <> 'AUD' AND fx_rate_bp IS NOT NULL AND fx_rate_bp > 0))
) STRICT;

CREATE INDEX supplier_quote_supplier ON supplier_quote (supplier_id);

-- Generally one per project. It references the quote it came from, so a
-- quote spanning six jobs produces six orders that all cost at one rate.
CREATE TABLE supplier_po (
    id             INTEGER PRIMARY KEY,
    entity_id      INTEGER NOT NULL REFERENCES entity(id),
    project_id     INTEGER NOT NULL REFERENCES project(id),
    supplier_id    INTEGER NOT NULL REFERENCES supplier(id),
    supplier_quote_id INTEGER REFERENCES supplier_quote(id),
    po_number      TEXT,
    po_date        TEXT,
    approved_by    TEXT,
    approved_date  TEXT,
    note           TEXT,
    created_by     INTEGER REFERENCES users(id),
    created_ts     INTEGER NOT NULL
) STRICT;

CREATE INDEX supplier_po_project ON supplier_po (project_id);
CREATE INDEX supplier_po_supplier ON supplier_po (supplier_id);
CREATE INDEX supplier_po_quote ON supplier_po (supplier_quote_id);

-- A supplier invoice. Sits beside the orders rather than under one, because
-- a single invoice regularly covers several.
CREATE TABLE supplier_invoice (
    id            INTEGER PRIMARY KEY,
    entity_id     INTEGER NOT NULL REFERENCES entity(id),
    supplier_id   INTEGER NOT NULL REFERENCES supplier(id),
    invoice_ref   TEXT    NOT NULL,
    invoice_date  TEXT,
    due_date      TEXT,
    note          TEXT,
    created_by    INTEGER REFERENCES users(id),
    created_ts    INTEGER NOT NULL,
    CHECK (length(trim(invoice_ref)) > 0)
) STRICT;

CREATE UNIQUE INDEX supplier_invoice_ref
    ON supplier_invoice (entity_id, supplier_id, invoice_ref);

-- THE LINE ITEM: what the engineer actually enters.
CREATE TABLE procurement_line (
    id              INTEGER PRIMARY KEY,
    entity_id       INTEGER NOT NULL REFERENCES entity(id),
    project_id      INTEGER NOT NULL REFERENCES project(id),
    supplier_id     INTEGER REFERENCES supplier(id),
    supplier_po_id  INTEGER REFERENCES supplier_po(id),
    supplier_quote_id INTEGER REFERENCES supplier_quote(id),
    -- Per LINE, not per order: several lines across several orders share an
    -- invoice, and one order can be invoiced twice for a part shipment.
    supplier_invoice_id INTEGER REFERENCES supplier_invoice(id),
    -- The anticipated payment month, the same axis invoicing uses. That is
    -- what lets procurement and claims meet in one cash forecast.
    period_id       INTEGER REFERENCES period(id),

    item            TEXT,
    description     TEXT,
    quantity        INTEGER NOT NULL DEFAULT 1,

    -- The FOREIGN amount where there is one, and the AUD it converts to.
    -- Both stored: the foreign figure is what the supplier charges and the
    -- AUD is what it costs us, and deriving either at read time would move
    -- money by a cent or two per line.
    currency        TEXT    NOT NULL DEFAULT 'AUD'
                    CHECK (currency IN ('AUD','USD')),
    unit_cost_cents INTEGER NOT NULL DEFAULT 0,
    total_cents     INTEGER NOT NULL DEFAULT 0,

    -- Independent facts, not a status (see 1 above).
    requested_date  TEXT,
    ordered_date    TEXT,
    invoiced_date   TEXT,
    delivered_date  TEXT,
    paid_date       TEXT,
    cancelled_date  TEXT,
    cancel_reason   TEXT,

    note            TEXT,
    created_by      INTEGER REFERENCES users(id),
    created_ts      INTEGER NOT NULL,
    CHECK (quantity > 0),
    CHECK (unit_cost_cents >= 0),
    CHECK (total_cents >= 0)
) STRICT;

CREATE INDEX procurement_line_project ON procurement_line (project_id);
CREATE INDEX procurement_line_period ON procurement_line (period_id);
CREATE INDEX procurement_line_po ON procurement_line (supplier_po_id);
CREATE INDEX procurement_line_invoice ON procurement_line (supplier_invoice_id);

CREATE TABLE procurement_line_revision (
    id         INTEGER PRIMARY KEY,
    line_id    INTEGER NOT NULL REFERENCES procurement_line(id),
    field      TEXT    NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    reason     TEXT,
    changed_by INTEGER REFERENCES users(id),
    changed_ts INTEGER NOT NULL
) STRICT;

CREATE INDEX procurement_line_revision_line ON procurement_line_revision (line_id);

-- ------------------------------------------------------------------ views
-- The status the register keeps in a column, derived from the dates that
-- produce it. Order matters: cancelled beats everything, and a line both
-- paid and delivered is complete however it got there.
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
        WHEN l.ordered_date IS NOT NULL THEN 'ordered'
        WHEN l.supplier_po_id IS NOT NULL THEN 'PO raised'
        ELSE 'to be ordered'
    END                                     AS state
FROM procurement_line l
JOIN project p ON p.id = l.project_id
LEFT JOIN supplier s ON s.id = l.supplier_id
LEFT JOIN supplier_po po ON po.id = l.supplier_po_id
LEFT JOIN supplier_quote q ON q.id = l.supplier_quote_id
LEFT JOIN supplier_invoice i ON i.id = l.supplier_invoice_id
LEFT JOIN period pe ON pe.id = l.period_id;

-- What a project has cost, and what is still coming. Cancelled lines are
-- excluded from both: a line nobody will pay for is not a cost.
CREATE VIEW v_project_procurement AS
SELECT
    p.id                                    AS project_id,
    p.entity_id                             AS entity_id,
    COALESCE(SUM(CASE WHEN l.cancelled_date IS NULL
                      THEN l.total_cents END), 0)        AS committed_cents,
    COALESCE(SUM(CASE WHEN l.cancelled_date IS NULL AND l.paid_date IS NOT NULL
                      THEN l.total_cents END), 0)        AS paid_cents,
    COALESCE(SUM(CASE WHEN l.cancelled_date IS NULL AND l.paid_date IS NULL
                      THEN l.total_cents END), 0)        AS outstanding_cents,
    COALESCE(SUM(CASE WHEN l.cancelled_date IS NULL
                           AND l.delivered_date IS NULL
                      THEN l.total_cents END), 0)        AS undelivered_cents,
    COUNT(l.id)                                          AS line_count
FROM project p
LEFT JOIN procurement_line l ON l.project_id = p.id
GROUP BY p.id, p.entity_id;
