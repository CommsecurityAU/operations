"""ops.modules.projects -- CRUD, validation, and what it refuses.

Most of the value is in the refusals. A create form that accepts anything is
how a register fills with unowned, untyped projects that nobody can bill.
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
from ops.modules import projects as mod  # noqa: E402
from ops.secrets import LocalProvider  # noqa: E402


class Case(unittest.TestCase):
    # Explicit, never inherited: no role implies another (§9), so a test
    # that needs to create AND delete has to hold both.
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
        self.user = auth.sign_in(self.db, {
            "sub": "s1", "email": "r@commsecurity.com.au", "name": "Richard"})
        for role in self.roles:
            self.db.grant_role(self.user["id"], 1, role, self.user["id"])
        with self.db._tx() as c:
            c.execute("INSERT INTO client (entity_id, name) VALUES (1,'Hines')")
        self.client_id = self.db.scalar("SELECT id FROM client WHERE name='Hines'")
        self.type_id = self.db.scalar(
            "SELECT id FROM project_type WHERE code='ICN'")

    def tearDown(self):
        self.sched.stop()
        self.server.shutdown()
        self.server.server_close()
        self.t.join(timeout=5)
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def call(self, method, path, body=None, user=None):
        user = user or self.user
        token = auth.mint_session(self.key, user["id"], user["token_version"])
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json",
                   "Sec-Fetch-Site": "same-origin",
                   "Cookie": f"{auth.COOKIE_NAME}={token}"}
        c.request(method, path,
                  body=None if body is None else json.dumps(body).encode(),
                  headers=headers)
        r = c.getresponse()
        raw = r.read()
        c.close()
        try:
            return r.status, json.loads(raw) if raw else None
        except ValueError:
            return r.status, raw

    def valid(self, **over):
        payload = {"name": "New Site - ICN", "client_id": self.client_id,
                   "type_id": self.type_id, "status": "Active",
                   "project_lead": "Joshua Koch",
                   "purchase_order_cents": 100000}
        payload.update(over)
        return payload


class TestCreate(Case):
    def test_creates_and_allocates_a_job_number(self):
        status, body = self.call("POST", "/api/projects", self.valid())
        self.assertEqual(status, 201)
        self.assertTrue(body["job_code"].startswith("JN-"))
        self.assertEqual(body["name"], "New Site - ICN")

    def test_job_numbers_are_sequential_and_not_reused(self):
        a = self.call("POST", "/api/projects", self.valid(name="A"))[1]
        b = self.call("POST", "/api/projects", self.valid(name="B"))[1]
        self.assertEqual(int(b["job_code"][3:]), int(a["job_code"][3:]) + 1)

    def test_a_rejected_create_does_not_burn_a_job_number(self):
        """The number is allocated inside the insert's transaction. If it
        were handed out when the form opened, every abandoned form would
        leave a gap in the series."""
        before = self.db.scalar("SELECT next_value FROM job_number_sequence")
        self.assertEqual(self.call("POST", "/api/projects",
                                   self.valid(name=""))[0], 400)
        self.assertEqual(
            self.db.scalar("SELECT next_value FROM job_number_sequence"), before)

    def test_create_is_audited(self):
        self.call("POST", "/api/projects", self.valid())
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM audit_log WHERE action='project_create'"), 1)

    def test_defaults_to_the_only_entity_the_user_can_see(self):
        _s, body = self.call("POST", "/api/projects", self.valid())
        self.assertEqual(
            self.db.scalar("SELECT entity_id FROM project WHERE id=?",
                           (body["id"],)), 1)

    def test_cannot_create_on_an_entity_you_cannot_see(self):
        status, _ = self.call("POST", "/api/projects",
                              self.valid(entity_id=2))
        self.assertEqual(status, 403)


class TestValidation(Case):
    def bad(self, **over):
        status, body = self.call("POST", "/api/projects", self.valid(**over))
        self.assertEqual(status, 400, body)
        return body["detail"]

    def test_name_is_required(self):
        self.assertIn("name", self.bad(name="   "))

    def test_lead_is_required(self):
        """STP-1: a project cannot exist without a lead. An unowned project
        is how work goes unclaimed and unbilled."""
        self.assertIn("project_lead", self.bad(project_lead=""))

    def test_client_and_type_are_required(self):
        """With no id AND no name the error is on client_name: "pick one or
        type a new name" is the actionable message, not "bad id"."""
        self.assertIn("client_name", self.bad(client_id=None))
        self.assertIn("type_id", self.bad(type_id=None))

    def test_status_must_be_in_the_taxonomy(self):
        self.assertIn("status", self.bad(status="Sort of active"))

    def test_duplicate_name_on_the_same_entity_is_refused(self):
        self.call("POST", "/api/projects", self.valid())
        self.assertIn("name", self.bad())

    def test_client_must_belong_to_the_entity(self):
        with self.db._tx() as c:
            c.execute("INSERT INTO client (entity_id, name) VALUES (2,'Other')")
        other = self.db.scalar("SELECT id FROM client WHERE name='Other'")
        self.assertIn("client_id", self.bad(client_id=other))

    def test_negative_money_is_refused(self):
        self.assertIn("purchase_order_cents",
                      self.bad(purchase_order_cents=-1))

    def test_absurd_money_is_refused(self):
        """A hundred million dollar project is a typo, not a project."""
        self.assertIn("purchase_order_cents",
                      self.bad(purchase_order_cents=999_999_999_00))

    def test_cannot_be_invoiced_more_than_the_contract(self):
        """The schema CHECK would refuse this, but an IntegrityError reaches
        the user as 'internal error'. Say what is actually wrong."""
        detail = self.bad(purchase_order_cents=1000,
                          invoiced_prior_cents=2000)
        self.assertIn("invoiced_prior_cents", detail)
        self.assertIn("cannot exceed", detail["invoiced_prior_cents"])

    def test_every_problem_is_reported_at_once(self):
        """One error at a time turns a single correction into four round
        trips."""
        detail = self.bad(name="", project_lead="", status="nope",
                          client_id=None)
        self.assertGreaterEqual(len(detail), 4)


class TestClientEntry(Case):
    """A client may be picked from the list or typed. Typing is necessary --
    new clients appear -- and is also how a register acquires three spellings
    of one company."""

    def test_a_new_client_is_created_by_name(self):
        status, body = self.call("POST", "/api/projects",
                                 self.valid(client_id=None,
                                            client_name="Kane Constructions"))
        self.assertEqual(status, 201)
        self.assertTrue(body["client_resolved"]["created"])
        self.assertEqual(body["client_resolved"]["name"], "Kane Constructions")
        self.assertEqual(
            self.db.scalar("SELECT name FROM client WHERE id=?",
                           (body["client_id"],)), "Kane Constructions")

    def test_an_exact_existing_name_is_reused(self):
        _s, body = self.call("POST", "/api/projects",
                             self.valid(client_id=None, client_name="Hines"))
        self.assertFalse(body["client_resolved"]["created"])
        self.assertEqual(body["client_id"], self.client_id)

    def test_case_and_punctuation_differences_reuse_the_existing_client(self):
        """'M Squared', 'MSquared' and 'm-squared' are one company. Three
        client rows would split the by-client rollup, and unpicking that
        after invoices reference all three is expensive."""
        with self.db._tx() as c:
            c.execute("INSERT INTO client (entity_id, name) VALUES (1,'MSquared')")
        existing = self.db.scalar("SELECT id FROM client WHERE name='MSquared'")
        for typed in ("M Squared", "m-squared", "  msquared  ", "M.Squared"):
            _s, body = self.call("POST", "/api/projects",
                                 self.valid(name=f"P {typed}", client_id=None,
                                            client_name=typed))
            self.assertEqual(body["client_id"], existing, typed)
            self.assertFalse(body["client_resolved"]["created"], typed)

    def test_a_reused_spelling_is_reported_not_silently_applied(self):
        """The user typed 'M Squared' and got 'MSquared'. Say so, or they
        find out from a report months later."""
        with self.db._tx() as c:
            c.execute("INSERT INTO client (entity_id, name) VALUES (1,'MSquared')")
        _s, body = self.call("POST", "/api/projects",
                             self.valid(client_id=None, client_name="M Squared"))
        resolved = body["client_resolved"]
        self.assertTrue(resolved["reused_existing_spelling"])
        self.assertEqual(resolved["typed"], "M Squared")
        self.assertEqual(resolved["name"], "MSquared")

    def test_no_duplicate_client_rows_are_created(self):
        for typed in ("Kane", "kane", "KANE", "K-a-n-e"):
            self.call("POST", "/api/projects",
                      self.valid(name=f"P {typed}", client_id=None,
                                 client_name=typed))
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM client WHERE entity_id=1 AND name LIKE 'K%'"), 1)

    def test_client_creation_is_audited(self):
        self.call("POST", "/api/projects",
                  self.valid(client_id=None, client_name="Brand New Pty Ltd"))
        row = self.db.query_one(
            "SELECT detail FROM audit_log WHERE action='client_create'")
        self.assertEqual(row["detail"], "Brand New Pty Ltd")

    def test_a_client_on_another_entity_is_not_reused(self):
        """Clients are entity-scoped: the same company dealing with two of
        our companies is two client records, because the invoices are."""
        with self.db._tx() as c:
            c.execute("INSERT INTO client (entity_id, name) VALUES (2,'Elsewhere')")
        _s, body = self.call("POST", "/api/projects",
                             self.valid(client_id=None, client_name="Elsewhere"))
        self.assertTrue(body["client_resolved"]["created"])
        self.assertEqual(self.db.scalar(
            "SELECT entity_id FROM client WHERE id=?", (body["client_id"],)), 1)

    def test_an_id_wins_over_a_name_when_both_are_sent(self):
        _s, body = self.call("POST", "/api/projects",
                             self.valid(client_id=self.client_id,
                                        client_name="Ignored"))
        self.assertEqual(body["client_id"], self.client_id)
        self.assertIsNone(self.db.query_one(
            "SELECT id FROM client WHERE name='Ignored'"))

    def test_blank_client_name_is_refused(self):
        status, body = self.call("POST", "/api/projects",
                                 self.valid(client_id=None, client_name="   "))
        self.assertEqual(status, 400)
        self.assertIn("client_name", body["detail"])

    def test_a_project_client_can_be_changed_by_typing_a_new_one(self):
        p = self.call("POST", "/api/projects", self.valid())[1]
        status, body = self.call("PATCH", f"/api/projects/{p['id']}",
                                 {"client_name": "Freshly Typed Pty Ltd"})
        self.assertEqual(status, 200)
        self.assertEqual(body["changed"], ["client_id"])
        self.assertTrue(body["client_resolved"]["created"])


class TestUpdate(Case):
    def make(self):
        return self.call("POST", "/api/projects", self.valid())[1]

    def test_patches_only_the_fields_supplied(self):
        p = self.make()
        status, body = self.call("PATCH", f"/api/projects/{p['id']}",
                                 {"status": "DLP"})
        self.assertEqual(status, 200)
        self.assertEqual(body["changed"], ["status"])
        self.assertEqual(body["project"]["name"], p["name"])
        self.assertEqual(body["project"]["project_lead"], p["project_lead"])

    def test_unchanged_values_are_not_recorded_as_changes(self):
        p = self.make()
        _s, body = self.call("PATCH", f"/api/projects/{p['id']}",
                             {"status": p["status"]})
        self.assertEqual(body["changed"], [])

    def test_every_change_is_audited_with_old_and_new(self):
        p = self.make()
        self.call("PATCH", f"/api/projects/{p['id']}",
                  {"project_lead": "Finau Taliauli"})
        row = self.db.query_one(
            "SELECT detail FROM audit_log WHERE action='project_update'")
        self.assertIn("Joshua Koch", row["detail"])
        self.assertIn("Finau Taliauli", row["detail"])

    def test_job_code_cannot_be_edited(self):
        """Reassigning a job number is a migration, not an edit -- it breaks
        every downstream reference including Xero."""
        p = self.make()
        status, body = self.call("PATCH", f"/api/projects/{p['id']}",
                                 {"job_code": "JN-9999"})
        self.assertEqual(status, 400)
        self.assertIn("job_code", body["detail"])

    def test_entity_cannot_be_edited(self):
        p = self.make()
        self.assertEqual(self.call("PATCH", f"/api/projects/{p['id']}",
                                   {"entity_id": 2})[0], 400)

    def test_unknown_field_is_refused_not_ignored(self):
        """Silently ignoring a typo'd key looks like a successful save that
        changed nothing."""
        p = self.make()
        status, body = self.call("PATCH", f"/api/projects/{p['id']}",
                                 {"projct_lead": "typo"})
        self.assertEqual(status, 400)
        self.assertIn("projct_lead", body["detail"])

    def test_validation_applies_to_updates_too(self):
        p = self.make()
        self.assertEqual(self.call("PATCH", f"/api/projects/{p['id']}",
                                   {"project_lead": ""})[0], 400)

    def test_patch_of_a_missing_project_is_404(self):
        self.assertEqual(self.call("PATCH", "/api/projects/9999",
                                   {"status": "DLP"})[0], 404)

    def test_concurrent_patches_of_different_fields_both_survive(self):
        """The common collision case. Field-level patching means two people
        editing different columns do not fight at all."""
        p = self.make()
        self.call("PATCH", f"/api/projects/{p['id']}", {"status": "DLP"})
        self.call("PATCH", f"/api/projects/{p['id']}",
                  {"project_lead": "Finau Taliauli"})
        row = self.db.query_one("SELECT * FROM project WHERE id=?", (p["id"],))
        self.assertEqual(row["status"], "DLP")
        self.assertEqual(row["project_lead"], "Finau Taliauli")


class TestDelete(Case):
    roles = ("viewer", "operations", "admin")

    def make(self, **over):
        return self.call("POST", "/api/projects", self.valid(**over))[1]

    def test_a_stub_with_no_money_can_be_deleted(self):
        p = self.make(purchase_order_cents=0)
        self.assertEqual(self.call("DELETE", f"/api/projects/{p['id']}")[0], 204)
        self.assertIsNone(self.db.query_one(
            "SELECT id FROM project WHERE id=?", (p["id"],)))

    def test_a_project_carrying_money_is_refused(self):
        """Deleting one destroys history that a signed-off total depends on.
        The workbook's own answer is status, so say that."""
        p = self.make(purchase_order_cents=500000)
        status, body = self.call("DELETE", f"/api/projects/{p['id']}")
        self.assertEqual(status, 409)
        self.assertIn("status to Complete", body["error"])
        self.assertIsNotNone(self.db.query_one(
            "SELECT id FROM project WHERE id=?", (p["id"],)))

    def test_delete_is_audited(self):
        p = self.make(purchase_order_cents=0)
        self.call("DELETE", f"/api/projects/{p['id']}")
        row = self.db.query_one(
            "SELECT detail FROM audit_log WHERE action='project_delete'")
        self.assertIn(p["job_code"], row["detail"])


class TestPermissions(Case):
    roles = ("viewer",)      # viewer only: no operations, no admin

    def test_a_viewer_can_read(self):
        self.assertEqual(self.call("GET", "/api/projects")[0], 200)

    def test_a_viewer_cannot_create(self):
        self.assertEqual(self.call("POST", "/api/projects", self.valid())[0], 403)

    def test_a_viewer_cannot_patch(self):
        self.assertEqual(self.call("PATCH", "/api/projects/1",
                                   {"status": "DLP"})[0], 403)

    def test_a_viewer_cannot_delete(self):
        self.assertEqual(self.call("DELETE", "/api/projects/1")[0], 403)

    def test_operations_cannot_delete(self):
        """No role implies another: delete is admin-only, and an operations
        user does not inherit it."""
        self.db.grant_role(self.user["id"], 1, "operations", self.user["id"])
        p = self.call("POST", "/api/projects",
                      self.valid(purchase_order_cents=0))[1]
        self.assertEqual(self.call("DELETE", f"/api/projects/{p['id']}")[0], 403)


class TestReference(Case):
    def test_reference_returns_everything_a_form_needs(self):
        status, body = self.call("GET", "/api/reference")
        self.assertEqual(status, 200)
        self.assertEqual([c["name"] for c in body["clients"]], ["Hines"])
        self.assertEqual(len(body["types"]), 9)
        self.assertEqual(body["statuses"], list(mod.STATUSES))

    def test_reference_is_scoped_to_granted_entities(self):
        with self.db._tx() as c:
            c.execute("INSERT INTO client (entity_id, name) VALUES (2,'Hidden')")
        _s, body = self.call("GET", "/api/reference")
        self.assertNotIn("Hidden", [c["name"] for c in body["clients"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
