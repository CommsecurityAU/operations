"""ops.modules.worklist — resolving ambiguous job codes.

The worklist reaching zero is STP-5's gate, so what matters is that closing
an issue means something happened: a number issued, or a reason recorded.
Closing one by clicking a button and writing nothing down would satisfy the
gate while leaving the register exactly as ambiguous as before.
"""

import http.client
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import import_register as imp  # noqa: E402
from ops import auth  # noqa: E402
from ops.config import Config  # noqa: E402
from ops.db import Db  # noqa: E402
from ops.main import boot  # noqa: E402
from ops.secrets import LocalProvider  # noqa: E402

FIXTURE = os.path.join(ROOT, "tests", "fixtures", "project_register_fy27.csv")


class Case(unittest.TestCase):
    roles = ("viewer", "operations")

    def setUp(self):
        for n in ("ops.http", "ops.main", "ops.auth"):
            logging.getLogger(n).setLevel(logging.CRITICAL)
        self.dir = tempfile.mkdtemp()
        db_path = os.path.join(self.dir, "ops.db")
        Db(db_path, os.path.join(ROOT, "ops", "migrations")).migrate()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys=ON")
        imp.load(conn, imp.validate(imp.read_rows(FIXTURE)))
        conn.commit()
        conn.close()

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
        self.user = auth.sign_in(self.db, {
            "sub": "s1", "email": "r@x", "name": "R"})
        for role in self.roles:
            self.db.grant_role(self.user["id"], 1, role, self.user["id"])

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

    def issues(self):
        return self.call("GET", "/api/worklist")[1]["issues"]

    def by_class(self, cls):
        return [i for i in self.issues() if i["class"] == cls]


class TestWorklistContents(Case):
    def test_the_real_register_produces_the_documented_worklist(self):
        """Pinned to the validated source: 6 placeholders and 4 projects
        sharing 2 codes."""
        data = self.call("GET", "/api/worklist")[1]
        self.assertEqual(data["open"], 10)
        self.assertEqual(len(self.by_class("B")), 6)
        self.assertEqual(len(self.by_class("C")), 4)
        self.assertEqual(len(self.by_class("A")), 0)

    def test_class_c_reports_how_many_projects_share_the_code(self):
        for issue in self.by_class("C"):
            self.assertEqual(issue["shared_by"], 2, issue["raw_code"])

    def test_placeholders_count_as_repeated_not_shared(self):
        """Five projects hold the string "TBA". That is a repeated
        placeholder, not a shared code -- the screen must not present it as
        sharing, because the remedy is completely different."""
        tba = [i for i in self.by_class("B") if i["raw_code"] == "TBA"]
        self.assertEqual(len(tba), 5)
        self.assertEqual(tba[0]["shared_by"], 5)
        with open(os.path.join(ROOT, "ops", "static", "worklist.js"),
                  encoding="utf-8") as f:
            ui = f.read()
        self.assertIn('cls === "C" && i.shared_by > 1', ui)

    def test_reissuing_a_class_c_needs_no_typed_reason(self):
        """Migration 001 requires a reason on any resolved class C row, and
        for `issue` the reason IS the action -- the code stops being shared.
        Demanding a typed justification blocked the most natural response to
        a shared code, and the schema CHECK then rejected the row anyway."""
        issue = self.by_class("C")[0]
        status, body = self.call("POST", f"/api/worklist/{issue['id']}/resolve",
                                 {"action": "issue"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["job_code"].startswith("JN-"))
        recorded = self.db.query_one(
            "SELECT reason, status FROM job_code_issue WHERE id = ?",
            (issue["id"],))
        self.assertEqual(recorded["status"], "resolved")
        self.assertIn("reissued as", recorded["reason"])

    def test_keep_still_demands_a_reason(self):
        """The judgement calls are different: `keep` leaves two projects on
        one code, which the next reader will query."""
        issue = self.by_class("C")[0]
        status, body = self.call("POST", f"/api/worklist/{issue['id']}/resolve",
                                 {"action": "keep"})
        self.assertEqual(status, 400)
        self.assertIn("reason", body["detail"])

    def test_the_next_job_number_is_shown_before_committing_to_it(self):
        data = self.call("GET", "/api/worklist")[1]
        self.assertTrue(data["next_job_code"].startswith("JN-"))
        # Previewing must not consume it.
        again = self.call("GET", "/api/worklist")[1]
        self.assertEqual(data["next_job_code"], again["next_job_code"])

    def test_each_class_carries_its_own_explanation(self):
        data = self.call("GET", "/api/worklist")[1]
        self.assertIn("B", data["help"])
        self.assertIn("C", data["help"])


class TestIssueANumber(Case):
    def test_issuing_replaces_a_placeholder_and_closes_the_issue(self):
        issue = self.by_class("B")[0]
        before = self.db.scalar("SELECT next_value FROM job_number_sequence")
        status, result = self.call(
            "POST", f"/api/worklist/{issue['id']}/resolve", {"action": "issue"})
        self.assertEqual(status, 200)
        self.assertEqual(result["job_code"], f"JN-{before}")
        self.assertEqual(
            self.db.scalar("SELECT job_code FROM project WHERE id=?",
                           (issue["project_id"],)), f"JN-{before}")
        self.assertEqual(self.db.scalar(
            "SELECT status FROM job_code_issue WHERE id=?", (issue["id"],)),
            "resolved")

    def test_the_flag_clears_on_the_project(self):
        """Two places holding the same fact is how a register shows a flag
        with nothing behind it."""
        issue = self.by_class("B")[0]
        self.assertEqual(self.db.scalar(
            "SELECT needs_resolution FROM project WHERE id=?",
            (issue["project_id"],)), 1)
        self.call("POST", f"/api/worklist/{issue['id']}/resolve",
                  {"action": "issue"})
        self.assertEqual(self.db.scalar(
            "SELECT needs_resolution FROM project WHERE id=?",
            (issue["project_id"],)), 0)

    def test_a_placeholder_is_not_kept_as_an_alias(self):
        """Five projects all aliased from "TBA" would be worse than nothing:
        the alias exists to preserve history, and a placeholder has none."""
        issue = [i for i in self.by_class("B") if i["raw_code"] == "TBA"][0]
        self.call("POST", f"/api/worklist/{issue['id']}/resolve",
                  {"action": "issue"})
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM job_code_alias WHERE legacy_code='TBA'"), 0)

    def test_resolution_is_audited(self):
        issue = self.by_class("B")[0]
        self.call("POST", f"/api/worklist/{issue['id']}/resolve",
                  {"action": "issue"})
        row = self.db.query_one(
            "SELECT detail FROM audit_log WHERE action='worklist_resolve'")
        self.assertIn("issue", row["detail"])

    def test_issuing_twice_is_refused(self):
        issue = self.by_class("B")[0]
        self.call("POST", f"/api/worklist/{issue['id']}/resolve",
                  {"action": "issue"})
        status, _ = self.call("POST", f"/api/worklist/{issue['id']}/resolve",
                              {"action": "issue"})
        self.assertEqual(status, 409)


class TestSharedCodes(Case):
    def test_keeping_a_shared_code_requires_a_reason(self):
        """Class C often IS correct — same site, two work types. But an
        unexplained shared code gets re-raised in six months."""
        issue = self.by_class("C")[0]
        status, body = self.call(
            "POST", f"/api/worklist/{issue['id']}/resolve", {"action": "keep"})
        self.assertEqual(status, 400)
        self.assertIn("reason", body["detail"])

    def test_keeping_with_a_reason_records_it(self):
        issue = self.by_class("C")[0]
        status, _ = self.call(
            "POST", f"/api/worklist/{issue['id']}/resolve",
            {"action": "keep",
             "reason": "One customer PO covers ICN and IBP at this site"})
        self.assertEqual(status, 200)
        self.assertEqual(self.db.scalar(
            "SELECT reason FROM job_code_issue WHERE id=?", (issue["id"],)),
            "One customer PO covers ICN and IBP at this site")

    def test_reissuing_one_auto_closes_the_sibling(self):
        """A class C issue asserts "this code covers two projects". Once one
        is reissued the assertion is false, so leaving the sibling open would
        mean the worklist claims something untrue."""
        code = self.by_class("C")[0]["raw_code"]
        pair = [i for i in self.by_class("C") if i["raw_code"] == code]
        self.assertEqual(len(pair), 2)
        status, result = self.call(
            "POST", f"/api/worklist/{pair[0]['id']}/resolve",
            {"action": "issue", "reason": "split by work type"})
        self.assertEqual(status, 200)
        self.assertEqual(result["cascaded"], [pair[1]["id"]])
        self.assertEqual(self.db.scalar(
            "SELECT reason FROM job_code_issue WHERE id=?", (pair[1]["id"],)),
            "code is no longer shared")

    def test_the_cascade_is_audited_not_silent(self):
        code = self.by_class("C")[0]["raw_code"]
        pair = [i for i in self.by_class("C") if i["raw_code"] == code]
        self.call("POST", f"/api/worklist/{pair[0]['id']}/resolve",
                  {"action": "issue", "reason": "split"})
        rows = self.db.query(
            "SELECT detail FROM audit_log WHERE action='worklist_resolve'")
        self.assertTrue(any("no longer shared" in r["detail"] for r in rows))

    def test_a_reissued_real_code_IS_kept_as_an_alias(self):
        """Unlike a placeholder, a real legacy code is history: invoices and
        emails reference it, so it has to remain findable."""
        issue = self.by_class("C")[0]
        old = issue["raw_code"]
        self.call("POST", f"/api/worklist/{issue['id']}/resolve",
                  {"action": "issue", "reason": "split by work type"})
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM job_code_alias WHERE legacy_code=? "
            "AND project_id=?", (old, issue["project_id"])), 1)


class TestAssignAndDismiss(Case):
    def test_assign_sets_a_specific_code(self):
        issue = self.by_class("B")[0]
        status, result = self.call(
            "POST", f"/api/worklist/{issue['id']}/resolve",
            {"action": "assign", "job_code": "JN-1234"})
        self.assertEqual(status, 200)
        self.assertEqual(result["job_code"], "JN-1234")

    def test_assign_without_a_code_is_refused(self):
        issue = self.by_class("B")[0]
        status, body = self.call(
            "POST", f"/api/worklist/{issue['id']}/resolve", {"action": "assign"})
        self.assertEqual(status, 400)
        self.assertIn("job_code", body["detail"])

    def test_assign_does_not_consume_the_sequence(self):
        issue = self.by_class("B")[0]
        before = self.db.scalar("SELECT next_value FROM job_number_sequence")
        self.call("POST", f"/api/worklist/{issue['id']}/resolve",
                  {"action": "assign", "job_code": "JN-1234"})
        self.assertEqual(
            self.db.scalar("SELECT next_value FROM job_number_sequence"), before)

    def test_dismiss_requires_a_reason(self):
        issue = self.by_class("B")[0]
        status, body = self.call(
            "POST", f"/api/worklist/{issue['id']}/resolve", {"action": "dismiss"})
        self.assertEqual(status, 400)
        self.assertIn("reason", body["detail"])

    def test_dismiss_records_it_as_dismissed_not_resolved(self):
        """The distinction matters: resolved means numbered, dismissed means
        it never needed one."""
        issue = self.by_class("B")[0]
        self.call("POST", f"/api/worklist/{issue['id']}/resolve",
                  {"action": "dismiss", "reason": "internal, non-project work"})
        self.assertEqual(self.db.scalar(
            "SELECT status FROM job_code_issue WHERE id=?", (issue["id"],)),
            "dismissed")

    def test_unknown_action_is_refused(self):
        issue = self.by_class("B")[0]
        status, body = self.call(
            "POST", f"/api/worklist/{issue['id']}/resolve", {"action": "sort it out"})
        self.assertEqual(status, 400)
        self.assertIn("action", body["detail"])


class TestClearingTheWorklist(Case):
    def test_the_whole_worklist_can_be_driven_to_zero(self):
        """STP-5's gate. Working through every real issue, in order."""
        guard = 0
        while True:
            issues = self.issues()
            if not issues or guard > 30:
                break
            guard += 1
            issue = issues[0]
            body = {"action": "issue"} if issue["class"] == "B" else {
                "action": "keep", "reason": "one PO covers both work types"}
            status, _ = self.call(
                "POST", f"/api/worklist/{issue['id']}/resolve", body)
            self.assertEqual(status, 200, issue)
        self.assertEqual(self.call("GET", "/api/worklist")[1]["open"], 0)
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM project WHERE needs_resolution=1"), 0)

    def test_clearing_the_worklist_does_not_move_the_money(self):
        """Resolving a code changes identity, never value. If orders in hand
        moved, something else was touched."""
        before = self.db.scalar(
            "SELECT SUM(purchase_order_cents - invoiced_prior_cents) FROM project")
        for issue in self.issues():
            body = {"action": "issue"} if issue["class"] == "B" else {
                "action": "keep", "reason": "deliberate"}
            self.call("POST", f"/api/worklist/{issue['id']}/resolve", body)
        self.assertEqual(self.db.scalar(
            "SELECT SUM(purchase_order_cents - invoiced_prior_cents) FROM project"),
            before)


class TestPermissions(Case):
    roles = ("viewer",)

    def test_a_viewer_can_see_the_worklist(self):
        self.assertEqual(self.call("GET", "/api/worklist")[0], 200)

    def test_a_viewer_cannot_resolve(self):
        issue = self.issues()[0]
        self.assertEqual(self.call(
            "POST", f"/api/worklist/{issue['id']}/resolve",
            {"action": "issue"})[0], 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
