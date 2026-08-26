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

    _code = [7000]

    def valid(self, **over):
        """Supplies a distinct job code by default.

        Creation no longer allocates (ADR-28), so a test that needs a real
        code has to say so -- which is the point: the platform records the
        number it was given rather than inventing one.
        """
        self._code[0] += 1
        payload = {"name": "New Site - ICN", "client_id": self.client_id,
                   "type_id": self.type_id, "status": "Active",
                   "project_lead": "Joshua Koch",
                   "purchase_order_cents": 100000,
                   "job_code_mode": "existing",
                   "job_code": f"JN-{self._code[0]}"}
        payload.update(over)
        return payload


class TestCreate(Case):
    def test_creates_with_the_code_it_was_given(self):
        status, body = self.call("POST", "/api/projects", self.valid())
        self.assertEqual(status, 201)
        self.assertTrue(body["job_code"].startswith("JN-"))
        self.assertEqual(body["name"], "New Site - ICN")

    def test_creation_NEVER_allocates_a_number(self):
        """ADR-28: iTrade still issues. A number allocated here could
        collide with one issued there tomorrow, and the collision would not
        surface until both reached Xero."""
        before = self.db.scalar("SELECT next_value FROM job_number_sequence")
        self.call("POST", "/api/projects", self.valid(name="A"))
        self.call("POST", "/api/projects", self.valid(name="B",
                                                      job_code_mode="defer"))
        self.assertEqual(
            self.db.scalar("SELECT next_value FROM job_number_sequence"), before)

    def test_the_default_is_to_defer_not_to_allocate(self):
        """A default that allocates is how two projects ended up with numbers
        nobody wanted."""
        payload = self.valid()
        del payload["job_code_mode"], payload["job_code"]
        _s, body = self.call("POST", "/api/projects", payload)
        self.assertEqual(body["job_code"], "TBA")
        self.assertEqual(body["needs_resolution"], 1)

    def test_asking_to_allocate_is_refused(self):
        status, body = self.call("POST", "/api/projects",
                                 self.valid(job_code_mode="issue"))
        self.assertEqual(status, 400)
        self.assertIn("job_code_mode", body["detail"])

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


class TestJobCodeOnCreate(Case):
    """Always allocating was wrong. Two projects created in the UI already
    had codes of their own, so the platform issued numbers nobody wanted and
    burnt them out of the sequence permanently."""

    def test_an_existing_code_can_be_supplied(self):
        _s, body = self.call("POST", "/api/projects",
                             self.valid(job_code_mode="existing",
                                        job_code="JN-6948"))
        self.assertEqual(body["job_code"], "JN-6948")

    def test_supplying_a_code_does_not_burn_a_number(self):
        before = self.db.scalar("SELECT next_value FROM job_number_sequence")
        self.call("POST", "/api/projects",
                  self.valid(job_code_mode="existing", job_code="JN-6948"))
        self.assertEqual(
            self.db.scalar("SELECT next_value FROM job_number_sequence"), before)

    def test_a_supplied_code_cannot_collide(self):
        """This is the class C defect trying to come back through the front
        door -- and the message names the other project."""
        self.call("POST", "/api/projects",
                  self.valid(name="First", job_code_mode="existing",
                             job_code="JN-6948"))
        status, body = self.call("POST", "/api/projects",
                                 self.valid(name="Second",
                                            job_code_mode="existing",
                                            job_code="JN-6948"))
        self.assertEqual(status, 400)
        self.assertIn("First", body["detail"]["job_code"])

    def test_deferring_creates_a_worklist_entry(self):
        """A project with no number yet is a decision deferred, not an
        error. The worklist entry is what stops it being a blank cell nobody
        revisits."""
        _s, body = self.call("POST", "/api/projects",
                             self.valid(job_code_mode="defer"))
        self.assertEqual(body["job_code"], "TBA")
        self.assertEqual(body["needs_resolution"], 1)
        self.assertIsNotNone(self.db.query_one(
            "SELECT id FROM job_code_issue WHERE project_id = ? AND status='open'",
            (body["id"],)))

    def test_deferring_does_not_burn_a_number(self):
        before = self.db.scalar("SELECT next_value FROM job_number_sequence")
        self.call("POST", "/api/projects", self.valid(job_code_mode="defer"))
        self.assertEqual(
            self.db.scalar("SELECT next_value FROM job_number_sequence"), before)

    def test_the_sequence_is_still_there_for_when_issuance_moves_here(self):
        """ADR-28 is "not yet", not "never". The worklist can still issue,
        as a deliberate act by someone who knows the number is ours to give."""
        self.assertGreater(
            self.db.scalar("SELECT next_value FROM job_number_sequence"), 0)

    def test_a_malformed_supplied_code_is_refused(self):
        for bad in ("", "a b c", "JN-<script>", "x" * 60):
            status, _b = self.call("POST", "/api/projects",
                                   self.valid(name=f"P {bad[:5]}",
                                              job_code_mode="existing",
                                              job_code=bad))
            self.assertEqual(status, 400, bad)


class TestJobCodeCorrection(Case):
    roles = ("viewer", "operations", "admin")

    def make(self, **over):
        return self.call("POST", "/api/projects", self.valid(**over))[1]

    def test_a_wrongly_issued_code_can_be_corrected(self):
        p = self.make(purchase_order_cents=0)
        status, body = self.call("POST", f"/api/projects/{p['id']}/job-code",
                                 {"job_code": "JN-6948",
                                  "reason": "iTrade had already issued this"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["job_code"], "JN-6948")
        self.assertEqual(self.db.scalar(
            "SELECT job_code FROM project WHERE id=?", (p["id"],)), "JN-6948")

    def test_the_old_code_survives_as_an_alias(self):
        """Traceability is the whole reason job_code is normally immutable,
        so a correction must not simply erase the old value."""
        p = self.make(purchase_order_cents=0)
        self.call("POST", f"/api/projects/{p['id']}/job-code",
                  {"job_code": "JN-6948", "reason": "wrong number issued"})
        self.assertIsNotNone(self.db.query_one(
            "SELECT id FROM job_code_alias WHERE legacy_code = ? AND project_id = ?",
            (p["job_code"], p["id"])))

    def test_a_reason_is_mandatory(self):
        p = self.make(purchase_order_cents=0)
        status, body = self.call("POST", f"/api/projects/{p['id']}/job-code",
                                 {"job_code": "JN-6948"})
        self.assertEqual(status, 400)
        self.assertIn("reason", body["detail"])

    def test_it_cannot_collide_with_another_project(self):
        a = self.make(name="A", purchase_order_cents=0)
        b = self.make(name="B", purchase_order_cents=0)
        status, body = self.call("POST", f"/api/projects/{b['id']}/job-code",
                                 {"job_code": a["job_code"], "reason": "x"})
        self.assertEqual(status, 409)
        self.assertIn("already used", body["error"])

    def test_refused_once_the_project_has_invoicing_history(self):
        """Same guard as delete, for the same reason: correcting a code with
        invoices against it orphans those references."""
        p = self.make(purchase_order_cents=100000,
                      invoiced_prior_cents=50000)
        status, body = self.call("POST", f"/api/projects/{p['id']}/job-code",
                                 {"job_code": "JN-7777", "reason": "x"})
        self.assertEqual(status, 409)
        self.assertIn("orphan", body["error"])

    def test_correcting_to_a_placeholder_works_even_when_others_hold_it(self):
        """A placeholder is non-unique by definition -- several projects sit
        on TBA at once, which is what it means. Enforcing uniqueness on it
        blocked the one honest way to say "this number was issued in error
        and there is no correct one yet"."""
        a = self.make(name="Already TBA", job_code_mode="defer",
                      purchase_order_cents=0)
        self.assertEqual(a["job_code"], "TBA")
        b = self.make(name="Wrongly Numbered", purchase_order_cents=0)
        status, body = self.call("POST", f"/api/projects/{b['id']}/job-code",
                                 {"job_code": "TBA",
                                  "reason": "number issued in error; none assigned yet"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM project WHERE job_code='TBA'"), 2)

    def test_real_codes_are_still_unique(self):
        """The exemption is for placeholders only."""
        a = self.make(name="A", purchase_order_cents=0)
        b = self.make(name="B", purchase_order_cents=0)
        status, _b = self.call("POST", f"/api/projects/{b['id']}/job-code",
                               {"job_code": a["job_code"], "reason": "x"})
        self.assertEqual(status, 409)

    def test_correcting_to_a_placeholder_puts_it_back_on_the_worklist(self):
        p = self.make(purchase_order_cents=0)
        self.call("POST", f"/api/projects/{p['id']}/job-code",
                  {"job_code": "TBA", "reason": "number not assigned yet"})
        self.assertIsNotNone(self.db.query_one(
            "SELECT id FROM job_code_issue WHERE project_id=? AND status='open'",
            (p["id"],)))
        self.assertEqual(self.db.scalar(
            "SELECT needs_resolution FROM project WHERE id=?", (p["id"],)), 1)

    def test_the_change_is_audited_with_the_reason(self):
        p = self.make(purchase_order_cents=0)
        self.call("POST", f"/api/projects/{p['id']}/job-code",
                  {"job_code": "JN-6948", "reason": "iTrade already issued it"})
        row = self.db.query_one(
            "SELECT detail FROM audit_log WHERE action='job_code_change'")
        self.assertIn("iTrade already issued it", row["detail"])
        self.assertIn(p["job_code"], row["detail"])

    def test_job_code_is_still_immutable_through_the_ordinary_edit_path(self):
        p = self.make(purchase_order_cents=0)
        status, _b = self.call("PATCH", f"/api/projects/{p['id']}",
                               {"job_code": "JN-6948"})
        self.assertEqual(status, 400)


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


class TestRetentionOnTheRegister(Case):
    def make(self, **over):
        return self.call("POST", "/api/projects", self.valid(**over))[1]

    def test_each_project_reports_what_is_held(self):
        """So the card can be summed over whatever the filters leave."""
        p = self.make()
        with self.db._tx() as c:
            c.execute("""UPDATE customer_po SET retention_applies=1,
                             retention_rate_bp=1000, retention_cap_bp=500
                         WHERE project_id=?""", (p["id"],))
            c.execute("""INSERT INTO claim_line (entity_id, project_id,
                             customer_po_id, status, amount_cents,
                             retention_cents, created_ts)
                         SELECT 1, ?, id, 'invoiced', 5000000, 500000, 0
                         FROM customer_po WHERE project_id = ?""",
                      (p["id"], p["id"]))
        _s, body = self.call("GET", "/api/projects")
        row = [r for r in body["projects"] if r["id"] == p["id"]][0]
        self.assertEqual(row["retention_held_cents"], 500000)

    def test_a_project_without_retention_reports_zero_not_null(self):
        """A dashboard cell showing null is how #N/A got into the workbook."""
        p = self.make()
        _s, body = self.call("GET", "/api/projects")
        row = [r for r in body["projects"] if r["id"] == p["id"]][0]
        self.assertEqual(row["retention_held_cents"], 0)


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
