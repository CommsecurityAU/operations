"""Boot, backup, and the STP-0 exit criteria end to end.

The last class walks the actual acceptance path from §17: a user signs in,
gets viewer-on-zero-entities, sees nothing, an admin grants a role, and the
projects appear WITHOUT re-login.
"""

import http.client
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from ops import auth, backup  # noqa: E402
from ops.config import Config, from_env  # noqa: E402
from ops.db import Db  # noqa: E402
from ops.main import boot, build_router, make_auth_hook  # noqa: E402
from ops.secrets import LocalProvider  # noqa: E402

MIGRATIONS = os.path.join(ROOT, "ops", "migrations")


class TestConfig(unittest.TestCase):
    def test_defaults_to_a_secret_reference_not_a_value(self):
        cfg = from_env({})
        self.assertEqual(cfg.oidc_client_secret, "secret://OIDC_CLIENT_SECRET")

    def test_redacted_never_prints_a_value(self):
        cfg = Config(oidc_client_secret="GOCSPX-actual-secret")
        blob = json.dumps(cfg.redacted())
        self.assertNotIn("GOCSPX-actual-secret", blob)
        self.assertIn("not a reference", blob)

    def test_redacted_keeps_references_visible(self):
        cfg = Config(oidc_client_secret="secret://OIDC_CLIENT_SECRET")
        self.assertEqual(cfg.redacted()["oidc_client_secret"],
                         "secret://OIDC_CLIENT_SECRET")

    def test_str_is_safe_to_log(self):
        self.assertNotIn("GOCSPX", str(Config(oidc_client_secret="GOCSPX-x")))

    def test_port_follows_tls(self):
        self.assertEqual(Config(tls=True).effective_port, 8443)
        self.assertEqual(Config(tls=False).effective_port, 8080)
        self.assertEqual(Config(tls=False, port=9999).effective_port, 9999)

    def test_tls_off_only_for_explicit_falsey_values(self):
        self.assertTrue(from_env({}).tls)
        for off in ("0", "off", "false", "no"):
            self.assertFalse(from_env({"OPS_TLS": off}).tls, off)
        self.assertTrue(from_env({"OPS_TLS": "on"}).tls)


class BackupCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Db(os.path.join(self.dir, "ops.db"), MIGRATIONS)
        self.db.migrate()
        self.backups = os.path.join(self.dir, "backups")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=False)


class TestBackup(BackupCase):
    def test_snapshot_is_a_usable_database(self):
        path = backup.snapshot(self.db, self.backups)
        self.assertTrue(os.path.exists(path))
        copy = Db(path, MIGRATIONS)
        try:
            self.assertEqual(copy.scalar("SELECT COUNT(*) FROM period"), 144)
        finally:
            copy.close()

    def test_snapshot_does_not_block_writes(self):
        backup.snapshot(self.db, self.backups)
        with self.db._tx() as c:
            c.execute("INSERT INTO client (entity_id, name) VALUES (1,'after')")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM client"), 1)

    def test_snapshot_captures_state_at_the_time_it_ran(self):
        with self.db._tx() as c:
            c.execute("INSERT INTO client (entity_id, name) VALUES (1,'before')")
        path = backup.snapshot(self.db, self.backups)
        with self.db._tx() as c:
            c.execute("INSERT INTO client (entity_id, name) VALUES (1,'after')")
        copy = Db(path, MIGRATIONS)
        try:
            self.assertEqual(copy.scalar("SELECT COUNT(*) FROM client"), 1)
        finally:
            copy.close()

    def test_prune_keeps_the_newest(self):
        os.makedirs(self.backups, exist_ok=True)
        for i in range(6):
            open(os.path.join(self.backups, f"ops-2026010{i}T000000Z.db"), "w").close()
        removed = backup.prune(self.backups, keep=2)
        self.assertEqual(len(removed), 4)
        left = sorted(os.listdir(self.backups))
        self.assertEqual(left, ["ops-20260104T000000Z.db",
                                "ops-20260105T000000Z.db"])

    def test_prune_ignores_unrelated_files(self):
        os.makedirs(self.backups, exist_ok=True)
        open(os.path.join(self.backups, "README.txt"), "w").close()
        backup.prune(self.backups, keep=0)
        self.assertIn("README.txt", os.listdir(self.backups))

    def test_integrity_check(self):
        self.assertEqual(backup.integrity_check(self.db), "ok")

    def test_failure_is_surfaced_on_healthz_not_just_logged(self):
        """A backup silently failing for a fortnight is worse than no backup:
        it buys false confidence."""
        sched = backup.Scheduler(self.db, "/nonexistent/\0/bad")
        logging.getLogger("ops.backup").setLevel(logging.CRITICAL)
        self.assertIsNone(sched.run_once())
        self.assertIsNotNone(self.db.last_backup_error)
        self.assertTrue(any("backup" in w for w in self.db.health()["warnings"]))

    def test_success_clears_a_previous_error(self):
        self.db.last_backup_error = "stale"
        backup.Scheduler(self.db, self.backups).run_once()
        self.assertIsNone(self.db.last_backup_error)
        self.assertTrue(self.db.health()["ok"])

    def test_scheduler_thread_runs_and_stops(self):
        sched = backup.Scheduler(self.db, self.backups, interval_s=0.05).start()
        time.sleep(0.25)
        sched.stop()
        self.assertGreaterEqual(sched.runs, 1)
        self.assertFalse(sched._thread.is_alive())


class TestBootRefusals(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        os.environ.pop("OPS_SECRETS_PATH", None)

    def test_missing_secret_exits_nonzero_rather_than_starting(self):
        """Boot failure = failed health gate = automatic rollback, so a
        missing secret self-reports instead of running with a blank
        credential."""
        cfg = Config(data_dir=self.dir, tls=False, port=0,
                     oidc_client_id="cid", oidc_redirect_uri="https://x/cb")
        logging.getLogger("ops.main").setLevel(logging.CRITICAL)
        with self.assertRaises(SystemExit) as e:
            boot(cfg=cfg, env={"OPS_SECRETS_PATH":
                               os.path.join(self.dir, "store.json")},
                 serve=False)
        self.assertEqual(e.exception.code, 2)


class Stp0Case(unittest.TestCase):
    """Full stack over a real socket."""

    def setUp(self):
        logging.getLogger("ops.http").setLevel(logging.CRITICAL)
        logging.getLogger("ops.main").setLevel(logging.CRITICAL)
        logging.getLogger("ops.auth").setLevel(logging.CRITICAL)
        self.dir = tempfile.mkdtemp()
        secrets_path = os.path.join(self.dir, "secrets", "store.json")
        LocalProvider(secrets_path).set("OIDC_CLIENT_SECRET", "test-secret")
        cfg = Config(data_dir=self.dir, tls=False, port=0,
                     oidc_client_id="cid.apps.googleusercontent.com",
                     oidc_redirect_uri="http://127.0.0.1/auth/callback",
                     backup_interval_s=3600)
        self.db, self.server, self.sched = boot(
            cfg=cfg, env={"OPS_SECRETS_PATH": secrets_path}, serve=False)
        self.cfg = cfg
        self.key = auth.load_or_create_key(cfg.session_key_path)
        self.port = self.server.server_address[1]
        self.t = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
            daemon=True)
        self.t.start()

    def tearDown(self):
        self.sched.stop()
        self.server.shutdown()
        self.server.server_close()
        self.t.join(timeout=5)
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def request(self, method, path, cookie=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Sec-Fetch-Site": "same-origin"}
        if cookie:
            headers["Cookie"] = cookie
        c.request(method, path, body=body, headers=headers)
        r = c.getresponse()
        payload = r.read()
        c.close()
        try:
            return r.status, json.loads(payload)
        except ValueError:
            return r.status, payload

    def session_for(self, user):
        token = auth.mint_session(self.key, user["id"], user["token_version"])
        return f"{auth.COOKIE_NAME}={token}"


class TestStp0ExitCriteria(Stp0Case):
    def test_healthz_is_200_after_boot(self):
        status, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["schema"]["missing"], [])

    def test_login_redirects_to_google(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/login")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 302)
        location = r.getheader("Location")
        c.close()
        self.assertIn("accounts.google.com", location)
        self.assertIn("hd=commsecurity.com.au", location)

    def test_api_requires_authentication(self):
        status, _ = self.request("GET", "/api/projects")
        self.assertEqual(status, 401)

    def test_first_sign_in_sees_an_empty_project_list(self):
        """§17: user auto-provisioned as viewer on ZERO entities."""
        user = auth.sign_in(self.db, {
            "sub": "s1", "email": "r@commsecurity.com.au", "name": "Richard"})
        self.assertEqual(self.db.roles_for(user["id"]), [])
        status, _ = self.request("GET", "/api/projects",
                                 cookie=self.session_for(user))
        self.assertEqual(status, 403)   # viewer role not held at all

    def test_grant_makes_projects_appear_without_re_login(self):
        """§17, and the point of resolving roles per request."""
        user = auth.sign_in(self.db, {
            "sub": "s1", "email": "r@commsecurity.com.au", "name": "Richard"})
        cookie = self.session_for(user)
        with self.db._tx() as c:
            c.execute("""INSERT INTO project
                         (entity_id, name, job_code, status, created_ts)
                         VALUES (1,'Test Project','JN-1','Active',0)""")
        self.assertEqual(self.request("GET", "/api/projects", cookie)[0], 403)

        self.db.grant_role(user["id"], 1, "viewer", user["id"])

        status, body = self.request("GET", "/api/projects", cookie)  # SAME cookie
        self.assertEqual(status, 200)
        self.assertEqual([p["name"] for p in body["projects"]], ["Test Project"])

    def test_a_second_account_with_no_grant_still_sees_nothing(self):
        a = auth.sign_in(self.db, {"sub": "s1", "email": "a@x", "name": "A"})
        b = auth.sign_in(self.db, {"sub": "s2", "email": "b@x", "name": "B"})
        self.db.grant_role(a["id"], 1, "viewer", a["id"])
        self.assertEqual(self.request("GET", "/api/projects",
                                      self.session_for(a))[0], 200)
        self.assertEqual(self.request("GET", "/api/projects",
                                      self.session_for(b))[0], 403)

    def test_revocation_kills_a_live_session(self):
        user = auth.sign_in(self.db, {"sub": "s1", "email": "r@x", "name": "R"})
        self.db.grant_role(user["id"], 1, "viewer", user["id"])
        cookie = self.session_for(user)
        self.assertEqual(self.request("GET", "/api/projects", cookie)[0], 200)
        self.db.bump_token_version(user["id"], user["id"])
        self.assertEqual(self.request("GET", "/api/projects", cookie)[0], 401)

    def test_me_returns_identity_and_roles(self):
        user = auth.sign_in(self.db, {
            "sub": "s1", "email": "r@commsecurity.com.au", "name": "Richard"})
        self.db.grant_role(user["id"], 1, "viewer", user["id"])
        status, body = self.request("GET", "/api/me", self.session_for(user))
        self.assertEqual(status, 200)
        self.assertEqual(body["display_name"], "Richard")
        self.assertEqual(body["roles"], [{"entity_id": 1, "role": "viewer"}])

    def test_projects_are_scoped_to_granted_entities(self):
        """A grant on entity 1 must not reveal entity 2's projects."""
        user = auth.sign_in(self.db, {"sub": "s1", "email": "r@x", "name": "R"})
        self.db.grant_role(user["id"], 1, "viewer", user["id"])
        with self.db._tx() as c:
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,created_ts)
                         VALUES (1,'Mine','JN-1','Active',0)""")
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,created_ts)
                         VALUES (2,'Theirs','JN-2','Active',0)""")
        _, body = self.request("GET", "/api/projects", self.session_for(user))
        self.assertEqual([p["name"] for p in body["projects"]], ["Mine"])

    def test_forged_cookie_is_refused(self):
        forged = auth.mint_session(b"z" * 32, 1, 1)
        status, _ = self.request("GET", "/api/projects",
                                 f"{auth.COOKIE_NAME}={forged}")
        self.assertEqual(status, 403)

    def test_security_headers_present_on_api_responses(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/healthz")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        c.close()

    def test_boot_created_the_session_key_and_secrets_store(self):
        self.assertTrue(os.path.exists(self.cfg.session_key_path))
        self.assertTrue(os.path.exists(self.cfg.secrets_path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
