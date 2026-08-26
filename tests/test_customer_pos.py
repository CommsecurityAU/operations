"""Customer POs — adding, varying, correcting.

The distinction that earns its keep is variation vs correction. In the data
both look like `amount_cents: X -> Y`; they differ only when someone asks
what orders in hand WAS on a past date, and reproducing a past position is
the thing this platform exists to do.
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
            c.execute("INSERT INTO client (entity_id,name) VALUES (1,'Hacer')")
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,created_ts)
                         VALUES (1,'120 Balmain Rd - ICN','JN-4335','DLP',0)""")
        self.project_id = self.db.scalar(
            "SELECT id FROM project WHERE job_code='JN-4335'")

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

    def add_po(self, **over):
        payload = {"amount_cents": 38000000, "po_number": "PO-1",
                   "issued_date": "2026-07-01"}
        payload.update(over)
        return self.call("POST", f"/api/projects/{self.project_id}/pos", payload)


class TestAdding(Case):
    def test_a_project_can_carry_several_pos(self):
        """Some jobs force a new PO per invoice, so this is normal rather
        than an edge case."""
        self.assertEqual(self.add_po(po_number="PO-1")[0], 201)
        self.assertEqual(self.add_po(po_number="PO-2", amount_cents=500000)[0], 201)
        _st, body = self.call("GET", f"/api/projects/{self.project_id}/pos")
        self.assertEqual(len(body["pos"]), 2)

    def test_orders_sum_to_what_has_been_ORDERED_not_the_contract(self):
        """The register's `Purchase Order` column was the CONTRACT VALUE.
        Treating POs as its components double-counted: on `200 Victoria -
        IBP` a $295,000 contract read $422,833 once four orders that were
        portions of it were summed alongside it."""
        self.add_po(po_number="PO-1", amount_cents=38000000)
        self.add_po(po_number="PO-2", amount_cents=500000)
        row = self.db.query_one(
            "SELECT * FROM v_project_orders_in_hand WHERE project_id = ?",
            (self.project_id,))
        self.assertEqual(row["ordered_cents"], 38500000)
        # The contract is the project's own figure and no order changed it.
        self.assertEqual(row["contract_value_cents"], 0)

    def test_orders_may_total_less_than_the_contract(self):
        """Which is the normal case on a job where POs arrive as the work
        does -- and the gap is what the customer has not yet ordered."""
        with self.db._tx() as c:
            c.execute("UPDATE project SET contract_value_cents = ? WHERE id = ?",
                      (50000000, self.project_id))
        self.add_po(po_number="PO-1", amount_cents=12000000)
        row = self.db.query_one(
            "SELECT * FROM v_project_orders_in_hand WHERE project_id = ?",
            (self.project_id,))
        self.assertEqual(row["contract_value_cents"], 50000000)
        self.assertEqual(row["ordered_cents"], 12000000)
        self.assertEqual(row["orders_in_hand_cents"], 50000000)   # nothing billed
        self.assertEqual(row["ordered_unbilled_cents"], 12000000)

    def test_a_placeholder_is_not_an_order(self):
        """The migrated rows carry claims and retention; they were never
        orders, so they must not appear in what has been ordered."""
        with self.db._tx() as c:
            c.execute("""INSERT INTO customer_po (entity_id, project_id,
                             amount_cents, note, is_placeholder, created_ts)
                         VALUES (1,?,29500000,'migrated',1,0)""",
                      (self.project_id,))
        self.add_po(po_number="PO-1", amount_cents=1191660)
        self.assertEqual(self.db.scalar(
            "SELECT ordered_cents FROM v_project_orders_in_hand "
            "WHERE project_id = ?", (self.project_id,)), 1191660)

    def test_a_duplicate_po_number_names_the_other_project(self):
        """So it is obvious whether it is a typo or the same order genuinely
        reaching two projects."""
        self.add_po(po_number="PO-1")
        with self.db._tx() as c:
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,created_ts)
                         VALUES (1,'Elsewhere','JN-9,9','Active',0)""")
        other = self.db.scalar("SELECT id FROM project WHERE name='Elsewhere'")
        status, body = self.call("POST", f"/api/projects/{other}/pos",
                                 {"amount_cents": 100, "po_number": "PO-1"})
        self.assertEqual(status, 400)
        self.assertIn("120 Balmain Rd - ICN", body["detail"]["po_number"])

    def test_a_po_may_have_no_number_yet(self):
        """Unknown, or a trial project at no cost. Both are real."""
        status, body = self.add_po(po_number="", amount_cents=0)
        self.assertEqual(status, 201)
        self.assertIsNone(body["po_number"])

    def test_an_absurd_amount_is_refused(self):
        status, body = self.add_po(amount_cents=999_999_999_00)
        self.assertEqual(status, 400)
        self.assertIn("amount_cents", body["detail"])


class TestThePanelPayload(Case):
    """The panel could not tell the contract from an order, so it counted
    five POs where there were four -- and reported $422,833 ordered against
    a $295,000 contract."""

    def test_the_list_says_which_rows_are_placeholders(self):
        with self.db._tx() as c:
            c.execute("""INSERT INTO customer_po (entity_id, project_id,
                             amount_cents, note, is_placeholder, created_ts)
                         VALUES (1,?,29500000,'migrated',1,0)""",
                      (self.project_id,))
        self.add_po(po_number="PO-1", amount_cents=1191660)
        _st, body = self.call("GET", f"/api/projects/{self.project_id}/pos")
        flags = sorted(p["is_placeholder"] for p in body["pos"])
        self.assertEqual(flags, [0, 1])

    def test_it_carries_the_contract_value(self):
        """So the heading states the contract rather than deriving it from
        rows that are not orders."""
        with self.db._tx() as c:
            c.execute("UPDATE project SET contract_value_cents = ? WHERE id = ?",
                      (29500000, self.project_id))
        _st, body = self.call("GET", f"/api/projects/{self.project_id}/pos")
        self.assertEqual(body["contract_value_cents"], 29500000)


class TestRemainingVersusForecast(Case):
    """Everything still to bill ought to sit in a month somewhere. Where it
    does not, the gap is either work nobody has forecast or a forecast that
    has outrun the contract -- both worth seeing on the project rather than
    discovered in a month-end total."""

    def setUp(self):
        super().setUp()
        with self.db._tx() as c:
            c.execute("UPDATE project SET contract_value_cents = ? WHERE id = ?",
                      (10000000, self.project_id))
        self.add_po(po_number="PO-1", amount_cents=10000000)
        self.po = self.db.scalar(
            "SELECT id FROM customer_po ORDER BY id DESC LIMIT 1")
        self.period = self.db.scalar("SELECT id FROM period LIMIT 1")

    def claim(self, cents, status="forecast"):
        with self.db._tx() as c:
            c.execute(
                """INSERT INTO claim_line (entity_id, project_id,
                       customer_po_id, period_id, status, amount_cents, created_ts)
                   VALUES (1,?,?,?,?,?,0)""",
                (self.project_id, self.po, self.period, status, cents))

    def panel(self):
        return self.call("GET", f"/api/projects/{self.project_id}/pos")[1]

    def test_a_fully_forecast_project_has_no_gap(self):
        self.claim(10000000)
        body = self.panel()
        self.assertEqual(body["remaining_cents"], body["forecast_cents"])

    def test_unforecast_work_shows_as_a_gap(self):
        self.claim(6000000)
        body = self.panel()
        self.assertEqual(body["remaining_cents"] - body["forecast_cents"], 4000000)

    def test_forecasting_beyond_the_contract_shows_the_other_way(self):
        """`88 Robertson St - QLD` plans $173,350 against $93,350 left."""
        self.claim(17000000)
        body = self.panel()
        self.assertLess(body["remaining_cents"], body["forecast_cents"])

    def test_invoicing_moves_both_sides_together(self):
        """A claim that becomes invoiced leaves the forecast and reduces
        what is left, so a project in step stays in step."""
        self.claim(10000000)
        before = self.panel()
        cid = self.db.scalar("SELECT id FROM claim_line LIMIT 1")
        self.db.transition_claim(cid, "due", {}, None, self.user["id"])
        self.db.transition_claim(cid, "approved", {}, None, self.user["id"])
        self.db.transition_claim(
            cid, "invoiced",
            {"invoice_number": "INV-1", "invoiced_date": "2026-09-22"},
            None, self.user["id"])
        after = self.panel()
        self.assertEqual(before["remaining_cents"] - after["remaining_cents"],
                         10000000)
        self.assertEqual(after["remaining_cents"], after["forecast_cents"])

    def test_due_and_approved_claims_count_as_planned(self):
        """They are still to bill and they sit in a month; excluding them
        would report committed work as unforecast."""
        self.claim(4000000, status="due")
        self.claim(6000000, status="approved")
        body = self.panel()
        self.assertEqual(body["forecast_cents"], 10000000)


class TestVariationVersusCorrection(Case):
    def po_id(self):
        self.add_po()
        return self.db.scalar("SELECT id FROM customer_po LIMIT 1")

    def test_a_variation_records_when_the_contract_changed(self):
        po = self.po_id()
        status, body = self.call("POST", f"/api/pos/{po}/revise", {
            "amount_cents": 42000000, "kind": "variation",
            "reason": "VO-3 additional readers", "effective_date": "2026-11-15"})
        self.assertEqual(status, 200, body)
        row = self.db.query_one(
            "SELECT * FROM customer_po_revision WHERE customer_po_id = ?", (po,))
        self.assertEqual(row["kind"], "variation")
        self.assertEqual(row["effective_date"], "2026-11-15")

    def test_a_variation_without_a_date_is_refused(self):
        """Without it a past position cannot be reproduced, which is the
        only reason to distinguish the two kinds at all."""
        po = self.po_id()
        status, body = self.call("POST", f"/api/pos/{po}/revise", {
            "amount_cents": 42000000, "kind": "variation", "reason": "VO-3"})
        self.assertEqual(status, 400)
        self.assertIn("effective_date", body["detail"])

    def test_a_correction_needs_no_date(self):
        """The figure was always wrong; there is no date on which it became
        wrong."""
        po = self.po_id()
        status, _b = self.call("POST", f"/api/pos/{po}/revise", {
            "amount_cents": 38500000, "kind": "correction",
            "reason": "transposed digits on entry"})
        self.assertEqual(status, 200)

    def test_both_need_a_reason(self):
        po = self.po_id()
        for kind in ("variation", "correction"):
            status, body = self.call("POST", f"/api/pos/{po}/revise", {
                "amount_cents": 1, "kind": kind, "effective_date": "2026-11-15"})
            self.assertEqual(status, 400, kind)
            self.assertIn("reason", body["detail"])

    def test_an_unknown_kind_is_refused(self):
        """It cannot be recovered from the numbers later, so guessing is
        worse than refusing."""
        po = self.po_id()
        status, body = self.call("POST", f"/api/pos/{po}/revise", {
            "amount_cents": 1, "kind": "adjustment", "reason": "x"})
        self.assertEqual(status, 400)
        self.assertIn("kind", body["detail"])

    def test_the_history_counts_them_separately(self):
        po = self.po_id()
        self.call("POST", f"/api/pos/{po}/revise", {
            "amount_cents": 42000000, "kind": "variation", "reason": "VO-3",
            "effective_date": "2026-11-15"})
        self.call("POST", f"/api/pos/{po}/revise", {
            "amount_cents": 42100000, "kind": "correction", "reason": "typo"})
        row = self.db.query_one(
            "SELECT * FROM v_customer_po_history WHERE customer_po_id = ?", (po,))
        self.assertEqual(row["variation_count"], 1)
        self.assertEqual(row["correction_count"], 1)

    def test_revising_to_the_same_amount_records_nothing(self):
        po = self.po_id()
        _st, body = self.call("POST", f"/api/pos/{po}/revise", {
            "amount_cents": 38000000, "kind": "correction", "reason": "x"})
        self.assertFalse(body["changed"])
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM customer_po_revision"), 0)

    def test_the_amount_cannot_be_changed_through_patch(self):
        """Otherwise the reason and the kind are optional in practice."""
        po = self.po_id()
        status, body = self.call("PATCH", f"/api/pos/{po}",
                                 {"amount_cents": 1})
        self.assertEqual(status, 400)
        self.assertIn("use revise", body["detail"]["amount_cents"])

    def test_a_variation_raises_the_retention_cap(self):
        po = self.po_id()
        self.call("PATCH", f"/api/pos/{po}", {
            "retention_applies": 1, "retention_rate_bp": 1000,
            "retention_cap_bp": 500})
        before = self.db.scalar(
            "SELECT cap_cents FROM v_po_retention WHERE customer_po_id = ?", (po,))
        self.call("POST", f"/api/pos/{po}/revise", {
            "amount_cents": 42000000, "kind": "variation", "reason": "VO-3",
            "effective_date": "2026-11-15"})
        after = self.db.scalar(
            "SELECT cap_cents FROM v_po_retention WHERE customer_po_id = ?", (po,))
        self.assertEqual(before, 1900000)      # 5% of $380,000
        self.assertEqual(after, 2100000)       # 5% of $420,000


class TestEditing(Case):
    """Everything except the value. Changing that says whether the contract
    grew or the figure was wrong, which needs `revise` and a reason."""

    def a_po(self):
        self.add_po()
        return self.db.scalar("SELECT id FROM customer_po ORDER BY id DESC LIMIT 1")

    def test_the_number_date_and_note_can_be_corrected(self):
        po = self.a_po()
        status, body = self.call("PATCH", f"/api/pos/{po}", {
            "po_number": "PO06932420_255549", "issued_date": "2026-05-15",
            "note": "Preliminary works"})
        self.assertEqual(status, 200, body)
        self.assertEqual(sorted(body["changed"]),
                         ["issued_date", "note", "po_number"])

    def test_retention_terms_belong_to_the_order(self):
        """A second PO on a project can carry its own terms, or none. There
        was no way to set them before -- `sync_register` works at project
        level, so an order added by hand could never have retention."""
        po = self.a_po()
        self.call("PATCH", f"/api/pos/{po}", {
            "retention_applies": 1, "retention_rate_bp": 1000,
            "retention_cap_bp": 500, "release_policy": "split",
            "release_split_bp": 5000})
        row = self.db.query_one(
            "SELECT * FROM v_po_retention WHERE customer_po_id = ?", (po,))
        self.assertTrue(row["retention_applies"])
        self.assertEqual(row["cap_cents"], 1900000)      # 5% of $380,000

    def test_two_orders_on_one_project_can_differ(self):
        first = self.a_po()
        self.add_po(po_number="PO-2", amount_cents=1000000)
        second = self.db.scalar(
            "SELECT id FROM customer_po ORDER BY id DESC LIMIT 1")
        self.call("PATCH", f"/api/pos/{first}", {
            "retention_applies": 1, "retention_rate_bp": 1000,
            "retention_cap_bp": 500})
        self.assertTrue(self.db.scalar(
            "SELECT retention_applies FROM customer_po WHERE id = ?", (first,)))
        self.assertFalse(self.db.scalar(
            "SELECT retention_applies FROM customer_po WHERE id = ?", (second,)))

    def test_turning_retention_off_clears_its_terms(self):
        """Leaving a stale cap behind would report a percentage of an order
        nobody is holding money against."""
        po = self.a_po()
        self.call("PATCH", f"/api/pos/{po}", {
            "retention_applies": 1, "retention_cap_bp": 500,
            "retention_rate_bp": 1000})
        self.call("PATCH", f"/api/pos/{po}", {
            "retention_applies": 0, "retention_cap_bp": None,
            "retention_rate_bp": None, "release_policy": None,
            "release_split_bp": None})
        row = self.db.query_one(
            "SELECT * FROM v_po_retention WHERE customer_po_id = ?", (po,))
        self.assertFalse(row["retention_applies"])
        self.assertEqual(row["cap_cents"], 0)

    def test_the_value_still_cannot_be_changed_here(self):
        po = self.a_po()
        status, body = self.call("PATCH", f"/api/pos/{po}",
                                 {"amount_cents": 1})
        self.assertEqual(status, 400)
        self.assertIn("use revise", body["detail"]["amount_cents"])

    def test_every_field_change_is_audited(self):
        po = self.a_po()
        self.call("PATCH", f"/api/pos/{po}", {"po_number": "PO-NEW"})
        row = self.db.query_one(
            "SELECT detail FROM audit_log WHERE action='po_update'")
        self.assertIn("PO-NEW", row["detail"])


class TestMoving(Case):
    """Putting a PO on the wrong project is an ordinary slip made while
    typing. Requiring an admin to undo it would make the mistake more
    expensive than it is."""

    def other_project(self):
        with self.db._tx() as c:
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,created_ts)
                         VALUES (1,'120 Balmain Rd - SBP','JN-4336','DLP',0)""")
        return self.db.scalar("SELECT id FROM project WHERE job_code='JN-4336'")

    def a_po(self):
        self.add_po()
        return self.db.scalar("SELECT id FROM customer_po ORDER BY id DESC LIMIT 1")

    def test_a_po_with_no_claims_moves(self):
        po, target = self.a_po(), self.other_project()
        status, body = self.call("POST", f"/api/pos/{po}/move",
                                 {"project_id": target})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["to"], "120 Balmain Rd - SBP")
        self.assertEqual(self.db.scalar(
            "SELECT project_id FROM customer_po WHERE id = ?", (po,)), target)

    def test_what_is_ORDERED_follows_it_and_the_contract_does_not(self):
        """Moving an order moves what has been ordered. It does not move the
        contract, which belongs to the project and describes the job."""
        po, target = self.a_po(), self.other_project()
        with self.db._tx() as c:
            c.execute("UPDATE project SET contract_value_cents = ? WHERE id = ?",
                      (38000000, self.project_id))
        self.call("POST", f"/api/pos/{po}/move", {"project_id": target})
        here = self.db.query_one(
            "SELECT * FROM v_project_orders_in_hand WHERE project_id = ?",
            (self.project_id,))
        there = self.db.query_one(
            "SELECT * FROM v_project_orders_in_hand WHERE project_id = ?",
            (target,))
        self.assertEqual(here["ordered_cents"], 0)
        self.assertEqual(there["ordered_cents"], 38000000)
        self.assertEqual(here["contract_value_cents"], 38000000)
        self.assertEqual(there["contract_value_cents"], 0)

    def test_a_po_with_claims_is_refused(self):
        """A claim carries both the project and the PO. Moving one without
        the other leaves them disagreeing about which project the work
        belongs to."""
        po, target = self.a_po(), self.other_project()
        period = self.db.scalar("SELECT id FROM period LIMIT 1")
        with self.db._tx() as c:
            c.execute(
                """INSERT INTO claim_line (entity_id, project_id,
                       customer_po_id, period_id, status, amount_cents, created_ts)
                   VALUES (1,?,?,?, 'forecast', 1000, 0)""",
                (self.project_id, po, period))
        status, body = self.call("POST", f"/api/pos/{po}/move",
                                 {"project_id": target})
        self.assertEqual(status, 409)
        self.assertIn("re-point them first", body["error"])

    def test_it_cannot_cross_entities(self):
        """Entities are separate legal companies; an order does not move
        between them by being dragged."""
        po = self.a_po()
        with self.db._tx() as c:
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,created_ts)
                         VALUES (2,'Elsewhere','JN-9,9','Active',0)""")
        other = self.db.scalar("SELECT id FROM project WHERE name='Elsewhere'")
        status, _b = self.call("POST", f"/api/pos/{po}/move",
                               {"project_id": other})
        self.assertEqual(status, 400)

    def test_the_move_is_audited_with_both_ends(self):
        po, target = self.a_po(), self.other_project()
        self.call("POST", f"/api/pos/{po}/move", {"project_id": target})
        row = self.db.query_one(
            "SELECT detail FROM audit_log WHERE action='po_move'")
        self.assertIn("120 Balmain Rd - ICN", row["detail"])
        self.assertIn("120 Balmain Rd - SBP", row["detail"])

    def test_operations_may_move_without_being_admin(self):
        po, target = self.a_po(), self.other_project()
        self.assertNotIn("admin", [r["role"] for r in
                                   self.db.roles_for(self.user["id"])])
        self.assertEqual(
            self.call("POST", f"/api/pos/{po}/move", {"project_id": target})[0], 200)


class TestDeleting(Case):
    roles = ("viewer", "operations", "admin")

    def test_a_po_with_no_claims_can_be_removed(self):
        self.add_po()
        po = self.db.scalar("SELECT id FROM customer_po LIMIT 1")
        self.assertEqual(self.call("DELETE", f"/api/pos/{po}")[0], 204)

    def test_a_po_with_claims_is_refused(self):
        """It is history: claims reference it, and removing it would orphan
        them."""
        self.add_po()
        po = self.db.scalar("SELECT id FROM customer_po LIMIT 1")
        period = self.db.scalar("SELECT id FROM period LIMIT 1")
        with self.db._tx() as c:
            c.execute(
                """INSERT INTO claim_line (entity_id, project_id,
                       customer_po_id, period_id, status, amount_cents, created_ts)
                   VALUES (1,?,?,?, 'forecast', 1000, 0)""",
                (self.project_id, po, period))
        status, body = self.call("DELETE", f"/api/pos/{po}")
        self.assertEqual(status, 409)
        self.assertIn("billed against", body["error"])


class TestPermissions(Case):
    roles = ("viewer",)

    def test_a_viewer_can_read_but_not_write(self):
        self.assertEqual(
            self.call("GET", f"/api/projects/{self.project_id}/pos")[0], 200)
        self.assertEqual(self.add_po()[0], 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
