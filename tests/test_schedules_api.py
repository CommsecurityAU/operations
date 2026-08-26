"""Schedules over HTTP — adoption, generation, renewals.

Every recurring project in the register arrived with its twelve rows
already typed. Generating over the top would double the year, so most of
these tests are about the schedule recognising work that already exists.
"""

import collections
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

MONTHLY = 189075        # $1,890.75 — 36 Wellington


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
            c.execute("INSERT INTO client (entity_id,name) VALUES (1,'Hines')")
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,created_ts)
                         VALUES (1,'36 Wellington','JN-3579','SLA',0)""")
        self.project_id = self.db.scalar(
            "SELECT id FROM project WHERE job_code='JN-3579'")
        with self.db._tx() as c:
            c.execute("""INSERT INTO customer_po (entity_id,project_id,
                             amount_cents,created_ts) VALUES (1,?,2268900,0)""",
                      (self.project_id,))
        self.po_id = self.db.scalar(
            "SELECT id FROM customer_po WHERE project_id=?", (self.project_id,))
        self.jul26 = self.period("2026-07-01")
        self.jun27 = self.period("2027-06-01")

    def tearDown(self):
        self.sched.stop()
        self.server.shutdown()
        self.server.server_close()
        self.t.join(timeout=5)
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def period(self, month_start):
        return self.db.scalar("SELECT id FROM period WHERE month_start = ?",
                              (month_start,))

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
                   "description": "ICN monthly Maintenance",
                   "amount_cents": MONTHLY, "frequency": "monthly",
                   "start_period_id": self.jul26, "end_period_id": self.jun27,
                   "renewal_date": "2027-05-01"}
        payload.update(over)
        return payload

    def hand_entered(self, months=12, amount=MONTHLY):
        """The twelve rows someone already typed."""
        rows = self.db.query(
            """SELECT id FROM period WHERE month_start >= '2026-07-01'
               ORDER BY month_start LIMIT ?""", (months,))
        with self.db._tx() as c:
            for row in rows:
                c.execute(
                    """INSERT INTO claim_line (entity_id, project_id,
                           customer_po_id, period_id, status, amount_cents,
                           detail, created_ts)
                       VALUES (1,?,?,?, 'forecast', ?, 'Maintenance', 0)""",
                    (self.project_id, self.po_id, row["id"], amount))
        return len(rows)


class TestCreate(Case):
    def test_it_creates_and_adopts_in_one_step(self):
        """A schedule that appeared to cover nothing would invite someone to
        press Generate and double the year."""
        self.hand_entered()
        status, body = self.call("POST", "/api/schedules", self.valid())
        self.assertEqual(status, 201, body)
        self.assertEqual(body["adopted"]["adopted"], 12)
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM claim_line WHERE schedule_id IS NOT NULL"), 12)

    def test_adoption_creates_nothing(self):
        before = self.hand_entered()
        self.call("POST", "/api/schedules", self.valid())
        self.assertEqual(
            self.db.scalar("SELECT COUNT(*) FROM claim_line"), before)

    def test_a_description_is_required(self):
        """It becomes the detail on every claim the schedule makes."""
        status, body = self.call("POST", "/api/schedules",
                                 self.valid(description="  "))
        self.assertEqual(status, 400)
        self.assertIn("description", body["detail"])

    def test_a_backwards_span_is_refused(self):
        status, body = self.call("POST", "/api/schedules",
                                 self.valid(start_period_id=self.jun27,
                                            end_period_id=self.jul26))
        self.assertEqual(status, 400)
        self.assertIn("end before it starts", body["detail"]["end_period_id"])

    def test_a_po_from_another_project_is_refused(self):
        with self.db._tx() as c:
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,created_ts)
                         VALUES (1,'Other','JN-9,9','Active',0)""")
        other = self.db.scalar("SELECT id FROM project WHERE name='Other'")
        with self.db._tx() as c:
            c.execute("""INSERT INTO customer_po (entity_id,project_id,
                             amount_cents,created_ts) VALUES (1,?,100,0)""",
                      (other,))
        other_po = self.db.scalar(
            "SELECT id FROM customer_po ORDER BY id DESC LIMIT 1")
        status, _b = self.call("POST", "/api/schedules",
                               self.valid(customer_po_id=other_po))
        self.assertEqual(status, 400)


class TestAdoptAndGenerate(Case):
    def make(self, **over):
        return self.call("POST", "/api/schedules", self.valid(**over))[1]["schedule"]

    def test_generate_fills_only_the_gaps(self):
        """Half the year typed, half not."""
        self.hand_entered(months=6)
        s = self.make()
        status, body = self.call("POST", f"/api/schedules/{s['id']}/generate")
        self.assertEqual(status, 200)
        self.assertEqual(body["created"], 6)
        self.assertEqual(body["existing"], 6)
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM claim_line WHERE schedule_id = ?",
            (s["id"],)), 12)

    def test_generating_twice_creates_nothing_more(self):
        s = self.make()
        self.call("POST", f"/api/schedules/{s['id']}/generate")
        _st, again = self.call("POST", f"/api/schedules/{s['id']}/generate")
        self.assertEqual(again["created"], 0)

    def test_a_differing_amount_is_reported_not_corrected(self):
        """A month where the charge differed is a fact about that month.
        The schedule does not get to overwrite it."""
        self.hand_entered(months=3, amount=250000)
        _st, body = self.call("POST", "/api/schedules", self.valid())
        self.assertEqual(body["adopted"]["adopted"], 3)
        self.assertEqual(len(body["adopted"]["differing"]), 3)
        self.assertEqual(body["adopted"]["differing"][0]["claim_cents"], 250000)
        self.assertEqual(self.db.scalar(
            "SELECT DISTINCT amount_cents FROM claim_line "
            "WHERE schedule_id IS NOT NULL"), 250000)

    def test_adopting_twice_adopts_nothing_the_second_time(self):
        self.hand_entered()
        s = self.make()
        _st, again = self.call("POST", f"/api/schedules/{s['id']}/adopt")
        self.assertEqual(again["adopted"], 0)

    def test_an_inactive_schedule_refuses_to_generate(self):
        s = self.make()
        self.call("PATCH", f"/api/schedules/{s['id']}", {"is_active": 0})
        status, _b = self.call("POST", f"/api/schedules/{s['id']}/generate")
        self.assertEqual(status, 409)

    def test_preview_says_what_generate_would_do(self):
        """Pressing Generate should never be how you find out."""
        self.hand_entered(months=4)
        s = self.make()
        _st, body = self.call("GET", f"/api/schedules/{s['id']}/preview")
        states = [p["state"] for p in body["periods"]]
        self.assertEqual(states.count("mine"), 4)
        self.assertEqual(states.count("missing"), 8)


class TestAMonthWithTwoClaims(Case):
    """`200 Victoria - ICN Maintenance` carries a $0.00 invoiced row
    alongside its monthly maintenance in Jul-26. The unique index allows a
    schedule ONE claim per period, so adopting the second breached it and
    returned a 500 that said only "internal error"."""

    def make(self, **over):
        return self.call("POST", "/api/schedules", self.valid(**over))[1]["schedule"]

    def two_in_july(self):
        self.hand_entered()
        with self.db._tx() as c:
            c.execute(
                """INSERT INTO claim_line (entity_id, project_id,
                       customer_po_id, period_id, status, amount_cents,
                       detail, created_ts)
                   VALUES (1,?,?,?, 'invoiced', 0, 'Service order', 0)""",
                (self.project_id, self.po_id, self.jul26))

    def test_adopting_twice_does_not_fail(self):
        self.two_in_july()
        s = self.make()
        status, body = self.call("POST", f"/api/schedules/{s['id']}/adopt")
        self.assertEqual(status, 200, body)

    def test_the_leftover_claim_is_reported(self):
        """Not silently ignored: a month with two claims is either a genuine
        extra or a duplicate, and both want a human."""
        self.two_in_july()
        s = self.make()
        _st, body = self.call("POST", f"/api/schedules/{s['id']}/adopt")
        self.assertEqual(len(body["not_adopted"]), 1)
        self.assertEqual(body["not_adopted"][0]["period"], "Jul-26")

    def test_the_schedule_owns_exactly_one_claim_per_period(self):
        self.two_in_july()
        s = self.make()
        self.call("POST", f"/api/schedules/{s['id']}/adopt")
        self.call("POST", f"/api/schedules/{s['id']}/adopt")
        rows = self.db.query(
            """SELECT period_id, COUNT(*) n FROM claim_line
               WHERE schedule_id = ? GROUP BY period_id HAVING n > 1""",
            (s["id"],))
        self.assertEqual(rows, [])

    def test_the_second_claim_keeps_its_own_life(self):
        """Unattached is not deleted -- the $0.00 row is still a claim."""
        self.two_in_july()
        before = self.db.scalar("SELECT COUNT(*) FROM claim_line")
        s = self.make()
        self.call("POST", f"/api/schedules/{s['id']}/adopt")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM claim_line"), before)


class TestCoverage(Case):
    """`12 claims` says nothing about whether that is all of them. The
    fraction is the only thing that answers "should I press Generate"."""

    def make(self, **over):
        return self.call("POST", "/api/schedules", self.valid(**over))[1]["schedule"]

    def test_a_complete_year_reports_twelve_of_twelve(self):
        self.hand_entered()
        self.make()
        _st, body = self.call("GET", "/api/schedules")
        row = body["schedules"][0]
        self.assertEqual(row["generated_count"], 12)
        self.assertEqual(row["expected_count"], 12)

    def test_a_half_covered_year_reports_the_gap(self):
        self.hand_entered(months=6)
        self.make()
        _st, body = self.call("GET", "/api/schedules")
        row = body["schedules"][0]
        self.assertEqual(row["generated_count"], 6)
        self.assertEqual(row["expected_count"], 12)

    def test_quarterly_expects_four_not_twelve(self):
        """The expected count follows the frequency, not the span."""
        self.make(frequency="quarterly")
        _st, body = self.call("GET", "/api/schedules")
        self.assertEqual(body["schedules"][0]["expected_count"], 4)

    def test_generating_closes_the_gap(self):
        self.hand_entered(months=6)
        s = self.make()
        self.call("POST", f"/api/schedules/{s['id']}/generate")
        _st, body = self.call("GET", "/api/schedules")
        row = body["schedules"][0]
        self.assertEqual(row["generated_count"], row["expected_count"])


class TestPreview(Case):
    """What Generate would do, without pressing it. The endpoint existed
    from the start and nothing called it, which meant the only way to learn
    what a schedule covered was to act on it."""

    def make(self, **over):
        return self.call("POST", "/api/schedules", self.valid(**over))[1]["schedule"]

    def test_it_lists_every_period_the_schedule_covers(self):
        s = self.make()
        _st, body = self.call("GET", f"/api/schedules/{s['id']}/preview")
        self.assertEqual(len(body["periods"]), 12)
        self.assertEqual(body["periods"][0]["label"], "Jul-26")

    def test_it_distinguishes_mine_unattached_and_missing(self):
        self.hand_entered(months=4)
        s = self.make()                       # adopts the four
        with self.db._tx() as c:
            c.execute(
                """INSERT INTO claim_line (entity_id, project_id,
                       customer_po_id, period_id, status, amount_cents,
                       created_ts)
                   SELECT 1, ?, ?, id, 'forecast', 999, 0 FROM period
                   WHERE month_start = '2026-12-01'""",
                (self.project_id, self.po_id))
        _st, body = self.call("GET", f"/api/schedules/{s['id']}/preview")
        states = collections.Counter(p["state"] for p in body["periods"])
        self.assertEqual(states["mine"], 4)
        self.assertEqual(states["unattached"], 1)
        self.assertEqual(states["missing"], 7)

    def test_it_shows_the_other_claims_in_a_month(self):
        """A period showing one claim when it holds two is how the Jul-26
        surprise happened."""
        self.hand_entered()
        with self.db._tx() as c:
            c.execute(
                """INSERT INTO claim_line (entity_id, project_id,
                       customer_po_id, period_id, status, amount_cents,
                       created_ts)
                   VALUES (1,?,?,?, 'invoiced', 0, 0)""",
                (self.project_id, self.po_id, self.jul26))
        s = self.make()
        _st, body = self.call("GET", f"/api/schedules/{s['id']}/preview")
        july = body["periods"][0]
        self.assertEqual(july["state"], "mine")
        self.assertEqual(len(july["others"]), 1)

    def test_a_viewer_may_preview(self):
        """Reading what a schedule covers is not a write."""
        s = self.make()
        self.db._write.execute(
            "DELETE FROM user_entity_role WHERE role = 'operations'")
        self.db._write.commit()
        self.assertEqual(
            self.call("GET", f"/api/schedules/{s['id']}/preview")[0], 200)


class TestRenewals(Case):
    def test_an_overdue_renewal_sorts_first(self):
        """A lapsed agreement is more urgent than one due next month, and
        dropping it off the end is how revenue quietly stops."""
        self.call("POST", "/api/schedules", self.valid(renewal_date="2099-01-01"))
        self.call("POST", "/api/schedules",
                  self.valid(description="Lapsed", renewal_date="2020-01-01"))
        _st, body = self.call("GET", "/api/schedules")
        self.assertEqual(body["schedules"][0]["renewal_state"], "overdue")

    def test_an_inactive_schedule_is_still_listed(self):
        """It is excluded from the renewals view; listing it here is what
        keeps it reachable rather than lost."""
        s = self.call("POST", "/api/schedules", self.valid())[1]["schedule"]
        self.call("PATCH", f"/api/schedules/{s['id']}", {"is_active": 0})
        _st, body = self.call("GET", "/api/schedules")
        self.assertEqual(len(body["schedules"]), 1)
        self.assertEqual(body["schedules"][0]["is_active"], 0)


class TestPermissions(Case):
    roles = ("viewer",)

    def test_a_viewer_can_read(self):
        self.assertEqual(self.call("GET", "/api/schedules")[0], 200)

    def test_a_viewer_cannot_create_or_generate(self):
        self.assertEqual(self.call("POST", "/api/schedules", self.valid())[0], 403)
        self.assertEqual(
            self.call("POST", "/api/schedules/1/generate")[0], 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
