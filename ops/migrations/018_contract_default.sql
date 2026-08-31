-- 018_contract_default.sql — keep the two contract columns in step.
--
-- EXPAND ONLY.
--
-- Caught by the N-1 gate, which is what it is for.
--
-- Migration `007` made `project.contract_value_cents` authoritative and
-- backfilled it from the `customer_po` rows that migration `003` had
-- created from the register's `Purchase Order` column (ADR-34). That
-- backfill ran once, over the data that existed then.
--
-- The PREVIOUS RELEASE's register importer writes `purchase_order_cents`
-- and creates no `customer_po` rows at all. A project it inserts against
-- this schema therefore has a contract value of ZERO, and every figure
-- derived from it reads zero: the gate saw `0 != 352004173`.
--
-- That is not only a test artefact. Roll back to the previous release,
-- create a project, roll forward, and that project has no contract.
--
-- So the schema defends itself. `purchase_order_cents` in the register
-- ALWAYS meant contract value, so a row arriving with one and not the
-- other gets the obvious answer rather than a zero. It never overwrites a
-- contract that was set: the trigger fires only when there is nothing to
-- lose.

CREATE TRIGGER project_contract_default_insert
AFTER INSERT ON project
WHEN NEW.contract_value_cents = 0 AND NEW.purchase_order_cents <> 0
BEGIN
    UPDATE project SET contract_value_cents = NEW.purchase_order_cents
    WHERE id = NEW.id;
END;

-- The same on update, because the previous release edits a project by
-- writing `purchase_order_cents` alone.
CREATE TRIGGER project_contract_default_update
AFTER UPDATE OF purchase_order_cents ON project
WHEN NEW.contract_value_cents = 0 AND NEW.purchase_order_cents <> 0
BEGIN
    UPDATE project SET contract_value_cents = NEW.purchase_order_cents
    WHERE id = NEW.id;
END;

-- Anything already in this state, from a rollback that has already
-- happened.
UPDATE project SET contract_value_cents = purchase_order_cents
WHERE contract_value_cents = 0 AND purchase_order_cents <> 0;
