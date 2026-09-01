-- 020_finance_role.sql — a role for money nobody else should see (STP-4).
--
-- Office expenses are wages. Justin's salary, Richard's salary, everyone's
-- superannuation: the four existing roles all imply seeing the project
-- register, and there is no reason a project engineer entering procurement
-- should also see what their colleagues earn.
--
-- So `finance` is a fifth role and it implies NOTHING. No role implies
-- another (§9), which is what makes adding one safe: nobody gains it by
-- having something else, including admin. An administrator can GRANT it --
-- that is what administering is -- but granting is not having.
--
-- `role` is a CHECK constraint and SQLite cannot alter one, so the table is
-- rebuilt. Every row is copied first.

CREATE TABLE user_entity_role_new (
    user_id     INTEGER NOT NULL REFERENCES users(id),
    entity_id   INTEGER NOT NULL REFERENCES entity(id),
    role        TEXT    NOT NULL CHECK (role IN ('viewer','operations',
                                                 'approver','admin','finance')),
    granted_by  INTEGER REFERENCES users(id),
    granted_ts  INTEGER NOT NULL,
    PRIMARY KEY (user_id, entity_id, role)
) STRICT;

INSERT INTO user_entity_role_new (user_id, entity_id, role, granted_by, granted_ts)
SELECT user_id, entity_id, role, granted_by, granted_ts FROM user_entity_role;

DROP TABLE user_entity_role;
ALTER TABLE user_entity_role_new RENAME TO user_entity_role;
