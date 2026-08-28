"""Access — granting and revoking roles over HTTP.

Until this existed, granting a role meant running Python against the
database. Fine for one person, impossible for anyone else.

The rule that matters: NO ROLE IMPLIES ANOTHER. An admin who is not also a
viewer cannot read the register, which has caught us twice and is
deliberate — the alternative is a hierarchy where granting one thing
quietly grants three.
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
    roles = ("viewer", "admin")

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
        self.me = auth.sign_in(self.db, {"sub": "s1", "email": "r@x", "name": "R"})
        for role in self.roles:
            self.db.grant_role(self.me["id"], 1, role, self.me["id"])
        self.other = auth.sign_in(
            self.db, {"sub": "s2", "email": "j@x", "name": "Justin"})

    def tearDown(self):
        self.sched.stop()
        self.server.shutdown()
        self.server.server_close()
        self.t.join(timeout=5)
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def call(self, method, path, body=None, as_user=None):
        who = as_user or self.me
        token = auth.mint_session(self.key, who["id"], who["token_version"])
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

    def roles_of(self, user_id):
        return sorted(r["role"] for r in self.db.roles_for(user_id))


class TestListing(Case):
    def test_it_shows_every_user_and_their_grants(self):
        status, body = self.call("GET", "/api/users")
        self.assertEqual(status, 200)
        names = sorted(u["display_name"] for u in body["users"])
        self.assertEqual(names, ["Justin", "R"])

    def test_a_user_with_no_roles_still_appears(self):
        """Otherwise the person who just signed in and got a 403 is
        invisible to the admin who needs to grant them something."""
        _s, body = self.call("GET", "/api/users")
        justin = [u for u in body["users"] if u["display_name"] == "Justin"][0]
        self.assertEqual(justin["roles"], [])

    def test_a_non_admin_is_refused(self):
        self.db.revoke_role(self.me["id"], 1, "admin", self.me["id"])
        self.assertEqual(self.call("GET", "/api/users")[0], 403)


class TestEntitiesInUse(Case):
    """The schema is multi-entity from migration 001, but the interface
    stays single-entity until a second one has something in it. Three rows
    per person for one real decision is three chances to click the wrong
    one."""

    def test_only_entities_with_something_in_them_appear(self):
        _s, body = self.call("GET", "/api/users")
        self.assertEqual([e["id"] for e in body["entities"]], [1])

    def test_a_second_entity_appears_once_it_has_a_project(self):
        with self.db._tx() as c:
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,
                             created_ts) VALUES (2,'Elsewhere','JN-9,9','Active',0)""")
        _s, body = self.call("GET", "/api/users")
        self.assertEqual(sorted(e["id"] for e in body["entities"]), [1, 2])

    def test_or_once_someone_has_a_role_on_it(self):
        self.db.grant_role(self.other["id"], 3, "viewer", self.me["id"])
        _s, body = self.call("GET", "/api/users")
        self.assertIn(3, [e["id"] for e in body["entities"]])


class TestGranting(Case):
    def test_granting_takes_effect_without_signing_in_again(self):
        """Roles resolve per request. Requiring a fresh sign-in would mean
        telling someone to log out and back in, which they will not do."""
        self.assertEqual(self.call("GET", "/api/projects",
                                   as_user=self.other)[0], 403)
        self.call("POST", f"/api/users/{self.other['id']}/roles",
                  {"entity_id": 1, "role": "viewer"})
        self.assertEqual(self.call("GET", "/api/projects",
                                   as_user=self.other)[0], 200)

    def test_no_role_implies_another(self):
        """An admin who is not a viewer cannot read the register."""
        self.call("POST", f"/api/users/{self.other['id']}/roles",
                  {"entity_id": 1, "role": "admin"})
        self.assertEqual(self.call("GET", "/api/projects",
                                   as_user=self.other)[0], 403)

    def test_an_unknown_role_is_refused(self):
        status, body = self.call("POST", f"/api/users/{self.other['id']}/roles",
                                 {"entity_id": 1, "role": "superuser"})
        self.assertEqual(status, 400)
        self.assertIn("role", body["detail"])

    def test_granting_twice_is_harmless(self):
        for _ in range(2):
            self.call("POST", f"/api/users/{self.other['id']}/roles",
                      {"entity_id": 1, "role": "viewer"})
        self.assertEqual(self.roles_of(self.other["id"]), ["viewer"])

    def test_it_is_audited(self):
        self.call("POST", f"/api/users/{self.other['id']}/roles",
                  {"entity_id": 1, "role": "operations"})
        self.assertIsNotNone(self.db.query_one(
            "SELECT id FROM audit_log WHERE action = 'role_grant'"))


class TestRevoking(Case):
    def test_revoking_takes_effect_immediately(self):
        self.call("POST", f"/api/users/{self.other['id']}/roles",
                  {"entity_id": 1, "role": "viewer"})
        self.assertEqual(self.call("GET", "/api/projects",
                                   as_user=self.other)[0], 200)
        self.call("DELETE", f"/api/users/{self.other['id']}/roles",
                  {"entity_id": 1, "role": "viewer"})
        self.assertEqual(self.call("GET", "/api/projects",
                                   as_user=self.other)[0], 403)

    def test_the_last_admin_cannot_be_removed(self):
        """Including yourself. A system nobody can administer needs a
        database client to recover, which is what this screen exists to
        end."""
        status, body = self.call("DELETE", f"/api/users/{self.me['id']}/roles",
                                 {"entity_id": 1, "role": "admin"})
        self.assertEqual(status, 409)
        self.assertIn("last admin", body["error"])
        self.assertIn("admin", self.roles_of(self.me["id"]))

    def test_it_can_be_removed_once_another_admin_exists(self):
        self.call("POST", f"/api/users/{self.other['id']}/roles",
                  {"entity_id": 1, "role": "admin"})
        self.assertEqual(self.call(
            "DELETE", f"/api/users/{self.me['id']}/roles",
            {"entity_id": 1, "role": "admin"})[0], 200)

    def test_an_inactive_admin_does_not_count(self):
        """Switching someone off and then removing the only other admin
        would leave nobody able to switch them back on."""
        self.call("POST", f"/api/users/{self.other['id']}/roles",
                  {"entity_id": 1, "role": "admin"})
        self.call("PATCH", f"/api/users/{self.other['id']}",
                  {"is_active": False})
        status, _b = self.call("DELETE", f"/api/users/{self.me['id']}/roles",
                               {"entity_id": 1, "role": "admin"})
        self.assertEqual(status, 409)


class TestSwitchingOff(Case):
    def test_every_session_stops_working_at_once(self):
        """Not when the cookie happens to expire. The token version moves,
        so the one they are holding stops validating on the next request."""
        self.call("POST", f"/api/users/{self.other['id']}/roles",
                  {"entity_id": 1, "role": "viewer"})
        token = auth.mint_session(self.key, self.other["id"],
                                  self.other["token_version"])
        self.call("PATCH", f"/api/users/{self.other['id']}",
                  {"is_active": False})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/projects",
                  headers={"Sec-Fetch-Site": "same-origin",
                           "Cookie": f"{auth.COOKIE_NAME}={token}"})
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.status, 401)

    def test_you_cannot_switch_off_your_own_account(self):
        status, body = self.call("PATCH", f"/api/users/{self.me['id']}",
                                 {"is_active": False})
        self.assertEqual(status, 409)
        self.assertIn("your own", body["error"])

    def test_reactivating_works(self):
        self.call("POST", f"/api/users/{self.other['id']}/roles",
                  {"entity_id": 1, "role": "viewer"})
        self.call("PATCH", f"/api/users/{self.other['id']}",
                  {"is_active": False})
        self.call("PATCH", f"/api/users/{self.other['id']}",
                  {"is_active": True})
        self.assertEqual(self.call("GET", "/api/projects",
                                   as_user=self.db.query_one(
                                       "SELECT id, token_version FROM users "
                                       "WHERE id = ?", (self.other["id"],)))[0],
                         200)


class TestPermissions(Case):
    roles = ("viewer",)

    def test_a_viewer_cannot_grant(self):
        self.assertEqual(self.call("POST", f"/api/users/{self.other['id']}/roles",
                                   {"entity_id": 1, "role": "admin"})[0], 403)

    def test_a_viewer_cannot_list(self):
        self.assertEqual(self.call("GET", "/api/users")[0], 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
