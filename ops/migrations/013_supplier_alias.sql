-- 013_supplier_alias.sql — what the register calls a supplier (STP-3).
--
-- EXPAND ONLY.
--
-- The procurement register uses working names, and only four of thirteen
-- match the iTrade supplier list exactly:
--
--     'USR'            -> Jinan USR IOT Technology Limited
--     'Kode Labs'      -> Kodelabs
--     'NaturaLight 3D' -> Natural Light 3D
--     'Colterlec'      -> Colterlec Pty Ltd
--     'Abakus'         -> Abukus Analytics        (spelled two ways)
--     'Eve', 'ICT', 'Kenrone', a mightyape URL    -> not in the list at all
--
-- Fuzzy matching would get `Colterlec` right and `USR` wrong, and a wrong
-- supplier on a purchase order is worse than a missing one -- it puts spend
-- against a company that never sold us anything. So the mapping is
-- RECORDED rather than inferred, once, and the next import matches without
-- asking. Same arrangement as `job_code_alias`.

CREATE TABLE supplier_alias (
    id          INTEGER PRIMARY KEY,
    entity_id   INTEGER NOT NULL REFERENCES entity(id),
    alias       TEXT    NOT NULL,
    supplier_id INTEGER NOT NULL REFERENCES supplier(id),
    note        TEXT,
    created_by  INTEGER REFERENCES users(id),
    created_ts  INTEGER NOT NULL,
    CHECK (length(trim(alias)) > 0)
) STRICT;

CREATE UNIQUE INDEX supplier_alias_unique
    ON supplier_alias (entity_id, alias COLLATE NOCASE);
