"""Claims — lifecycle, EOM assignment, slippage, retention at invoice.

Most of the value is in what it refuses. A claim table that accepts anything
is how the Invoicing tab became something nobody could reconcile.
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
from ops.modules import claims as mod  # noqa: E402
from ops.secrets import LocalProvider  # noqa: E402


class Case(unittest.TestCase):
    roles = ("viewer", "operations")

    def setUp(self):
        for n in ("ops.http", "ops.main", "ops.auth"):
            logging.getLogger(n).setLevel(logging.CRITICAL)
        self.dir = tempfile.mkdtemp()
        secrets_path = os.path.join(self.dir, "secrets", "store.json")
        LocalProvider(secrets_path).set("OIDC_CLIENT_SECRET", "x")
        cfg = Config(data_dir=self.dir, tls=False, port=0,
                     oidc_client_id="cid", oidc_redirect_uri="http://x/cb")
        self.db, self.server, self.sched = boot(
            cfg=cfg, env={"OPS_SECRETS_PATH": secrets_path}, serve=False)
        self.port = self.server.server_address[1]
        self.t = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
            daemon=True)
        self.t.start()
        self.key = auth.load_or_create_key(cfg.session_key_path)
        self.user = auth.sign_in(self.db, {"sub": "s1", "email": "r@x", "name": "R"})
        for role in self.roles:
            self.db.grant_role(self.user["id"], 1, role, self.user["id"])

        with self.db._tx() as c:
            c.execute("INSERT INTO client (entity_id,name) VALUES (1,'Hines')")
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,
                             created_ts, practical_completion_date, dlp_end_date)
                         VALUES (1,'Claimable','JN-9900','Active',0,
                                 '2027-03-31','2028-03-31')""")
        self.project_id = self.db.scalar(
            "SELECT id FROM project WHERE job_code='JN-9900'")
        with self.db._tx() as c:
            c.execute("""INSERT INTO customer_po (entity_id,project_id,
                             amount_cents,retention_applies,retention_rate_bp,
                             retention_cap_bp,release_policy,created_ts)
                         VALUES (1,?,70000000,1,1000,250,'dlp',0)""",
                      (self.project_id,))
        self.po_id = self.db.scalar(
            "SELECT id FROM customer_po WHERE project_id=?", (self.project_id,))
        self.sep = self.db.scalar(
            "SELECT id FROM period WHERE month_start='2026-09-01'")
        self.oct = self.db.scalar(
            "SELECT id FROM period WHERE month_start='2026-10-01'")

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

    def valid(self, **over):
        payload = {"project_id": self.project_id, "customer_po_id": self.po_id,
                   "period_id": self.sep, "amount_cents": 10000000,
                   "status": "forecast", "detail": "Progress claim"}
        payload.update(over)
        return payload

    def make(self, **over):
        status, body = self.call("POST", "/api/claims", self.valid(**over))
        self.assertEqual(status, 201, body)
        return body

    def move(self, claim_id, to, **extra):
        return self.call("POST", f"/api/claims/{claim_id}/status",
                         {"status": to, **extra})


class TestCreate(Case):
    def test_creates_a_forecast(self):
        c = self.make()
        self.assertEqual(c["status"], "forecast")
        self.assertEqual(c["period_id"], self.sep)

    def test_an_eom_is_mandatory(self):
        """We invoice monthly. A claim with no EOM cannot appear in the month
        it belongs to, which is the only view anyone works from."""
        status, body = self.call("POST", "/api/claims",
                                 self.valid(period_id=None))
        self.assertEqual(status, 400)
        self.assertIn("period_id", body["detail"])

    def test_a_po_is_mandatory(self):
        """Only an opening balance may float free of a PO, and those are made
        by migration, never here."""
        status, body = self.call("POST", "/api/claims",
                                 self.valid(customer_po_id=None))
        self.assertEqual(status, 400)
        self.assertIn("customer_po_id", body["detail"])

    def test_a_claim_cannot_start_already_invoiced(self):
        """Arriving invoiced would skip every check between intent and
        invoice."""
        status, body = self.call("POST", "/api/claims",
                                 self.valid(status="invoiced"))
        self.assertEqual(status, 400)
        self.assertIn("status", body["detail"])

    def test_a_negative_amount_is_refused(self):
        status, body = self.call("POST", "/api/claims",
                                 self.valid(amount_cents=-100))
        self.assertEqual(status, 400)
        self.assertIn("cancel", body["detail"]["amount_cents"])

    def test_a_po_from_another_project_is_refused(self):
        with self.db._tx() as c:
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,created_ts)
                         VALUES (1,'Other','JN-9901','Active',0)""")
            other = c.execute("SELECT id FROM project WHERE job_code='JN-9901'").fetchone()[0]
            c.execute("""INSERT INTO customer_po (entity_id,project_id,amount_cents,created_ts)
                         VALUES (1,?,1000,0)""", (other,))
        other_po = self.db.scalar(
            "SELECT id FROM customer_po ORDER BY id DESC LIMIT 1")
        status, body = self.call("POST", "/api/claims",
                                 self.valid(customer_po_id=other_po))
        self.assertEqual(status, 400)
        self.assertIn("customer_po_id", body["detail"])


class TestLifecycle(Case):
    def test_the_happy_path(self):
        c = self.make()
        self.assertEqual(self.move(c["id"], "due")[0], 200)
        self.assertEqual(self.move(c["id"], "approved",
                                   approved_date="2026-09-20")[0], 200)
        self.assertEqual(self.move(c["id"], "invoiced",
                                   invoice_number="INV-7251",
                                   invoiced_date="2026-09-22")[0], 200)
        status, body = self.move(c["id"], "paid", paid_date="2026-10-15")
        self.assertEqual(status, 200)
        self.assertEqual(body["claim"]["status"], "paid")

    def test_a_stage_cannot_be_skipped(self):
        c = self.make()
        status, body = self.move(c["id"], "invoiced",
                                 invoice_number="X", invoiced_date="2026-09-22")
        self.assertEqual(status, 409)
        self.assertIn("allowed:", body["error"])

    def test_invoicing_requires_an_invoice_number_and_date(self):
        """The workbook captured these inconsistently -- some rows had a
        number, some had none at all."""
        c = self.make()
        self.move(c["id"], "due")
        self.move(c["id"], "approved", approved_date="2026-09-20")
        status, body = self.move(c["id"], "invoiced")
        self.assertEqual(status, 400)
        self.assertIn("invoice_number", body["detail"])
        self.assertIn("invoiced_date", body["detail"])

    def test_moving_backwards_needs_a_reason(self):
        c = self.make()
        self.move(c["id"], "due")
        self.assertEqual(self.move(c["id"], "forecast")[0], 400)
        self.assertEqual(
            self.move(c["id"], "forecast", reason="scope deferred")[0], 200)

    def test_reversing_an_invoice_needs_the_approver_role(self):
        """Undoing something that exists in Xero."""
        c = self.make()
        self.move(c["id"], "due")
        self.move(c["id"], "approved", approved_date="2026-09-20")
        self.move(c["id"], "invoiced", invoice_number="INV-1",
                  invoiced_date="2026-09-22")
        status, body = self.move(c["id"], "approved", reason="invoice voided")
        self.assertEqual(status, 403)
        self.assertIn("approver", body["error"])

        self.db.grant_role(self.user["id"], 1, "approver", self.user["id"])
        self.assertEqual(
            self.move(c["id"], "approved", reason="invoice voided")[0], 200)

    def test_every_move_is_recorded(self):
        c = self.make()
        self.move(c["id"], "due")
        self.move(c["id"], "forecast", reason="client pushed it out")
        _s, body = self.call("GET", f"/api/claims/{c['id']}/history")
        moves = [r for r in body["revisions"] if r["field"] == "status"]
        self.assertEqual(len(moves), 2)
        self.assertEqual(moves[-1]["reason"], "client pushed it out")


class TestSlippage(Case):
    def test_moving_to_another_month_needs_a_reason(self):
        """A forecast that is quietly rewritten always looks like it was
        right. Slippage is the number forecasting accuracy is measured
        against, so it has to be recorded."""
        c = self.make()
        status, body = self.call("PATCH", f"/api/claims/{c['id']}",
                                 {"period_id": self.oct})
        self.assertEqual(status, 400)
        self.assertIn("slippage", body["detail"]["reason"])

    def test_with_a_reason_it_moves_and_is_recorded(self):
        c = self.make()
        status, body = self.call("PATCH", f"/api/claims/{c['id']}",
                                 {"period_id": self.oct,
                                  "reason": "site access delayed"})
        self.assertEqual(status, 200)
        self.assertEqual(body["changed"], ["period_id"])
        _s, hist = self.call("GET", f"/api/claims/{c['id']}/history")
        slip = [r for r in hist["revisions"] if r["field"] == "period_id"][0]
        self.assertEqual(slip["old_value"], str(self.sep))
        self.assertEqual(slip["new_value"], str(self.oct))
        self.assertEqual(slip["reason"], "site access delayed")

    def test_other_edits_do_not_need_a_reason(self):
        c = self.make()
        self.assertEqual(self.call("PATCH", f"/api/claims/{c['id']}",
                                   {"detail": "revised wording"})[0], 200)


class TestRetentionAtInvoice(Case):
    def invoice(self, claim, number="INV-1"):
        self.move(claim["id"], "due")
        self.move(claim["id"], "approved", approved_date="2026-09-20")
        return self.move(claim["id"], "invoiced", invoice_number=number,
                         invoiced_date="2026-09-22")

    def test_retention_is_withheld_when_the_claim_is_invoiced(self):
        c = self.make(amount_cents=10000000)
        _s, body = self.invoice(c)
        self.assertEqual(body["retention_cents"], 1000000)

    def test_a_forecast_withholds_nothing(self):
        c = self.make()
        self.assertEqual(c["retention_cents"], 0)

    def test_retention_is_computed_at_INVOICE_not_at_creation(self):
        """Two forecasts each computed against the same remaining capacity
        would both take the full 10%, and together exceed the cap the moment
        both were invoiced. Only invoicing withholds anything, so that is
        where the figure is fixed."""
        a = self.make(amount_cents=10000000)
        b = self.make(amount_cents=10000000)
        c = self.make(amount_cents=10000000)
        self.assertEqual(self.invoice(a, "INV-1")[1]["retention_cents"], 1000000)
        self.assertEqual(self.invoice(b, "INV-2")[1]["retention_cents"], 750000)
        self.assertEqual(self.invoice(c, "INV-3")[1]["retention_cents"], 0)
        position = self.db.query_one(
            "SELECT * FROM v_po_retention_position WHERE customer_po_id = ?",
            (self.po_id,))
        self.assertEqual(position["withheld_cents"], 1750000)

    def test_reversing_an_invoice_releases_the_withholding(self):
        """The customer is not holding money against an invoice that no
        longer exists."""
        self.db.grant_role(self.user["id"], 1, "approver", self.user["id"])
        c = self.make(amount_cents=10000000)
        self.invoice(c)
        self.move(c["id"], "approved", reason="voided")
        self.assertEqual(self.db.scalar(
            "SELECT retention_cents FROM claim_line WHERE id = ?", (c["id"],)), 0)
        self.assertEqual(self.db.query_one(
            "SELECT * FROM v_po_retention_position WHERE customer_po_id = ?",
            (self.po_id,))["withheld_cents"], 0)


class TestOpeningBalancesAreUntouchable(Case):
    def test_they_cannot_be_patched_or_moved(self):
        with self.db._tx() as c:
            c.execute("""INSERT INTO claim_line (entity_id,project_id,status,
                             amount_cents,is_opening_balance,created_ts)
                         VALUES (1,?, 'invoiced',5000,1,0)""", (self.project_id,))
        cid = self.db.scalar(
            "SELECT id FROM claim_line WHERE is_opening_balance=1")
        self.assertEqual(self.call("PATCH", f"/api/claims/{cid}",
                                   {"detail": "x"})[0], 409)
        self.assertEqual(self.move(cid, "paid", paid_date="2026-07-01")[0], 409)


class TestTheMonthView(Case):
    def test_claims_can_be_filtered_by_eom(self):
        self.make(period_id=self.sep)
        self.make(period_id=self.oct, amount_cents=5000000)
        _s, sep = self.call("GET", f"/api/claims?period={self.sep}")
        self.assertEqual(len(sep["claims"]), 1)
        self.assertEqual(sep["totals"]["forecast"], 10000000)

    def test_the_response_carries_the_totals_by_status(self):
        self.make()
        c = self.make(amount_cents=2000000)
        self.move(c["id"], "due")
        _s, body = self.call("GET", "/api/claims")
        self.assertEqual(body["totals"]["forecast"], 10000000)
        self.assertEqual(body["totals"]["due"], 2000000)

    def test_it_reports_the_period_label_for_display(self):
        self.make()
        _s, body = self.call("GET", "/api/claims")
        self.assertEqual(body["claims"][0]["period_label"], "Sep-26")

    def test_the_transition_map_is_published_for_the_ui(self):
        """So the screen offers only the moves that exist, rather than
        discovering them by being refused."""
        _s, body = self.call("GET", "/api/claims")
        self.assertEqual(set(body["transitions"]), set(mod.TRANSITIONS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
