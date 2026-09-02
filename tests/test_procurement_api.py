"""Procurement over HTTP — the register as a screen can use it."""

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

RATE = 13_885_610


class Case(unittest.TestCase):
    roles = ("viewer", "operations")

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
        self.user = auth.sign_in(self.db, {"sub": "s1", "email": "r@x", "name": "R"})
        for role in self.roles:
            self.db.grant_role(self.user["id"], 1, role, self.user["id"])
        with self.db._tx() as c:
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,
                             created_ts) VALUES (1,'720 Bourke','JN-5749','Active',0)""")
        self.project = self.db.scalar(
            "SELECT id FROM project WHERE job_code='JN-5749'")
        self.db.create_suppliers(
            [{"entity_id": 1, "name": "USR", "default_currency": "USD"}],
            self.user["id"])
        self.supplier = self.db.scalar("SELECT id FROM supplier WHERE name='USR'")

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

    def add(self, **over):
        payload = {"project_id": self.project, "supplier_id": self.supplier,
                   "item": "USR-N510-H7-4", "quantity": 1,
                   "unit_cost_cents": 4582}
        payload.update(over)
        return self.call("POST", "/api/procurement", payload)


class TestAddingALine(Case):
    def test_it_lands(self):
        status, body = self.add()
        self.assertEqual(status, 201, body)
        self.assertEqual(body["total_cents"], 4582)

    def test_the_total_is_extended_not_per_unit(self):
        _s, body = self.add(quantity=7, unit_cost_cents=4582)
        self.assertEqual(body["total_cents"], 32074)

    def test_a_usd_line_needs_a_quote_carrying_the_rate(self):
        """Without one it cannot be costed in AUD, and storing it at par
        would understate the cost by forty per cent."""
        status, body = self.add(currency="USD", unit_cost_cents=3300)
        self.assertEqual(status, 400)
        self.assertIn("supplier_quote_id", body["detail"])

    def test_with_a_quote_the_usd_line_converts(self):
        quote = self.db.create_supplier_quote(
            {"entity_id": 1, "supplier_id": self.supplier, "currency": "USD",
             "fx_rate_bp": RATE, "quote_ref": "11395"}, self.user["id"])
        _s, body = self.add(currency="USD", unit_cost_cents=3300, quantity=7,
                            supplier_quote_id=quote["id"])
        self.assertEqual(body["total_cents"], 32076)

    def test_a_project_on_another_entity_is_refused(self):
        with self.db._tx() as c:
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,
                             created_ts) VALUES (2,'Elsewhere','JN-9,9','Active',0)""")
        other = self.db.scalar("SELECT id FROM project WHERE name='Elsewhere'")
        self.assertEqual(self.add(project_id=other)[0], 400)


class TestDatesNotStatuses(Case):
    def line(self):
        return self.add()[1]["id"]

    def test_recording_a_delivery_changes_the_state(self):
        line = self.line()
        self.call("PATCH", f"/api/procurement/{line}",
                  {"delivered_date": "2026-07-20"})
        self.assertEqual(self.db.scalar(
            "SELECT state FROM v_procurement_line WHERE id = ?", (line,)),
            "delivered, unpaid")

    def test_a_real_date_supersedes_the_imported_text(self):
        """The sheet said something; a date says when. Once there is a date
        the sheet stops being consulted."""
        line = self.line()
        self.db.update_procurement_line(
            line, {"stated_state": "delivered"}, self.user["id"])
        self.call("PATCH", f"/api/procurement/{line}",
                  {"paid_date": "2026-07-25"})
        row = self.db.query_one(
            "SELECT state, state_undated FROM v_procurement_line "
            "WHERE id = ?", (line,))
        self.assertEqual(row["state"], "paid, pending delivery")
        self.assertEqual(row["state_undated"], 0)

    def test_cancelling_needs_a_reason(self):
        """A line that vanishes from the cost without one is a figure
        nobody can explain at month end."""
        line = self.line()
        status, body = self.call("PATCH", f"/api/procurement/{line}",
                                 {"cancelled_date": "2026-07-06"})
        self.assertEqual(status, 400)
        self.assertIn("cancel_reason", body["detail"])

    def test_with_a_reason_it_cancels_and_stops_costing(self):
        line = self.line()
        self.call("PATCH", f"/api/procurement/{line}",
                  {"cancelled_date": "2026-07-06", "cancel_reason": "returned"})
        self.assertEqual(self.db.scalar(
            "SELECT committed_cents FROM v_project_procurement "
            "WHERE project_id = ?", (self.project,)), 0)

    def test_changing_the_quantity_recomputes_the_total(self):
        line = self.line()
        self.call("PATCH", f"/api/procurement/{line}", {"quantity": 3})
        self.assertEqual(self.db.scalar(
            "SELECT total_cents FROM procurement_line WHERE id = ?", (line,)),
            4582 * 3)

    def test_every_change_is_in_the_history(self):
        line = self.line()
        self.call("PATCH", f"/api/procurement/{line}",
                  {"delivered_date": "2026-07-20", "reason": "arrived early"})
        _s, body = self.call("GET", f"/api/procurement/{line}/history")
        fields = [r["field"] for r in body["revisions"]]
        self.assertIn("delivered_date", fields)


class TestOneInvoiceAcrossLines(Case):
    def test_the_same_reference_attaches_both(self):
        first = self.add()[1]["id"]
        second = self.add()[1]["id"]
        one, _b = self.call("POST", f"/api/procurement/{first}/invoice",
                            {"invoice_ref": "INV-000733"})
        two, body = self.call("POST", f"/api/procurement/{second}/invoice",
                              {"invoice_ref": "INV-000733"})
        self.assertEqual((one, two), (200, 200))
        self.assertFalse(body["created"])
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(DISTINCT supplier_invoice_id) FROM procurement_line"), 1)

    def test_attaching_marks_it_invoiced(self):
        line = self.add()[1]["id"]
        self.call("POST", f"/api/procurement/{line}/invoice",
                  {"invoice_ref": "INV-1", "invoice_date": "2026-07-10"})
        self.assertEqual(self.db.scalar(
            "SELECT state FROM v_procurement_line WHERE id = ?", (line,)),
            "invoiced")

    def test_a_line_with_no_supplier_is_refused(self):
        """An invoice belongs to a supplier."""
        line = self.add(supplier_id=None)[1]["id"]
        status, _b = self.call("POST", f"/api/procurement/{line}/invoice",
                               {"invoice_ref": "INV-1"})
        self.assertEqual(status, 409)


class TestEditingEverything(Case):
    def line(self):
        return self.add()[1]["id"]

    def test_the_project_can_be_changed(self):
        """A line entered against the wrong job is the commonest slip, and
        it moves the cost to another project's margin."""
        with self.db._tx() as c:
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,
                             created_ts) VALUES (1,'Other','JN-9,9','Active',0)""")
        other = self.db.scalar("SELECT id FROM project WHERE name='Other'")
        line = self.line()
        status, _b = self.call("PATCH", f"/api/procurement/{line}",
                               {"project_id": other})
        self.assertEqual(status, 200)
        self.assertEqual(self.db.scalar(
            "SELECT project_id FROM procurement_line WHERE id = ?", (line,)),
            other)

    def test_the_eom_can_be_changed(self):
        line = self.line()
        period = self.db.scalar("SELECT id FROM period WHERE label = 'Sep-26'")
        self.call("PATCH", f"/api/procurement/{line}", {"period_id": period})
        self.assertEqual(self.db.scalar(
            "SELECT period_id FROM procurement_line WHERE id = ?", (line,)),
            period)

    def test_the_supplier_can_be_changed_or_cleared(self):
        line = self.line()
        self.call("PATCH", f"/api/procurement/{line}", {"supplier_id": None})
        self.assertIsNone(self.db.scalar(
            "SELECT supplier_id FROM procurement_line WHERE id = ?", (line,)))

    def test_moving_to_another_quote_recosts_at_that_rate(self):
        """The whole reason the rate lives on the quote."""
        quote = self.db.create_supplier_quote(
            {"entity_id": 1, "supplier_id": self.supplier, "currency": "USD",
             "fx_rate_bp": RATE}, self.user["id"])
        line = self.add(currency="USD", unit_cost_cents=3300, quantity=7,
                        supplier_quote_id=quote["id"])[1]["id"]
        self.assertEqual(self.db.scalar(
            "SELECT total_cents FROM procurement_line WHERE id = ?", (line,)),
            32076)
        cheaper = self.db.create_supplier_quote(
            {"entity_id": 1, "supplier_id": self.supplier, "currency": "USD",
             "fx_rate_bp": 15_000_000}, self.user["id"])
        self.call("PATCH", f"/api/procurement/{line}",
                  {"supplier_quote_id": cheaper["id"], "quantity": 7,
                   "unit_cost_cents": 3300})
        self.assertEqual(self.db.scalar(
            "SELECT total_cents FROM procurement_line WHERE id = ?", (line,)),
            34650)

    def test_clearing_the_quote_on_a_usd_line_is_refused(self):
        """It would leave a foreign cost with no rate to state it in AUD."""
        quote = self.db.create_supplier_quote(
            {"entity_id": 1, "supplier_id": self.supplier, "currency": "USD",
             "fx_rate_bp": RATE}, self.user["id"])
        line = self.add(currency="USD", unit_cost_cents=3300,
                        supplier_quote_id=quote["id"])[1]["id"]
        status, body = self.call("PATCH", f"/api/procurement/{line}",
                                 {"supplier_quote_id": None, "quantity": 1,
                                  "unit_cost_cents": 3300})
        self.assertEqual(status, 400)
        self.assertIn("supplier_quote_id", body["detail"])


class TestCreatingQuotesAndOrders(Case):
    def test_a_foreign_quote_needs_a_rate(self):
        status, body = self.call("POST", "/api/procurement/quotes",
                                 {"supplier_id": self.supplier,
                                  "currency": "USD"})
        self.assertEqual(status, 400)
        self.assertIn("fx_rate", body["detail"])

    def test_a_quote_stores_the_rate_as_basis_points(self):
        status, body = self.call("POST", "/api/procurement/quotes",
                                 {"supplier_id": self.supplier,
                                  "currency": "USD", "fx_rate": 1.388561,
                                  "quote_ref": "11395"})
        self.assertEqual(status, 201)
        self.assertEqual(body["fx_rate_bp"], RATE)

    def test_an_aud_quote_takes_no_rate(self):
        status, body = self.call("POST", "/api/procurement/quotes",
                                 {"supplier_id": self.supplier,
                                  "currency": "AUD"})
        self.assertEqual(status, 201)
        self.assertIsNone(body["fx_rate_bp"])

    def test_an_order_needs_a_number(self):
        status, body = self.call("POST", "/api/procurement/pos",
                                 {"project_id": self.project,
                                  "supplier_id": self.supplier})
        self.assertEqual(status, 400)
        self.assertIn("po_number", body["detail"])

    def test_an_order_lands_and_can_be_attached(self):
        _s, po = self.call("POST", "/api/procurement/pos",
                           {"project_id": self.project,
                            "supplier_id": self.supplier,
                            "po_number": "PO-2225 5749"})
        line = self.add()[1]["id"]
        self.call("PATCH", f"/api/procurement/{line}",
                  {"supplier_po_id": po["id"]})
        self.assertEqual(self.db.scalar(
            "SELECT po_number FROM v_procurement_line WHERE id = ?", (line,)),
            "PO-2225 5749")

    def test_a_created_quote_comes_back_whole(self):
        """The grid names what it attached -- `quote 11395 attached` -- so
        the response has to carry the reference, not just an id."""
        _s, body = self.call("POST", "/api/procurement/quotes",
                             {"supplier_id": self.supplier, "currency": "AUD",
                              "quote_ref": "11395"})
        self.assertEqual(body["quote_ref"], "11395")
        self.assertIn("id", body)

    def test_a_created_order_comes_back_whole(self):
        _s, body = self.call("POST", "/api/procurement/pos",
                             {"project_id": self.project,
                              "supplier_id": self.supplier,
                              "po_number": "PO-2225 5749"})
        self.assertEqual(body["po_number"], "PO-2225 5749")
        self.assertIn("id", body)

    def test_a_quote_made_for_a_line_can_be_attached_at_once(self):
        """The sequence the grid performs: create, then point the line at
        it. A quote usually does not exist until someone is entering the
        line it belongs to."""
        line = self.add()[1]["id"]
        _s, quote = self.call("POST", "/api/procurement/quotes",
                              {"supplier_id": self.supplier,
                               "currency": "USD", "fx_rate": 1.388561,
                               "quote_ref": "11395"})
        status, _b = self.call("PATCH", f"/api/procurement/{line}",
                               {"supplier_quote_id": quote["id"]})
        self.assertEqual(status, 200)
        self.assertEqual(self.db.scalar(
            "SELECT quote_ref FROM v_procurement_line WHERE id = ?", (line,)),
            "11395")

    def test_the_lookups_come_with_the_list(self):
        """So a dialog opens without a round trip per field."""
        _s, body = self.call("GET", "/api/procurement")
        for key in ("projects", "suppliers", "quotes", "pos", "periods",
                    "invoices"):
            self.assertIn(key, body)


class TestStatingAStateWithoutADate(Case):
    """Most lines never get a date: somebody knows it arrived and says so.
    Requiring a date to record that would mean either inventing one or
    leaving the register out of step with reality."""

    def line(self):
        return self.add()[1]["id"]

    def test_a_state_can_be_set_with_no_date(self):
        line = self.line()
        status, _b = self.call("PATCH", f"/api/procurement/{line}",
                               {"stated_state": "delivered"})
        self.assertEqual(status, 200)
        row = self.db.query_one(
            "SELECT state, state_undated FROM v_procurement_line WHERE id = ?",
            (line,))
        self.assertEqual(row["state"], "delivered")
        self.assertEqual(row["state_undated"], 1)

    def test_an_unknown_state_is_refused(self):
        line = self.line()
        status, body = self.call("PATCH", f"/api/procurement/{line}",
                                 {"stated_state": "nearly there"})
        self.assertEqual(status, 400)
        self.assertIn("stated_state", body["detail"])

    def test_it_can_be_cleared(self):
        line = self.line()
        self.call("PATCH", f"/api/procurement/{line}",
                  {"stated_state": "delivered"})
        self.call("PATCH", f"/api/procurement/{line}", {"stated_state": ""})
        self.assertEqual(self.db.scalar(
            "SELECT state FROM v_procurement_line WHERE id = ?", (line,)),
            "to be ordered")

    def test_a_real_date_supersedes_it(self):
        """`When` beats `what someone said`, and leaving both would let them
        disagree."""
        line = self.line()
        self.call("PATCH", f"/api/procurement/{line}",
                  {"stated_state": "delivered"})
        self.call("PATCH", f"/api/procurement/{line}",
                  {"paid_date": "2026-07-25"})
        row = self.db.query_one(
            "SELECT state, state_undated, stated_state FROM v_procurement_line "
            "WHERE id = ?", (line,))
        self.assertEqual(row["state"], "paid, pending delivery")
        self.assertIsNone(row["stated_state"])
        self.assertEqual(row["state_undated"], 0)

    def test_the_vocabulary_is_the_registers(self):
        """It is what people say to each other, so it is what the dropdown
        offers."""
        _s, body = self.call("GET", "/api/procurement")
        self.assertIn("paid - pending delivery", body["states"])
        self.assertIn("delivered", body["states"])
        # Cancelling needs a reason, so it does not belong in a one-click
        # dropdown.
        self.assertNotIn("cancelled", body["states"])


class TestEstimatesOnTheScreen(Case):
    def test_the_totals_are_reported_apart(self):
        line = self.add()[1]["id"]
        self.call("PATCH", f"/api/procurement/{line}", {"is_estimate": True})
        self.add()
        _s, body = self.call("GET", "/api/procurement")
        self.assertEqual(body["totals"]["estimated_cents"], 4582)
        self.assertEqual(body["totals"]["committed_cents"], 4582)

    def test_an_estimate_can_be_made_real(self):
        line = self.add()[1]["id"]
        self.call("PATCH", f"/api/procurement/{line}", {"is_estimate": True})
        self.call("PATCH", f"/api/procurement/{line}", {"is_estimate": False})
        _s, body = self.call("GET", "/api/procurement")
        self.assertEqual(body["totals"]["estimated_cents"], 0)


class TestTheTotalsAnswerTheQuestion(Case):
    """Committed, paid and undelivered each leave something out — an
    estimate, a cancellation, or both — so none of them is the figure
    somebody means when they ask what this lot is worth."""

    def test_the_total_includes_estimates(self):
        line = self.add()[1]["id"]
        self.call("PATCH", f"/api/procurement/{line}", {"is_estimate": True})
        self.add()
        _s, body = self.call("GET", "/api/procurement")
        self.assertEqual(body["totals"]["total_cents"], 4582 * 2)
        self.assertEqual(body["totals"]["committed_cents"], 4582)
        self.assertEqual(body["totals"]["estimated_cents"], 4582)

    def test_the_total_excludes_a_cancelled_line(self):
        """A line nobody will pay for is not worth anything."""
        line = self.add()[1]["id"]
        self.call("PATCH", f"/api/procurement/{line}",
                  {"cancelled_date": "2026-07-06", "cancel_reason": "returned"})
        _s, body = self.call("GET", "/api/procurement")
        self.assertEqual(body["totals"]["total_cents"], 0)

    def test_the_current_year_comes_with_the_list(self):
        """So the grid opens on it. Worked out on the server, because a
        financial year computed in two places will eventually disagree with
        itself."""
        _s, body = self.call("GET", "/api/procurement")
        self.assertRegex(body["current_fy_label"], r"^FY\d\d$")


class TestDeletingALineThatShouldNotExist(Case):
    """DELETE is for a row that should never have existed — entered twice,
    or an estimate superseded by the real purchase. CANCEL is for one that
    was real and is not any more, and that leaves a trace on purpose."""

    roles = ("viewer", "operations", "approver")

    def test_a_plain_line_can_be_deleted_with_a_reason(self):
        line = self.add()[1]["id"]
        status, body = self.call("DELETE", f"/api/procurement/{line}",
                                 {"reason": "duplicate of the real order"})
        self.assertEqual(status, 200, body)
        self.assertIsNone(self.db.query_one(
            "SELECT id FROM procurement_line WHERE id = ?", (line,)))

    def test_a_reason_is_required(self):
        """A deletion nobody can question is a deletion nobody can
        check."""
        line = self.add()[1]["id"]
        status, body = self.call("DELETE", f"/api/procurement/{line}", {})
        self.assertEqual(status, 400)
        self.assertIn("reason", body["detail"])

    def test_the_whole_row_is_written_down_first(self):
        """A deletion nobody can reconstruct is a deletion nobody can
        question."""
        line = self.add(item="USR-N510-H7-4")[1]["id"]
        self.call("DELETE", f"/api/procurement/{line}",
                  {"reason": "entered twice"})
        row = self.db.query_one(
            "SELECT detail FROM audit_log WHERE action = 'procurement_delete'")
        self.assertIn("USR-N510-H7-4", row["detail"])
        self.assertIn("entered twice", row["detail"])

    def test_a_paid_line_is_refused(self):
        """Money that moved is cancelled, not erased."""
        line = self.add()[1]["id"]
        self.call("PATCH", f"/api/procurement/{line}",
                  {"paid_date": "2026-07-01"})
        status, body = self.call("DELETE", f"/api/procurement/{line}",
                                 {"reason": "oops"})
        self.assertEqual(status, 409)
        self.assertIn("cancel it", body["error"])

    def test_a_delivered_line_is_refused(self):
        line = self.add()[1]["id"]
        self.call("PATCH", f"/api/procurement/{line}",
                  {"delivered_date": "2026-07-20"})
        self.assertEqual(self.call("DELETE", f"/api/procurement/{line}",
                                   {"reason": "oops"})[0], 409)

    def test_a_line_on_an_invoice_is_refused(self):
        line = self.add()[1]["id"]
        self.call("POST", f"/api/procurement/{line}/invoice",
                  {"invoice_ref": "INV-1"})
        status, body = self.call("DELETE", f"/api/procurement/{line}",
                                 {"reason": "oops"})
        self.assertEqual(status, 409)
        self.assertIn("invoice", body["error"])

    def test_an_estimate_can_always_go(self):
        """Nothing was ordered, so nothing moved. This is the case it was
        built for: a $44,000 estimate superseded by a $39,600 purchase."""
        line = self.add()[1]["id"]
        self.call("PATCH", f"/api/procurement/{line}", {"is_estimate": True})
        self.assertEqual(self.call("DELETE", f"/api/procurement/{line}",
                                   {"reason": "superseded by the real order"})[0],
                         200)

    def test_operations_alone_cannot_delete(self):
        """It is the one action that leaves nothing on the screen to
        notice."""
        self.db.revoke_role(self.user["id"], 1, "approver", self.user["id"])
        line = self.add()[1]["id"]
        self.assertEqual(self.call("DELETE", f"/api/procurement/{line}",
                                   {"reason": "x"})[0], 403)


class TestPermissions(Case):
    roles = ("viewer",)

    def test_a_viewer_can_read(self):
        self.assertEqual(self.call("GET", "/api/procurement")[0], 200)

    def test_a_viewer_cannot_add_or_change(self):
        self.assertEqual(self.add()[0], 403)
        self.assertEqual(self.call("PATCH", "/api/procurement/1",
                                   {"paid_date": "2026-07-01"})[0], 403)
        self.assertEqual(self.call("POST", "/api/procurement/quotes",
                                   {"supplier_id": self.supplier})[0], 403)
        self.assertEqual(self.call("POST", "/api/procurement/pos",
                                   {"project_id": self.project})[0], 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
