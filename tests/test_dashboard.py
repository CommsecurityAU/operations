"""The operations dashboard (STP-5).

    revenue - project cost - office cost = gross profit
    gross profit - corporate tax         = net profit

Two rules the sheet got wrong, and they are the whole point of computing it.
"""

import http.client
import json
import logging
import os
import shutil
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


class Case(unittest.TestCase):
    roles = ("viewer", "finance", "admin")

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
        with self.db._tx() as c:
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,
                             created_ts, contract_value_cents)
                         VALUES (1,'A Job','JN-1,1','Active',0,50000000)""")
            c.execute("""INSERT INTO customer_po (entity_id,project_id,
                             amount_cents,created_ts)
                         VALUES (1,1,50000000,0)""")
        self.project = 1

    def tearDown(self):
        self.sched.stop()
        self.server.shutdown()
        self.server.server_close()
        self.t.join(timeout=5)
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def call(self, method, path, body=None):
        token = auth.mint_session(self.key, self.user["id"],
                                  self.user["token_version"])
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path,
                  body=None if body is None else json.dumps(body).encode(),
                  headers={"Content-Type": "application/json",
                           "Sec-Fetch-Site": "same-origin",
                           "Cookie": f"{auth.COOKIE_NAME}={token}"})
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, (json.loads(raw) if raw else None)

    def period(self, label):
        return self.db.scalar("SELECT id FROM period WHERE label = ?", (label,))

    def claim(self, label, cents, status="forecast"):
        with self.db._tx() as c:
            c.execute(
                """INSERT INTO claim_line (entity_id, project_id,
                       customer_po_id, period_id, status, amount_cents,
                       created_ts)
                   VALUES (1,?,1,?,?,?,0)""",
                (self.project, self.period(label), status, cents))

    def cost(self, label, cents, estimate=0):
        self.db.create_procurement_line(
            {"entity_id": 1, "project_id": self.project,
             "period_id": self.period(label), "quantity": 1,
             "unit_cost_cents": cents, "total_cents": cents,
             "is_estimate": estimate}, self.user["id"])

    def office(self, label, cents):
        category = self.db.create_expense_category(1, f"Rent {label}",
                                                   "expense", self.user["id"])
        line = self.db.create_expense_line(
            {"entity_id": 1, "category_id": category["id"], "name": "Rent"},
            self.user["id"])
        self.db.set_expense_amount(line["line_id"], self.period(label), cents,
                                   self.user["id"])

    def year(self, label="FY27"):
        _s, body = self.call("GET", "/api/dashboard")
        return next((y for y in body["years"] if y["fy_label"] == label), None)


class TestTheArithmetic(Case):
    def test_gross_profit_is_revenue_less_both_costs(self):
        self.claim("Sep-26", 100000_00)
        self.cost("Sep-26", 30000_00)
        self.office("Sep-26", 20000_00)
        year = self.year()
        self.assertEqual(year["revenue_cents"], 100000_00)
        self.assertEqual(year["project_cost_cents"], 30000_00)
        self.assertEqual(year["office_cost_cents"], 20000_00)
        self.assertEqual(year["gross_profit_cents"], 50000_00)

    def test_office_cost_is_not_charged_to_a_project(self):
        """Rent is not bought for a job. Spreading it across jobs would
        invent a margin nobody agreed to."""
        self.office("Sep-26", 20000_00)
        _s, body = self.call("GET", "/api/dashboard")
        project = body["projects"][0]
        self.assertEqual(project["committed_cents"], 0)
        self.assertEqual(self.year()["office_cost_cents"], 20000_00)

    def test_estimates_count_as_project_cost(self):
        """A forecast that leaves out the expected cost of the work is not
        a forecast."""
        self.cost("Sep-26", 10000_00)
        self.cost("Oct-26", 40000_00, estimate=1)
        year = self.year()
        self.assertEqual(year["project_cost_cents"], 50000_00)
        self.assertEqual(year["estimated_cost_cents"], 40000_00)

    def test_billed_and_forecast_revenue_are_kept_apart(self):
        """One is a fact and the other is a plan."""
        self.claim("Jul-26", 60000_00, status="invoiced")
        self.claim("Sep-26", 40000_00)
        year = self.year()
        self.assertEqual(year["invoiced_cents"], 60000_00)
        self.assertEqual(year["forecast_cents"], 40000_00)
        self.assertEqual(year["revenue_cents"], 100000_00)


class TestTaxIsAssessedOnTheYear(Case):
    """The sheet taxed each profitable month and gave no credit for loss
    months: $267,227 against $104,647 of gross profit, and a headline net
    profit of MINUS $162,580. Assessed on the year it is $26,162 and plus
    $78,485. A quarter of a million apart, and the second is how company tax
    works."""

    def test_a_loss_month_offsets_a_profit_month(self):
        self.claim("Sep-26", 100000_00)
        self.cost("Mar-27", 90000_00)
        year = self.year()
        self.assertEqual(year["gross_profit_cents"], 10000_00)
        # 25% of the YEAR's $10,000, not 25% of September's $100,000.
        self.assertEqual(year["corporate_tax_cents"], 2500_00)
        self.assertEqual(year["net_profit_cents"], 7500_00)

    def test_a_losing_year_pays_none(self):
        self.claim("Sep-26", 10000_00)
        self.cost("Sep-26", 40000_00)
        year = self.year()
        self.assertLess(year["gross_profit_cents"], 0)
        self.assertEqual(year["corporate_tax_cents"], 0)
        self.assertEqual(year["net_profit_cents"], year["gross_profit_cents"])

    def test_the_rate_defaults_to_twenty_five_per_cent(self):
        self.claim("Sep-26", 100000_00)
        self.assertEqual(self.year()["tax_rate_bp"], Db.rate(25))

    def test_the_rate_can_be_changed_per_year(self):
        """It belongs per financial year because it changes per financial
        year."""
        self.claim("Sep-26", 100000_00)
        status, _b = self.call("POST", "/api/dashboard/settings",
                               {"fy": 2027, "tax_rate_percent": 30})
        self.assertEqual(status, 200)
        year = self.year()
        self.assertEqual(year["corporate_tax_cents"], 30000_00)

    def test_a_nonsense_rate_is_refused(self):
        for bad in ("a quarter", -5, 150):
            status, body = self.call("POST", "/api/dashboard/settings",
                                     {"fy": 2027, "tax_rate_percent": bad})
            self.assertEqual(status, 400, bad)
            self.assertIn("tax_rate_percent", body["detail"])


class TestFurtherSales(Case):
    def test_it_adds_to_planned_revenue_without_touching_the_actual(self):
        """A judgement rather than a record, so it is held where a
        judgement can be seen and changed."""
        self.claim("Sep-26", 100000_00)
        self.call("POST", "/api/dashboard/settings",
                  {"fy": 2027, "further_sales_cents": 50000_00})
        year = self.year()
        self.assertEqual(year["revenue_cents"], 100000_00)
        self.assertEqual(year["planned_revenue_cents"], 150000_00)
        # And it is NOT taxed: it has not happened.
        self.assertEqual(year["gross_profit_cents"], 100000_00)

    def test_changing_the_settings_is_audited(self):
        self.call("POST", "/api/dashboard/settings",
                  {"fy": 2027, "further_sales_cents": 50000_00})
        self.assertIsNotNone(self.db.query_one(
            "SELECT id FROM audit_log WHERE action = 'fy_settings'"))


class TestActualsAndProjections(Case):
    def test_a_month_that_has_ended_is_actual(self):
        """A dashboard that mixes them silently is one nobody can act
        on."""
        self.claim("Jul-26", 10000_00, status="invoiced")
        self.claim("Jun-27", 10000_00)
        _s, body = self.call("GET", "/api/dashboard")
        by_label = {m["label"]: m for m in body["months"]}
        self.assertTrue(by_label["Jul-26"]["is_actual"])
        self.assertFalse(by_label["Jun-27"]["is_actual"])


class TestInvoicingByProject(Case):
    """Where the year's revenue actually comes from — the table people
    read after the totals."""

    def rows(self):
        _s, body = self.call("GET", "/api/dashboard")
        return body["project_months"]

    def test_a_project_month_appears_once(self):
        self.claim("Sep-26", 10000_00)
        self.claim("Sep-26", 5000_00)
        rows = [r for r in self.rows()
                if r["period_id"] == self.period("Sep-26")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount_cents"], 15000_00)

    def test_billed_and_forecast_are_distinguishable(self):
        """One is a fact. The grid colours them differently, so the split
        has to survive the round trip."""
        self.claim("Jul-26", 8000_00, status="invoiced")
        self.claim("Sep-26", 4000_00)
        by_period = {r["period_id"]: r for r in self.rows()}
        self.assertEqual(
            by_period[self.period("Jul-26")]["invoiced_cents"], 8000_00)
        self.assertEqual(
            by_period[self.period("Sep-26")]["invoiced_cents"], 0)

    def test_an_opening_balance_is_not_in_it(self):
        """It has no month, and it belongs to a year this platform did not
        cover."""
        with self.db._tx() as c:
            c.execute(
                """INSERT INTO claim_line (entity_id, project_id,
                       customer_po_id, status, amount_cents,
                       is_opening_balance, claim_date, invoiced_date,
                       created_ts)
                   VALUES (1,?,1,'invoiced',?,1,'2026-06-30',
                           '2026-06-30',0)""",
                (self.project, 99999_00))
        self.assertEqual(self.rows(), [])

    def test_the_columns_sum_to_the_month(self):
        """The table's own total has to agree with the figure above it."""
        self.claim("Sep-26", 30000_00)
        self.claim("Oct-26", 20000_00)
        _s, body = self.call("GET", "/api/dashboard")
        by_period = {}
        for row in body["project_months"]:
            by_period[row["period_id"]] = \
                by_period.get(row["period_id"], 0) + row["amount_cents"]
        for month in body["months"]:
            self.assertEqual(by_period.get(month["period_id"], 0),
                             month["revenue_cents"], month["label"])


class TestPermissions(Case):
    roles = ("viewer",)

    def test_the_dashboard_needs_finance(self):
        self.assertEqual(self.call("GET", "/api/dashboard")[0], 403)

    def test_the_settings_need_admin(self):
        self.db.grant_role(self.user["id"], 1, "finance", self.user["id"])
        self.assertEqual(self.call("GET", "/api/dashboard")[0], 200)
        # Finance reads it; changing the tax rate changes what every figure
        # means, so that is an administrator's decision.
        self.assertEqual(self.call("POST", "/api/dashboard/settings",
                                   {"fy": 2027, "tax_rate_percent": 10})[0],
                         403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
