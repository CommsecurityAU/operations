-- 011_suppliers.sql — who we buy from (STP-3).
--
-- EXPAND ONLY.
--
-- The starting authority is the iTrade supplier list: 92 names with an
-- `ID#` that means something outside this system. Xero has its own
-- reference and is being brought up to date as we go, so `xero_ref` exists
-- and is empty -- filled in when Xero becomes the authority, and until then
-- the platform reconciles against iTrade the way it does the register.
--
-- CURRENCY belongs to the supplier as a DEFAULT, and to the purchase order
-- as the fact. Two suppliers invoice in USD today -- Kodelabs and Jinan USR
-- IOT -- and any of the others may tomorrow, so the default is a
-- convenience rather than a constraint: a PO carries its own currency and
-- its own rate.
--
-- ABN, address and payment terms are nullable because they are being
-- gathered. A supplier with no ABN is withheld at 47% under the no-ABN
-- rule, so the column exists now rather than being retrofitted when
-- somebody notices the withholding.

CREATE TABLE supplier (
    id             INTEGER PRIMARY KEY,
    entity_id      INTEGER NOT NULL REFERENCES entity(id),
    name           TEXT    NOT NULL,
    -- The iTrade `ID#`. Not the platform's own id: it means something to
    -- people outside this system, which is exactly why it is kept.
    itrade_ref     TEXT,
    -- Filled in when Xero's list is current. Nullable for as long as that
    -- takes, which is the honest state rather than a guess.
    xero_ref       TEXT,
    abn            TEXT,
    default_currency TEXT NOT NULL DEFAULT 'AUD'
                     CHECK (default_currency IN ('AUD','USD')),
    -- Days from invoice to payment. Null until known: an invented figure
    -- would put a date in a cash forecast that nobody agreed to.
    payment_terms_days INTEGER,
    contact_name   TEXT,
    phone          TEXT,
    email          TEXT,
    address        TEXT,
    note           TEXT,
    is_active      INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_by     INTEGER REFERENCES users(id),
    created_ts     INTEGER NOT NULL,
    CHECK (length(trim(name)) > 0),
    CHECK (payment_terms_days IS NULL OR payment_terms_days >= 0)
) STRICT;

-- Names are how people find a supplier, and two rows for one company is how
-- spend gets split in half without anyone noticing. Enforced per entity:
-- separate companies may legitimately buy from the same supplier.
CREATE UNIQUE INDEX supplier_name_unique
    ON supplier (entity_id, name COLLATE NOCASE);

CREATE UNIQUE INDEX supplier_itrade_unique
    ON supplier (entity_id, itrade_ref) WHERE itrade_ref IS NOT NULL;

CREATE INDEX supplier_active ON supplier (entity_id, is_active);

-- Changes worth keeping: a currency or an ABN that turns out to have been
-- wrong changes what was withheld and what was paid.
CREATE TABLE supplier_revision (
    id           INTEGER PRIMARY KEY,
    supplier_id  INTEGER NOT NULL REFERENCES supplier(id),
    field        TEXT    NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    reason       TEXT,
    changed_by   INTEGER REFERENCES users(id),
    changed_ts   INTEGER NOT NULL
) STRICT;

CREATE INDEX supplier_revision_supplier ON supplier_revision (supplier_id);
