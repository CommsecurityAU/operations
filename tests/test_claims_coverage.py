"""Forecast coverage: what is left to bill and sits in no month.

`orders in hand` is contract less invoiced — everything still to bill. The
forecast is what has been put into a month. If the forecasting is complete
they agree, and where they do not the difference is either work nobody has
scheduled or a month carrying more than the contract allows.
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
from ops.main import boot  # noqa: E402
from ops.secrets import LocalProvider  # noqa: E402


class Case(unittest.TestCase):
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
        for role in ("viewer", "operations"):
            self.db.grant_role(self.user["id"], 1, role, self.user["id"])
        with self.db._tx() as c:
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,
                             created_ts, contract_value_cents)
                         VALUES (1,'A Job','JN-1,1','Active',0,10000000)""")
            c.execute("""INSERT INTO customer_po (entity_id,project_id,
                             amount_cents,created_ts) VALUES (1,1,10000000,0)""")

    def tearDown(self):
        self.sched.stop()
        self.server.shutdown()
        self.server.server_close()
        self.t.join(timeout=5)
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def call(self, path):
        token = auth.mint_session(self.key, self.user["id"],
                                  self.user["token_version"])
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path,
                  headers={"Sec-Fetch-Site": "same-origin",
                           "Cookie": f"{auth.COOKIE_NAME}={token}"})
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, (json.loads(raw) if raw else None)

    def claim(self, label, cents, status="forecast"):
        period = self.db.scalar("SELECT id FROM period WHERE label = ?",
                                (label,))
        with self.db._tx() as c:
            c.execute(
                """INSERT INTO claim_line (entity_id, project_id,
                       customer_po_id, period_id, status, amount_cents,
                       created_ts)
                   VALUES (1,1,1,?,?,?,0)""", (period, status, cents))

    def coverage(self):
        _s, body = self.call("/api/claims")
        return body["coverage"][0], body["coverage_totals"]


class TestTheCardsReadAsAPosition(Case):
    """Filtered to one project the cards should tell the whole story:
    what the job is worth, what is left to bill, what has been billed,
    what is scheduled, and what is not."""

    def test_the_contract_comes_with_the_coverage(self):
        """The cards sum it over the projects on screen, so it has to be
        in the payload per project rather than derived from the claims."""
        self.claim("Sep-26", 6000000)
        row, _t = self.coverage()
        self.assertEqual(row["contract_value_cents"], 10000000)
        self.assertEqual(row["orders_in_hand_cents"], 10000000)

    def test_a_contract_is_not_a_monthly_quantity(self):
        """Two months of claims, one contract. Summing the coverage rows
        rather than the claims is what keeps it that way."""
        self.claim("Sep-26", 3000000)
        self.claim("Oct-26", 3000000)
        _s, body = self.call("/api/claims")
        self.assertEqual(len(body["coverage"]), 1)
        self.assertEqual(body["coverage"][0]["contract_value_cents"],
                         10000000)

    def test_invoicing_moves_orders_in_hand_and_not_the_contract(self):
        self.claim("Jul-26", 4000000, status="invoiced")
        row, _t = self.coverage()
        self.assertEqual(row["contract_value_cents"], 10000000)
        self.assertEqual(row["orders_in_hand_cents"], 6000000)


class TestCoverage(Case):
    def test_a_fully_forecast_project_is_covered(self):
        self.claim("Sep-26", 10000000)
        row, totals = self.coverage()
        self.assertEqual(row["state"], "complete")
        self.assertEqual(totals["not_forecast_cents"], 0)

    def test_work_left_to_bill_with_no_month_is_reported(self):
        self.claim("Sep-26", 6000000)
        row, totals = self.coverage()
        self.assertEqual(row["state"], "project under")
        self.assertEqual(row["gap_cents"], 4000000)
        self.assertEqual(totals["not_forecast_cents"], 4000000)
        self.assertEqual(totals["projects_not_forecast"], 1)

    def test_a_month_carrying_more_than_the_contract_is_reported(self):
        self.claim("Sep-26", 12000000)
        row, totals = self.coverage()
        self.assertEqual(row["state"], "project over")
        self.assertEqual(totals["over_forecast_cents"], 2000000)

    def test_an_invoiced_claim_is_not_forecast(self):
        """It has happened. Counting it as forecast would say the work is
        scheduled when it is already billed."""
        self.claim("Jul-26", 4000000, status="invoiced")
        self.claim("Sep-26", 6000000)
        row, _t = self.coverage()
        self.assertEqual(row["forecast_cents"], 6000000)
        self.assertEqual(row["orders_in_hand_cents"], 6000000)
        self.assertEqual(row["state"], "complete")

    def test_a_dollar_is_not_a_finding(self):
        """Rounding in the source. Amber on half the register for a cent
        teaches the eye to skip the check."""
        self.claim("Sep-26", 9999950)
        row, _t = self.coverage()
        self.assertEqual(row["state"], "complete")

    def test_every_claim_carries_its_project_coverage(self):
        """So the grid can be filtered down to the jobs that need
        scheduling."""
        self.claim("Sep-26", 6000000)
        _s, body = self.call("/api/claims")
        self.assertTrue(body["claims"])
        for row in body["claims"]:
            self.assertEqual(row["coverage"], "project under")

    def test_the_totals_do_not_net_out(self):
        """A portfolio net would cancel an under-forecast job against an
        over-forecast one and report that everything is fine."""
        with self.db._tx() as c:
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,
                             created_ts, contract_value_cents)
                         VALUES (1,'B Job','JN-2,2','Active',0,10000000)""")
            c.execute("""INSERT INTO customer_po (entity_id,project_id,
                             amount_cents,created_ts) VALUES (1,2,10000000,0)""")
            period = self.db.scalar(
                "SELECT id FROM period WHERE label = 'Sep-26'")
            c.execute(
                """INSERT INTO claim_line (entity_id, project_id,
                       customer_po_id, period_id, status, amount_cents,
                       created_ts)
                   VALUES (1,2,2,?, 'forecast', 14000000, 0)""", (period,))
        self.claim("Sep-26", 6000000)
        _s, body = self.call("/api/claims")
        totals = body["coverage_totals"]
        self.assertEqual(totals["not_forecast_cents"], 4000000)
        self.assertEqual(totals["over_forecast_cents"], 4000000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
