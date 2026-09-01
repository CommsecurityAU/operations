-- 022_payroll_role.sql — seeing the costs is not seeing the salaries.
--
-- `finance` opens the office-expense screen: the rent, the subscriptions,
-- the payroll tax, the total cost of running the business. Reporting will
-- need that, and more than one person will have it.
--
-- WHAT PEOPLE EARN IS A DIFFERENT QUESTION. `payroll` is the role for it,
-- and like every other it implies nothing and is implied by nothing --
-- including `finance` and including `admin`. Somebody can see that wages
-- cost $96,250.01 in July without seeing that Justin is on $215,000.
--
-- An administrator grants it, which is what administering is. That an
-- administrator could grant it to themselves is true and is the point of
-- the audit log: the control is that it is RECORDED, not that it is
-- impossible.
--
-- Sixth role, second rebuild of this table. SQLite cannot alter a CHECK.

CREATE TABLE user_entity_role_new (
    user_id     INTEGER NOT NULL REFERENCES users(id),
    entity_id   INTEGER NOT NULL REFERENCES entity(id),
    role        TEXT    NOT NULL CHECK (role IN ('viewer','operations',
                                                 'approver','admin','finance',
                                                 'payroll')),
    granted_by  INTEGER REFERENCES users(id),
    granted_ts  INTEGER NOT NULL,
    PRIMARY KEY (user_id, entity_id, role)
) STRICT;

INSERT INTO user_entity_role_new (user_id, entity_id, role, granted_by, granted_ts)
SELECT user_id, entity_id, role, granted_by, granted_ts FROM user_entity_role;

DROP TABLE user_entity_role;
ALTER TABLE user_entity_role_new RENAME TO user_entity_role;
