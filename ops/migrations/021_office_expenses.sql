-- 021_office_expenses.sql — what the business costs to run (STP-4).
--
-- EXPAND ONLY.
--
-- The Office Expenses sheet is a matrix: a KEY (the category), a line, and
-- eighteen months. Categories are Wages, Superannuation, Leave, Rent &
-- Utilities, Work Cover, Public & Product, Payroll Tax, Licenses &
-- Subscriptions, Expenses, External Services, Test Equipment -- and both
-- categories and lines must be addable, because the list is not finished.
--
-- THREE THINGS THE SHEET ENCODES WITHOUT SAYING SO.
--
-- 1. A WAGE IS ANNUAL SALARY DIVIDED BY TWELVE, and salaries change. Finau
--    goes from $5,833.33 to $7,083.33 in Oct-26, which is $70,000 to
--    $85,000; Joshua from $120,000 to $130,000. Storing eighteen monthly
--    figures loses the salary, and a rise then has to be typed twelve
--    times. So a person has SALARY REVISIONS with an effective month, and
--    the monthly figure is derived.
--
-- 2. STATE MATTERS. One employee is in NSW, and Work Cover and Payroll Tax
--    are both state schemes at different rates: Work Cover 1.785% in VIC
--    against 0.39% under iCare in NSW, payroll tax 4.85% against 5.45%. So
--    a person has a state, and the statutory lines have a rate and a state.
--
-- 3. SOME LINES ARE FORECAST. `Finau (Forecasted)`, `New Employee 1`: a
--    person who has not started, whose cost is real for planning and not
--    yet real for paying. Flagged, not hidden.
--
-- WHAT THIS DOES NOT DO YET. The statutory amounts are stored as the sheet
-- states them, because the BASE those rates apply to is not derivable from
-- the values alone: VIC Work Cover is exactly 1.785% of VIC wages plus VIC
-- super, but VIC payroll tax comes to a constant 1.1237 times that same
-- base and the NSW figures do not track the one NSW employee at all. The
-- rate and the state are recorded so the calculation can be added when the
-- formula is known -- and until then the figures are what the sheet says
-- rather than something invented.

CREATE TABLE expense_category (
    id          INTEGER PRIMARY KEY,
    entity_id   INTEGER NOT NULL REFERENCES entity(id),
    name        TEXT    NOT NULL,
    -- `wages` and `super` drive the statutory bases; the rest are ordinary
    -- costs. Named rather than inferred from the label, because a category
    -- someone renames should not change what it means.
    kind        TEXT    NOT NULL DEFAULT 'expense'
                CHECK (kind IN ('wages','super','statutory','expense')),
    sequence    INTEGER NOT NULL DEFAULT 0,
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_by  INTEGER REFERENCES users(id),
    created_ts  INTEGER NOT NULL,
    CHECK (length(trim(name)) > 0)
) STRICT;

CREATE UNIQUE INDEX expense_category_name
    ON expense_category (entity_id, name COLLATE NOCASE);

CREATE TABLE expense_line (
    id            INTEGER PRIMARY KEY,
    entity_id     INTEGER NOT NULL REFERENCES entity(id),
    category_id   INTEGER NOT NULL REFERENCES expense_category(id),
    name          TEXT    NOT NULL,
    -- Where this person is employed. Null for anything that is not a
    -- person: Work Cover and Payroll Tax are state schemes, and one
    -- employee in NSW changes both.
    state         TEXT CHECK (state IS NULL OR state IN ('VIC','NSW','QLD',
                                                         'SA','WA','TAS',
                                                         'NT','ACT')),
    -- A cost that is real for planning and not yet real for paying.
    is_forecast   INTEGER NOT NULL DEFAULT 0 CHECK (is_forecast IN (0,1)),
    -- Basis points: 1.785% is 178 at two decimals, so rates are held in
    -- HUNDREDTHS of a basis point -- 1.785% is 17850 -- because 0.405% and
    -- 4.85% both matter to the cent and neither survives rounding to whole
    -- basis points.
    rate_bp       INTEGER,

    -- How this line is worked out, or NULL where somebody types it.
    --
    --   percent_of_line     super: 12% of that person's wages
    --   percent_of_state    Work Cover, Payroll Tax (VIC): a rate on
    --                       wages plus super for the line's state
    --   percent_less_annual Payroll Tax (NSW): the same, less a threshold
    --                       expressed per YEAR -- ((base*12 - 47000) *
    --                       rate) / 12
    --
    -- Named rather than an expression, because an expression language is a
    -- thing to get wrong and there are three formulas.
    formula       TEXT CHECK (formula IS NULL OR formula IN
                              ('percent_of_line','percent_of_state',
                               'percent_less_annual')),
    -- What `percent_of_line` reads: a person's super follows their wages.
    basis_line_id INTEGER REFERENCES expense_line(id),
    -- The annual reduction, for `percent_less_annual`. NSW payroll tax is
    -- charged on the year less $47,000.
    threshold_annual_cents INTEGER,
    sequence      INTEGER NOT NULL DEFAULT 0,
    note          TEXT,
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    created_by    INTEGER REFERENCES users(id),
    created_ts    INTEGER NOT NULL,
    CHECK (length(trim(name)) > 0)
) STRICT;

CREATE INDEX expense_line_category ON expense_line (category_id);

CREATE UNIQUE INDEX expense_line_name
    ON expense_line (entity_id, category_id, name COLLATE NOCASE);

-- An annual salary, from a month. A rise is a new row, not an edit: what
-- somebody earned last year is a fact about last year.
CREATE TABLE salary_revision (
    id             INTEGER PRIMARY KEY,
    expense_line_id INTEGER NOT NULL REFERENCES expense_line(id),
    from_period_id INTEGER NOT NULL REFERENCES period(id),
    annual_cents   INTEGER NOT NULL,
    note           TEXT,
    created_by     INTEGER REFERENCES users(id),
    created_ts     INTEGER NOT NULL,
    CHECK (annual_cents >= 0),
    UNIQUE (expense_line_id, from_period_id)
) STRICT;

CREATE INDEX salary_revision_line ON salary_revision (expense_line_id);

-- The monthly figure. Present for every line: derived for wages, entered
-- for everything else. Stored either way, so a month always has a number
-- and nothing has to be recomputed to read a total.
CREATE TABLE expense_amount (
    id             INTEGER PRIMARY KEY,
    expense_line_id INTEGER NOT NULL REFERENCES expense_line(id),
    period_id      INTEGER NOT NULL REFERENCES period(id),
    amount_cents   INTEGER NOT NULL DEFAULT 0,
    -- Where the figure came from. A derived figure is recomputed when the
    -- salary changes; an entered one is never touched.
    source         TEXT    NOT NULL DEFAULT 'entered'
                   CHECK (source IN ('entered','salary','rate')),
    note           TEXT,
    created_by     INTEGER REFERENCES users(id),
    created_ts     INTEGER NOT NULL,
    UNIQUE (expense_line_id, period_id)
) STRICT;

CREATE INDEX expense_amount_period ON expense_amount (period_id);

-- Keyed on the LINE and the MONTH, not on the amount row.
--
-- Clearing a month DELETES its row -- a month a line does not run in should
-- be absent, not zero -- and a revision pointing at the row it records the
-- removal of cannot survive that. The line and the month are the identity;
-- the row holding the figure is not.
CREATE TABLE expense_amount_revision (
    id              INTEGER PRIMARY KEY,
    expense_line_id INTEGER NOT NULL REFERENCES expense_line(id),
    period_id       INTEGER NOT NULL REFERENCES period(id),
    old_cents       INTEGER,
    new_cents       INTEGER,
    reason          TEXT,
    changed_by      INTEGER REFERENCES users(id),
    changed_ts      INTEGER NOT NULL
) STRICT;

CREATE INDEX expense_amount_revision_line
    ON expense_amount_revision (expense_line_id, period_id);

-- ------------------------------------------------------------------ views
CREATE VIEW v_expense_line AS
SELECT
    l.id                                    AS line_id,
    l.entity_id                             AS entity_id,
    l.name                                  AS line_name,
    l.state                                 AS state,
    l.is_forecast                           AS is_forecast,
    l.rate_bp                               AS rate_bp,
    l.formula                               AS formula,
    l.basis_line_id                         AS basis_line_id,
    l.threshold_annual_cents                AS threshold_annual_cents,
    -- Read by the edit dialog. A field the screen shows and the view omits
    -- comes back blank and is saved as blank, which erases it silently.
    l.note                                  AS note,
    l.sequence                              AS line_sequence,
    l.is_active                             AS is_active,
    c.id                                    AS category_id,
    c.name                                  AS category_name,
    c.kind                                  AS category_kind,
    c.sequence                              AS category_sequence,
    -- What this line is currently paid, where it is a salary.
    (SELECT r.annual_cents FROM salary_revision r
     JOIN period p ON p.id = r.from_period_id
     WHERE r.expense_line_id = l.id
     ORDER BY p.month_start DESC LIMIT 1)   AS annual_cents
FROM expense_line l
JOIN expense_category c ON c.id = l.category_id;

CREATE VIEW v_expense_month AS
SELECT
    a.period_id                             AS period_id,
    pe.label                                AS period_label,
    pe.fy                                   AS fy,
    pe.fy_label                             AS fy_label,
    pe.month_start                          AS month_start,
    l.entity_id                             AS entity_id,
    l.id                                    AS line_id,
    l.name                                  AS line_name,
    l.state                                 AS state,
    l.is_forecast                           AS is_forecast,
    c.id                                    AS category_id,
    c.name                                  AS category_name,
    c.kind                                  AS category_kind,
    a.amount_cents                          AS amount_cents,
    a.source                                AS source
FROM expense_amount a
JOIN expense_line l ON l.id = a.expense_line_id
JOIN expense_category c ON c.id = l.category_id
JOIN period pe ON pe.id = a.period_id;

-- The base the statutory rates apply to, by state and month: wages plus
-- super for that state. Every one of the four statutory lines uses it, and
-- the two Work Cover rates and VIC payroll tax are a straight percentage of
-- it. NSW payroll tax is the same base less $47,000 a year.
CREATE VIEW v_wage_base AS
SELECT
    a.period_id                             AS period_id,
    l.entity_id                             AS entity_id,
    COALESCE(l.state, 'VIC')                AS state,
    SUM(CASE WHEN c.kind = 'wages' THEN a.amount_cents ELSE 0 END)
                                            AS wages_cents,
    SUM(CASE WHEN c.kind = 'super' THEN a.amount_cents ELSE 0 END)
                                            AS super_cents,
    SUM(a.amount_cents)                     AS base_cents
FROM expense_amount a
JOIN expense_line l ON l.id = a.expense_line_id
JOIN expense_category c ON c.id = l.category_id
WHERE c.kind IN ('wages', 'super')
GROUP BY a.period_id, l.entity_id, COALESCE(l.state, 'VIC');
