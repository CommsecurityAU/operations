"""Office expenses (STP-4) — and who may see them.

These figures are wages. `finance` implies nothing and is implied by
nothing, including admin: there is no reason a project engineer entering
procurement should also see what colleagues earn.
"""

import http.client
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from ops import auth  # noqa: E402
from ops.config import Config  # noqa: E402
from ops.db import Db  # noqa: E402
from ops.main import boot  # noqa: E402
from ops.secrets import LocalProvider  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "office_expenses_fy27.csv")


class Case(unittest.TestCase):
    roles = ("viewer", "finance", "payroll")
    imported = True

    def setUp(self):
        for n in ("ops.http", "ops.main", "ops.auth"):
            logging.getLogger(n).setLevel(logging.CRITICAL)
        self.dir = tempfile.mkdtemp()
        secrets = os.path.join(self.dir, "secrets", "store.json")
        LocalProvider(secrets).set("OIDC_CLIENT_SECRET", "x")
        cfg = Config(data_dir=self.dir, tls=False, port=0,
                     oidc_client_id="cid", oidc_redirect_uri="http://x/cb")
        self.db, self.server, self.sched = boot(
            cfg=cfg, env={"OPS_SECRETS_PATH": secrets}, serve=False)
        self.port = self.server.server_address[1]
        self.t = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
            daemon=True)
        self.t.start()
        self.key = auth.load_or_create_key(cfg.session_key_path)
        self.user = auth.sign_in(self.db, {"sub": "s1", "email": "r@x",
                                           "name": "R"})
        for role in self.roles:
            self.db.grant_role(self.user["id"], 1, role, self.user["id"])
        if self.imported:
            # NOT `self.db.close()` first: the running server is using that
            # connection, and closing it made every later request fail with
            # `Cannot operate on a closed database` -- a 500 that looked
            # like a module fault for twenty minutes. The importer opens its
            # own connection; WAL lets both write.
            subprocess.run(
                [sys.executable, os.path.join(ROOT, "tools", "import_expenses.py"),
                 "--db", os.path.join(self.dir, "ops.db"), "--csv", FIXTURE,
                 "--nsw", "Justin Anders", "--apply"],
                capture_output=True, check=True)

    def tearDown(self):
        self.sched.stop()
        self.server.shutdown()
        self.server.server_close()
        self.t.join(timeout=5)
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    #: Whether this case has re-authenticated. Salaries are withheld from
    #: the response without it, so most tests run WITHOUT and the ones that
    #: need a salary say so.
    elevated = True

    def call(self, method, path, body=None, elevated=None):
        token = auth.mint_session(self.key, self.user["id"],
                                  self.user["token_version"])
        cookies = [f"{auth.COOKIE_NAME}={token}"]
        want = self.elevated if elevated is None else elevated
        if want:
            cookies.append(
                f"{auth.ELEVATION_COOKIE}="
                + auth.mint_elevation(self.key, self.user["id"]))
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path,
                  body=None if body is None else json.dumps(body).encode(),
                  headers={"Content-Type": "application/json",
                           "Sec-Fetch-Site": "same-origin",
                           "Cookie": "; ".join(cookies)})
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, (json.loads(raw) if raw else None)

    def overview(self):
        status, body = self.call("GET", "/api/expenses")
        self.assertEqual(status, 200)
        return body

    def period(self, label):
        return self.db.scalar("SELECT id FROM period WHERE label = ?", (label,))

    def line_named(self, name):
        return self.db.query_one(
            "SELECT * FROM v_expense_line WHERE line_name = ?", (name,))


class TestTheImport(Case):
    def test_the_whole_matrix_lands(self):
        body = self.overview()
        # Thirteen, not eleven: Work Cover and Payroll Tax each split by
        # state.
        self.assertEqual(len(body["categories"]), 13)
        self.assertEqual(len(body["lines"]), 54)
        self.assertEqual(len(body["amounts"]), 684)

    def test_a_salary_is_recovered_from_the_monthly_figures(self):
        """`Finau` at $5,833.33 a month is $70,000 a year. Storing eighteen
        monthly figures loses the salary, and a rise then has to be typed
        twelve times."""
        line = self.line_named("Finau (Forecasted)")
        self.assertEqual(line["annual_cents"], 8500000)   # after the Oct rise

    def test_a_rise_is_recorded_as_a_revision(self):
        line = self.line_named("Finau (Forecasted)")
        revisions = self.db.query(
            """SELECT r.annual_cents, pe.label FROM salary_revision r
               JOIN period pe ON pe.id = r.from_period_id
               WHERE r.expense_line_id = ? ORDER BY pe.month_start""",
            (line["line_id"],))
        self.assertEqual([(r["label"], r["annual_cents"]) for r in revisions],
                         [("Jul-26", 7000000), ("Oct-26", 8500000)])

    def test_the_nsw_employee_is_flagged(self):
        """Work Cover and Payroll Tax are state schemes at different rates,
        so one employee in NSW changes both."""
        self.assertEqual(self.line_named("Justin Anders")["state"], "NSW")
        self.assertEqual(self.line_named("Richard Roberts")["state"], "VIC")

    def test_forecast_people_are_flagged_not_hidden(self):
        """A cost that is real for planning and not yet real for paying."""
        self.assertEqual(self.line_named("Finau (Forecasted)")["is_forecast"], 1)
        self.assertEqual(self.line_named("Matthew Parnell")["is_forecast"], 0)

    def test_a_statutory_rate_is_stored_exactly(self):
        """Hundredths of a basis point, so 1.785% and 0.405% both survive:
        rounding to whole basis points loses a cent a month on one and a
        tenth of the charge on the other."""
        self.assertEqual(self.line_named("Work Cover 1.785%")["rate_bp"], 17850)
        self.assertEqual(
            self.line_named("Payroll Tax (VIC) 4.85%")["rate_bp"], 48500)
        self.assertEqual(
            self.line_named("Work Cover NSW (iCare) 0.39%")["rate_bp"], 3900)

    def test_the_nsw_work_cover_label_was_the_correct_one(self):
        """The line is called `0.39%` and the sheet computed 0.405%.
        Confirmed with the Ops Manager: the LABEL is right, so $81.27 a
        month becomes $78.26.

        Worth having asked. Everywhere else this week the figures were the
        fact and the label described intent -- the legend cell reading
        `#F26722` while the flags were `#FF9900` (ADR-44), the `Phase`
        column meaning three different things. Here it was the other way
        round, and assuming the usual direction would have carried a wrong
        rate forward."""
        line = self.line_named("Work Cover NSW (iCare) 0.39%")
        self.assertEqual(line["rate_bp"], Db.rate(0.39))

    def test_lines_with_no_figures_still_exist(self):
        """`New Employee 2` has no numbers yet and needs somewhere to put
        them."""
        self.assertIsNotNone(self.line_named("New Employee 2"))

    def test_the_vic_work_cover_base_is_wages_plus_super(self):
        """The one statutory base that IS derivable: $87,733.34 at 1.785%
        is the $1,566.04 the sheet states."""
        base = self.db.query_one(
            """SELECT base_cents FROM v_wage_base
               WHERE state = 'VIC' AND period_id = ?""", (self.period("Jul-26"),))
        self.assertEqual(base["base_cents"], 8773334)

    def test_running_it_twice_is_refused(self):
        result = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "import_expenses.py"),
             "--db", os.path.join(self.dir, "ops.db"), "--csv", FIXTURE,
             "--apply"], capture_output=True)
        self.assertEqual(result.returncode, 2)


class TestTheStatesAreSeparateCategories(Case):
    """Work Cover and Payroll Tax are each two obligations, under two
    schemes, to two insurers. Grouped together the header is the sum of a
    VIC charge and an NSW one — a number nobody asks for."""

    def category_of(self, line_name):
        return self.db.scalar(
            """SELECT c.name FROM expense_line l
               JOIN expense_category c ON c.id = l.category_id
               WHERE l.name = ?""", (line_name,))

    def test_work_cover_splits(self):
        self.assertEqual(self.category_of("Work Cover 1.785%"),
                         "Work Cover (VIC)")
        self.assertEqual(self.category_of("Work Cover NSW (iCare) 0.39%"),
                         "Work Cover (NSW)")

    def test_payroll_tax_splits(self):
        self.assertEqual(self.category_of("Payroll Tax (VIC) 4.85%"),
                         "Payroll Tax (VIC)")
        self.assertEqual(self.category_of("Payroll Tax (NSW) 5.45"),
                         "Payroll Tax (NSW)")

    def test_each_still_drives_the_statutory_base(self):
        """Splitting the CATEGORY must not change what it is: the kind is
        read from the sheet's own key, not from the name it is given
        here."""
        for name in ("Work Cover (VIC)", "Work Cover (NSW)",
                     "Payroll Tax (VIC)", "Payroll Tax (NSW)"):
            self.assertEqual(self.db.scalar(
                "SELECT kind FROM expense_category WHERE name = ?", (name,)),
                "statutory", name)

    def test_a_group_header_is_the_sum_of_its_own_lines(self):
        self.db.recompute_derived(1, self.user["id"])
        for category, expected in (("Work Cover (VIC)", 156604),
                                   ("Work Cover (NSW)", 7826),
                                   ("Payroll Tax (VIC)", 425507)):
            self.assertEqual(self.db.scalar(
                """SELECT SUM(a.amount_cents) FROM expense_amount a
                   JOIN expense_line l ON l.id = a.expense_line_id
                   JOIN expense_category c ON c.id = l.category_id
                   JOIN period p ON p.id = a.period_id
                   WHERE c.name = ? AND p.label = 'Jul-26'""",
                (category,)), expected, category)


class TestSalariesDriveMonths(Case):
    def test_a_raise_recomputes_every_month_after_it(self):
        """A salary is the fact and the months are its consequence."""
        line = self.line_named("Finau (Forecasted)")
        status, body = self.call("POST", "/api/expenses/salaries", {
            "line_id": line["line_id"], "from_period_id": self.period("Oct-26"),
            "annual_cents": 9500000})
        self.assertEqual(status, 200)
        self.assertGreater(body["months_updated"], 12)
        self.assertEqual(self.db.scalar(
            """SELECT amount_cents FROM expense_amount
               WHERE expense_line_id = ? AND period_id = ?""",
            (line["line_id"], self.period("Nov-26"))), 791667)

    def test_months_before_it_are_untouched(self):
        line = self.line_named("Finau (Forecasted)")
        self.call("POST", "/api/expenses/salaries", {
            "line_id": line["line_id"], "from_period_id": self.period("Oct-26"),
            "annual_cents": 9500000})
        self.assertEqual(self.db.scalar(
            """SELECT amount_cents FROM expense_amount
               WHERE expense_line_id = ? AND period_id = ?""",
            (line["line_id"], self.period("Sep-26"))), 583333)

    def test_a_typed_figure_is_not_overwritten(self):
        """Somebody typed that on purpose."""
        line = self.line_named("Finau (Forecasted)")
        self.call("POST", "/api/expenses/amounts", {
            "line_id": line["line_id"], "period_id": self.period("Nov-26"),
            "amount_cents": 100})
        self.call("POST", "/api/expenses/salaries", {
            "line_id": line["line_id"], "from_period_id": self.period("Oct-26"),
            "annual_cents": 9500000})
        self.assertEqual(self.db.scalar(
            """SELECT amount_cents FROM expense_amount
               WHERE expense_line_id = ? AND period_id = ?""",
            (line["line_id"], self.period("Nov-26"))), 100)

    def test_a_salary_on_a_non_wage_line_is_refused(self):
        status, _b = self.call("POST", "/api/expenses/salaries", {
            "line_id": self.line_named("Xero")["line_id"],
            "from_period_id": self.period("Jul-26"), "annual_cents": 100})
        self.assertEqual(status, 409)


class TestTheDerivedFigures(Case):
    """Four formulas, given by the Ops Manager and verified against the
    sheet in every month:

        super                 12% of that person's wages
        Work Cover VIC        (wages + super) x 1.785%
        Work Cover NSW        (wages + super) x 0.405%
        Payroll Tax VIC       (wages + super) x 4.85%
        Payroll Tax NSW       ((wages + super) x 12 - $47,000) x 5.45% / 12
    """

    def value(self, name, label):
        return self.db.scalar(
            """SELECT a.amount_cents FROM expense_amount a
               JOIN expense_line l ON l.id = a.expense_line_id
               JOIN period p ON p.id = a.period_id
               WHERE l.name = ? AND p.label = ?""", (name, label))

    def test_the_rate_helper_is_exact(self):
        """Written by hand, `0.405%` became `405_00` and an $81.27 charge
        came out at $812.70. Underscores group digits; they do not check
        them."""
        for percent, expected in ((12, 120000), (1.785, 17850),
                                  (0.405, 4050), (4.85, 48500),
                                  (5.45, 54500)):
            self.assertEqual(Db.rate(percent), expected, str(percent))

    def test_recomputing_reproduces_the_sheet(self):
        self.db.recompute_derived(1, self.user["id"])
        for name, label, expected in (
                ("Work Cover 1.785%", "Jul-26", 156604),
                ("Work Cover 1.785%", "Oct-26", 185759),
                # 0.39%, not the 0.405% the sheet used.
                ("Work Cover NSW (iCare) 0.39%", "Jul-26", 7826),
                ("Payroll Tax (VIC) 4.85%", "Jul-26", 425507),
                ("Payroll Tax (NSW) 5.45", "Jul-26", 88018)):
            self.assertEqual(self.value(name, label), expected,
                             f"{name} {label}")

    def test_it_corrects_the_stale_payroll_tax(self):
        """The sheet's VIC payroll tax froze at $4,255.07 while wages rose
        in Oct-26 -- $792.16 a month, $16,635.36 across the two years it
        covers. A figure that has to be dragged across a row by hand is a
        figure that eventually is not."""
        self.assertEqual(self.value("Payroll Tax (VIC) 4.85%", "Oct-26"), 425507)
        self.db.recompute_derived(1, self.user["id"])
        self.assertEqual(self.value("Payroll Tax (VIC) 4.85%", "Oct-26"), 504723)

    def test_super_follows_the_persons_own_wages(self):
        self.db.recompute_derived(1, self.user["id"])
        line = self.line_named("Finau (Forecasted)")
        self.call("POST", "/api/expenses/salaries", {
            "line_id": line["line_id"], "from_period_id": self.period("Jul-26"),
            "annual_cents": 12000000})
        self.db.recompute_derived(1, self.user["id"])
        # $120,000 a year is $10,000 a month; 12% of that is $1,200.
        self.assertEqual(self.db.scalar(
            """SELECT a.amount_cents FROM expense_amount a
               JOIN expense_line l ON l.id = a.expense_line_id
               JOIN expense_category c ON c.id = l.category_id
               JOIN period p ON p.id = a.period_id
               WHERE c.kind = 'super' AND l.name = ? AND p.label = 'Jul-26'""",
            ("Finau (Forecasted)",)), 120000)

    def test_a_raise_moves_the_statutory_charges_with_it(self):
        """The whole point: nothing has to be dragged across a row."""
        self.db.recompute_derived(1, self.user["id"])
        before = self.value("Work Cover 1.785%", "Jul-26")
        line = self.line_named("Matthew Parnell")
        self.call("POST", "/api/expenses/salaries", {
            "line_id": line["line_id"], "from_period_id": self.period("Jul-26"),
            "annual_cents": 24000000})
        self.db.recompute_derived(1, self.user["id"])
        self.assertGreater(self.value("Work Cover 1.785%", "Jul-26"), before)

    def test_a_typed_figure_survives_a_recompute(self):
        """Somebody typed that on purpose."""
        line = self.line_named("Work Cover 1.785%")
        self.call("POST", "/api/expenses/amounts", {
            "line_id": line["line_id"], "period_id": self.period("Jul-26"),
            "amount_cents": 111})
        self.db.recompute_derived(1, self.user["id"])
        self.assertEqual(self.value("Work Cover 1.785%", "Jul-26"), 111)

    def test_recomputing_twice_changes_nothing(self):
        self.db.recompute_derived(1, self.user["id"])
        self.assertEqual(self.db.recompute_derived(1, self.user["id"]), 0)

    def test_the_nsw_threshold_is_annual_not_monthly(self):
        """$47,000 comes off the YEAR, so the monthly charge is
        (base x 12 - 47,000) x 5.45% / 12 rather than (base - 47,000) x
        5.45%, which would be negative."""
        self.db.recompute_derived(1, self.user["id"])
        self.assertEqual(self.value("Payroll Tax (NSW) 5.45", "Jul-26"), 88018)


class TestRatesChange(Case):
    """Work Cover is reassessed yearly and payroll tax moves with the state
    budget, so the rate is editable — and changing it has to reach the
    months, or it is a rate nobody has changed."""

    def value(self, name, label):
        return self.db.scalar(
            """SELECT a.amount_cents FROM expense_amount a
               JOIN expense_line l ON l.id = a.expense_line_id
               JOIN period p ON p.id = a.period_id
               WHERE l.name = ? AND p.label = ?""", (name, label))

    def test_a_new_rate_reaches_every_month(self):
        self.db.recompute_derived(1, self.user["id"])
        line = self.line_named("Work Cover 1.785%")
        status, body = self.call(
            "PATCH", f"/api/expenses/lines/{line['line_id']}",
            {"rate_percent": 2.0})
        self.assertEqual(status, 200)
        self.assertGreater(body["recomputed"], 20)
        # 2% of $87,733.34
        self.assertEqual(self.value("Work Cover 1.785%", "Jul-26"), 175467)

    def test_the_percentage_is_stored_exactly(self):
        line = self.line_named("Payroll Tax (VIC) 4.85%")
        self.call("PATCH", f"/api/expenses/lines/{line['line_id']}",
                  {"rate_percent": 4.95})
        self.assertEqual(
            self.line_named("Payroll Tax (VIC) 4.85%")["rate_bp"],
            Db.rate(4.95))

    def test_a_nonsense_rate_is_refused(self):
        line = self.line_named("Work Cover 1.785%")
        for bad in ("half", -1, 250):
            status, body = self.call(
                "PATCH", f"/api/expenses/lines/{line['line_id']}",
                {"rate_percent": bad})
            self.assertEqual(status, 400, bad)
            self.assertIn("rate_percent", body["detail"])

    def test_the_nsw_threshold_can_change(self):
        self.db.recompute_derived(1, self.user["id"])
        before = self.value("Payroll Tax (NSW) 5.45", "Jul-26")
        line = self.line_named("Payroll Tax (NSW) 5.45")
        self.call("PATCH", f"/api/expenses/lines/{line['line_id']}",
                  {"threshold_annual_cents": 10000000})
        self.assertLess(self.value("Payroll Tax (NSW) 5.45", "Jul-26"), before)

    def test_a_raise_moves_everything_downstream_at_once(self):
        """Super follows the wage and the statutory charges follow both, so
        a raise moves five other lines without anyone remembering to."""
        self.db.recompute_derived(1, self.user["id"])
        before = self.value("Work Cover 1.785%", "Jun-27")
        line = self.line_named("Matthew Parnell")
        status, body = self.call("POST", "/api/expenses/salaries", {
            "line_id": line["line_id"], "from_period_id": self.period("Jul-26"),
            "annual_cents": 24000000})
        self.assertEqual(status, 200)
        self.assertGreater(body["recomputed"], 0)
        self.assertGreater(self.value("Work Cover 1.785%", "Jun-27"), before)


class TestAddingCategoriesAndLines(Case):
    def test_a_category_can_be_added(self):
        status, body = self.call("POST", "/api/expenses/categories",
                                 {"name": "Insurance"})
        self.assertEqual(status, 201)
        self.assertEqual(body["kind"], "expense")

    def test_a_duplicate_category_is_refused(self):
        status, body = self.call("POST", "/api/expenses/categories",
                                 {"name": "Wages"})
        self.assertEqual(status, 409)
        self.assertIn("already", body["error"])

    def test_a_line_can_be_added_and_given_a_figure(self):
        _s, category = self.call("POST", "/api/expenses/categories",
                                 {"name": "Insurance"})
        _s, line = self.call("POST", "/api/expenses/lines",
                             {"category_id": category["id"],
                              "name": "Cyber cover"})
        status, body = self.call("POST", "/api/expenses/amounts",
                                 {"line_id": line["line_id"],
                                  "period_id": self.period("Jul-26"),
                                  "amount_cents": 150000})
        self.assertEqual(status, 200)
        self.assertEqual(body["amount_cents"], 150000)

    def test_setting_an_amount_to_nothing_removes_it(self):
        """A month a line does not run in should be absent, not zero."""
        line = self.line_named("Xero")
        status, body = self.call("POST", "/api/expenses/amounts",
                                 {"line_id": line["line_id"],
                                  "period_id": self.period("Jul-26"),
                                  "amount_cents": 0})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["removed"], body)
        self.assertIsNone(self.db.query_one(
            """SELECT id FROM expense_amount
               WHERE expense_line_id = ? AND period_id = ?""",
            (line["line_id"], self.period("Jul-26"))))

    def test_a_line_can_move_category(self):
        _s, category = self.call("POST", "/api/expenses/categories",
                                 {"name": "Insurance"})
        line = self.line_named("Public and Product")
        status, _b = self.call("PATCH", f"/api/expenses/lines/{line['line_id']}",
                               {"category_id": category["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.db.scalar(
            "SELECT category_id FROM expense_line WHERE id = ?",
            (line["line_id"],)), category["id"])

    def test_a_state_can_be_corrected(self):
        line = self.line_named("Matthew Parnell")
        self.call("PATCH", f"/api/expenses/lines/{line['line_id']}",
                  {"state": "NSW"})
        self.assertEqual(self.line_named("Matthew Parnell")["state"], "NSW")

    def test_an_unknown_state_is_refused(self):
        line = self.line_named("Matthew Parnell")
        status, body = self.call(
            "PATCH", f"/api/expenses/lines/{line['line_id']}",
            {"state": "Victoria"})
        self.assertEqual(status, 400)
        self.assertIn("state", body["detail"])


class TestSalariesNeverLeaveWithoutAReAuthentication(Case):
    """Hiding a figure in the interface hides it from nobody who matters: a
    salary the browser is sent is a salary anyone with the developer tools
    already has. So it is WITHHELD from the response.

    There is no password in this platform -- sign-in is Google -- so
    demanding one means demanding a fresh Google authentication, which
    `prompt=login` gets.
    """

    def test_the_list_omits_salaries(self):
        status, body = self.call("GET", "/api/expenses", elevated=False)
        self.assertEqual(status, 200)
        self.assertFalse(body["elevated"])
        for line in body["lines"]:
            self.assertNotIn("annual_cents", line)

    def test_it_still_says_which_lines_have_one(self):
        """So the screen can offer to reveal a salary without knowing what
        it is."""
        _s, body = self.call("GET", "/api/expenses", elevated=False)
        self.assertGreater(len(body["salaried"]), 5)

    def test_the_revision_history_is_withheld_too(self):
        """It is the salary, month by month."""
        _s, body = self.call("GET", "/api/expenses", elevated=False)
        self.assertEqual(body["salaries"], [])

    def test_with_a_live_elevation_they_are_included(self):
        _s, body = self.call("GET", "/api/expenses", elevated=True)
        self.assertTrue(body["elevated"])
        self.assertTrue(any("annual_cents" in l for l in body["lines"]))

    def test_one_salary_needs_the_elevation(self):
        line = self.line_named("Richard Roberts")
        status, body = self.call(
            "GET", f"/api/expenses/salary/{line['line_id']}", elevated=False)
        self.assertEqual(status, 403)
        self.assertIn("elevate", body["detail"])

    def test_with_it_the_salary_and_its_history_come_back(self):
        line = self.line_named("Richard Roberts")
        status, body = self.call(
            "GET", f"/api/expenses/salary/{line['line_id']}", elevated=True)
        self.assertEqual(status, 200)
        self.assertEqual(body["annual_cents"], 21500000)
        self.assertTrue(body["revisions"])

    def test_looking_is_recorded(self):
        """A control that leaves no trace is a control nobody can check was
        working."""
        line = self.line_named("Richard Roberts")
        self.call("GET", f"/api/expenses/salary/{line['line_id']}")
        self.assertIsNotNone(self.db.query_one(
            "SELECT id FROM audit_log WHERE action = 'salary_view'"))

    def test_setting_a_salary_needs_it_as_well(self):
        line = self.line_named("Finau (Forecasted)")
        status, _b = self.call("POST", "/api/expenses/salaries", {
            "line_id": line["line_id"], "from_period_id": self.period("Jul-26"),
            "annual_cents": 9000000}, elevated=False)
        self.assertEqual(status, 403)

    def test_an_elevation_for_someone_else_does_not_count(self):
        other = auth.sign_in(self.db, {"sub": "s2", "email": "j@x",
                                       "name": "J"})
        token = auth.mint_session(self.key, self.user["id"],
                                  self.user["token_version"])
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/expenses",
                  headers={"Sec-Fetch-Site": "same-origin",
                           "Cookie": f"{auth.COOKIE_NAME}={token}; "
                                     f"{auth.ELEVATION_COOKIE}="
                                     + auth.mint_elevation(self.key,
                                                           other["id"])})
        r = c.getresponse()
        body = json.loads(r.read())
        c.close()
        self.assertFalse(body["elevated"])

    def test_an_expired_elevation_does_not_count(self):
        stale = auth.mint_elevation(self.key, self.user["id"],
                                    now=1, ttl=1)
        self.assertFalse(
            auth.verify_elevation(self.key, stale, self.user["id"]))

    def test_a_tampered_elevation_does_not_count(self):
        good = auth.mint_elevation(self.key, self.user["id"])
        body, sig = good.split(".")
        self.assertFalse(
            auth.verify_elevation(self.key, f"{body}x.{sig}",
                                  self.user["id"]))


class TestTheExport(Case):
    def test_it_comes_back_as_csv(self):
        token = auth.mint_session(self.key, self.user["id"],
                                  self.user["token_version"])
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/expenses/export",
                  headers={"Sec-Fetch-Site": "same-origin",
                           "Cookie": f"{auth.COOKIE_NAME}={token}"})
        r = c.getresponse()
        text = r.read().decode("utf-8-sig")
        c.close()
        self.assertEqual(r.status, 200)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        self.assertIn("Category,Line,State", text)
        self.assertIn("Work Cover (VIC)", text)
        self.assertIn("Jul-26", text)

    def test_it_leaves_salaries_out_without_an_elevation(self):
        """An export is the easiest way for a figure to leave the
        building."""
        token = auth.mint_session(self.key, self.user["id"],
                                  self.user["token_version"])
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/expenses/export",
                  headers={"Sec-Fetch-Site": "same-origin",
                           "Cookie": f"{auth.COOKIE_NAME}={token}"})
        text = c.getresponse().read().decode("utf-8-sig")
        c.close()
        self.assertNotIn("Annual salary", text)

    def test_with_an_elevation_it_includes_them(self):
        token = auth.mint_session(self.key, self.user["id"],
                                  self.user["token_version"])
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/expenses/export",
                  headers={"Sec-Fetch-Site": "same-origin",
                           "Cookie": f"{auth.COOKIE_NAME}={token}; "
                                     f"{auth.ELEVATION_COOKIE}="
                                     + auth.mint_elevation(self.key,
                                                           self.user["id"])})
        text = c.getresponse().read().decode("utf-8-sig")
        c.close()
        self.assertIn("Annual salary", text)
        self.assertIn("$215,000.00", text)


class TestPayrollIsASeparateGrant(Case):
    """`finance` opens the screen: the rent, the subscriptions, the total
    cost of running the business. Reporting will need that, and more than
    one person will have it.

    What people EARN is a different question, and a different grant.
    Somebody can see that wages cost $96,250.01 in July without seeing that
    Justin is on $215,000.
    """

    roles = ("viewer", "finance")        # no payroll

    def test_the_screen_still_opens(self):
        status, body = self.overview()[1] if False else self.call(
            "GET", "/api/expenses")
        self.assertEqual(status, 200)
        self.assertFalse(body["may_see_salaries"])

    def test_the_totals_are_all_there(self):
        """The point of the split: the cost is visible, the salary is
        not."""
        _s, body = self.call("GET", "/api/expenses")
        wages = [l for l in body["lines"] if l["category_kind"] == "wages"]
        self.assertEqual(len(wages), 11)
        for line in body["lines"]:
            self.assertNotIn("annual_cents", line)

    def test_a_salary_is_refused_even_with_a_live_elevation(self):
        """The elevation says they are still at the keyboard. It does not
        say they are allowed."""
        line = self.line_named("Richard Roberts")
        status, body = self.call(
            "GET", f"/api/expenses/salary/{line['line_id']}", elevated=True)
        self.assertEqual(status, 403)
        self.assertIn("payroll", body["error"])

    def test_nor_may_they_set_one(self):
        line = self.line_named("Finau (Forecasted)")
        status, _b = self.call("POST", "/api/expenses/salaries", {
            "line_id": line["line_id"], "from_period_id": self.period("Jul-26"),
            "annual_cents": 9000000}, elevated=True)
        self.assertEqual(status, 403)

    def test_the_export_leaves_salaries_out(self):
        token = auth.mint_session(self.key, self.user["id"],
                                  self.user["token_version"])
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/expenses/export",
                  headers={"Sec-Fetch-Site": "same-origin",
                           "Cookie": f"{auth.COOKIE_NAME}={token}; "
                                     f"{auth.ELEVATION_COOKIE}="
                                     + auth.mint_elevation(self.key,
                                                           self.user["id"])})
        text = c.getresponse().read().decode("utf-8-sig")
        c.close()
        self.assertNotIn("Annual salary", text)
        # But the costs are all there.
        self.assertIn("Wages", text)

    def test_granting_payroll_is_enough(self):
        self.db.grant_role(self.user["id"], 1, "payroll", self.user["id"])
        _s, body = self.call("GET", "/api/expenses", elevated=True)
        self.assertTrue(body["may_see_salaries"])
        self.assertTrue(body["elevated"])

    def test_payroll_alone_without_finance_opens_nothing(self):
        """Neither implies the other."""
        self.db.revoke_role(self.user["id"], 1, "finance", self.user["id"])
        self.db.grant_role(self.user["id"], 1, "payroll", self.user["id"])
        self.assertEqual(self.call("GET", "/api/expenses")[0], 403)


class TestOnlyFinanceMaySee(Case):
    roles = ("viewer", "operations", "approver", "admin")
    imported = False

    def test_every_other_role_together_is_not_enough(self):
        """`finance` is implied by nothing, including admin."""
        self.assertEqual(self.call("GET", "/api/expenses")[0], 403)

    def test_nor_may_they_write(self):
        for method, path, body in (
                ("POST", "/api/expenses/categories", {"name": "X"}),
                ("POST", "/api/expenses/lines", {"name": "X"}),
                ("POST", "/api/expenses/amounts", {"line_id": 1}),
                ("POST", "/api/expenses/salaries", {"line_id": 1})):
            self.assertEqual(self.call(method, path, body)[0], 403,
                             f"{method} {path}")

    def test_granting_it_is_enough(self):
        self.db.grant_role(self.user["id"], 1, "finance", self.user["id"])
        self.assertEqual(self.call("GET", "/api/expenses")[0], 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
