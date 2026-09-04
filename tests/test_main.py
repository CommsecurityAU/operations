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
import ssl
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
from ops import main as main_mod  # noqa: E402
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

    def test_tls_material_is_never_logged(self):
        cfg = from_env({"OPS_TLS_CERT": "Q0VSVA==", "OPS_TLS_KEY": "S0VZ"})
        blob = json.dumps(cfg.redacted())
        self.assertNotIn("S0VZ", blob)
        self.assertNotIn("Q0VSVA", blob)
        self.assertIn("bytes", blob)
        self.assertEqual(from_env({}).tls_key_b64, "")

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


class TestCertExpiry(unittest.TestCase):
    """Certificate dates from a DER walk, using only public stdlib.

    The tempting shortcut is `ssl._ssl._test_decode_cert` -- private,
    undocumented, named for testing. Depending on it would break silently on
    a base-image bump, which is the exact failure the digest pin exists to
    prevent.
    """

    def make_cert(self, days):
        import subprocess
        crt = os.path.join(self.dir, f"c{days}.pem")
        key = os.path.join(self.dir, f"k{days}.pem")
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key,
             "-out", crt, "-days", str(days), "-nodes",
             "-subj", "/CN=ops.commsecurity.com.au"],
            check=True, capture_output=True)
        return crt

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        if shutil.which("openssl") is None:
            self.skipTest("openssl not available")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_matches_openssl(self):
        import subprocess
        crt = self.make_cert(45)
        with open(crt, "rb") as f:
            mine = main_mod.cert_not_after(f.read())
        out = subprocess.run(["openssl", "x509", "-in", crt, "-noout",
                              "-enddate"], capture_output=True, text=True).stdout
        theirs = ssl.cert_time_to_seconds(out.strip().split("=", 1)[1])
        self.assertEqual(mine, theirs)

    def test_days_remaining(self):
        self.assertIn(main_mod.check_cert_expiry(self.make_cert(45)), (44, 45))

    def test_warns_when_close_to_expiry(self):
        with self.assertLogs("ops.main", level="WARNING") as cm:
            main_mod.check_cert_expiry(self.make_cert(10))
        self.assertTrue(any("EXPIRES IN" in m for m in cm.output))

    def test_no_warning_when_far_from_expiry(self):
        logger = logging.getLogger("ops.main")
        logger.setLevel(logging.WARNING)
        with self.assertNoLogs("ops.main", level="WARNING"):
            main_mod.check_cert_expiry(self.make_cert(400))

    def test_missing_file_returns_none(self):
        self.assertIsNone(main_mod.check_cert_expiry("/no/such/cert.pem"))

    def test_garbage_returns_none_rather_than_crashing_boot(self):
        bad = os.path.join(self.dir, "bad.pem")
        with open(bad, "w") as f:
            f.write("-----BEGIN CERTIFICATE-----\nbm90IGEgY2VydA==\n"
                    "-----END CERTIFICATE-----\n")
        logging.getLogger("ops.main").setLevel(logging.CRITICAL)
        self.assertIsNone(main_mod.check_cert_expiry(bad))


class TestCodeFingerprint(unittest.TestCase):
    """The staleness detector. Python loads a module once, so a running
    server can be several edits behind the working tree while every test
    passes and the browser disagrees."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "migrations"))
        with open(os.path.join(self.dir, "a.py"), "w") as f:
            f.write("x = 1\n")
        with open(os.path.join(self.dir, "migrations", "001.sql"), "w") as f:
            f.write("CREATE TABLE t (a INTEGER) STRICT;\n")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_is_stable(self):
        a = main_mod.code_fingerprint(self.dir)
        self.assertEqual(a, main_mod.code_fingerprint(self.dir))
        self.assertEqual(len(a), 12)

    def test_changes_when_python_changes(self):
        before = main_mod.code_fingerprint(self.dir)
        with open(os.path.join(self.dir, "a.py"), "w") as f:
            f.write("x = 2\n")
        self.assertNotEqual(before, main_mod.code_fingerprint(self.dir))

    def test_changes_when_sql_changes(self):
        """A migration edit must move it too -- a stale runner is exactly as
        confusing as stale handlers."""
        before = main_mod.code_fingerprint(self.dir)
        with open(os.path.join(self.dir, "migrations", "001.sql"), "a") as f:
            f.write("-- edited\n")
        self.assertNotEqual(before, main_mod.code_fingerprint(self.dir))

    def test_changes_when_a_file_is_added(self):
        before = main_mod.code_fingerprint(self.dir)
        with open(os.path.join(self.dir, "b.py"), "w") as f:
            f.write("y = 1\n")
        self.assertNotEqual(before, main_mod.code_fingerprint(self.dir))

    def test_ignores_pycache(self):
        """Bytecode is derived, and it appears and disappears on its own."""
        before = main_mod.code_fingerprint(self.dir)
        cache = os.path.join(self.dir, "__pycache__")
        os.makedirs(cache)
        with open(os.path.join(cache, "a.cpython-312.pyc"), "wb") as f:
            f.write(b"\x00compiled")
        self.assertEqual(before, main_mod.code_fingerprint(self.dir))

    def test_renaming_a_file_changes_it(self):
        """Content alone is not enough: two files swapping names is a
        different program."""
        before = main_mod.code_fingerprint(self.dir)
        os.rename(os.path.join(self.dir, "a.py"),
                  os.path.join(self.dir, "z.py"))
        self.assertNotEqual(before, main_mod.code_fingerprint(self.dir))


class TestTlsPreflight(unittest.TestCase):
    """TLS had never been exercised before the first deploy was planned.

    All three failure modes surfaced as `FileNotFoundError: [Errno 2] No
    such file or directory` with no path, or a raw OpenSSL string. Those are
    the messages someone reads while the service is down.
    """

    def setUp(self):
        if shutil.which("openssl") is None:
            self.skipTest("openssl not available")
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "tls"))
        os.makedirs(os.path.join(self.dir, "secrets"))
        LocalProvider(os.path.join(self.dir, "secrets", "store.json")).set(
            "OIDC_CLIENT_SECRET", "x")
        logging.getLogger("ops.main").setLevel(logging.CRITICAL)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def make_pair(self, crt="server.crt", key="server.key", days=365):
        import subprocess
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048",
             "-keyout", os.path.join(self.dir, "tls", key),
             "-out", os.path.join(self.dir, "tls", crt),
             "-days", str(days), "-nodes", "-subj", "/CN=ops.test"],
            check=True, capture_output=True)

    def cfg(self):
        return Config(data_dir=self.dir, tls=True, port=0,
                      oidc_client_id="cid",
                      oidc_redirect_uri="https://ops.test/auth/callback")

    def boot_it(self):
        return boot(cfg=self.cfg(),
                    env={"OPS_SECRETS_PATH":
                         os.path.join(self.dir, "secrets", "store.json")},
                    serve=False)

    def test_a_complete_pair_boots_over_tls(self):
        self.make_pair()
        db, server, sched = self.boot_it()
        try:
            self.assertTrue(hasattr(server.socket, "context"))
        finally:
            sched.stop()
            server.server_close()
            db.close()

    def test_missing_certificate_names_the_path(self):
        with self.assertRaises(SystemExit) as e:
            self.boot_it()
        self.assertEqual(e.exception.code, 2)

    def test_missing_key_is_refused(self):
        self.make_pair()
        os.unlink(os.path.join(self.dir, "tls", "server.key"))
        with self.assertRaises(SystemExit) as e:
            self.boot_it()
        self.assertEqual(e.exception.code, 2)

    def test_a_mismatched_pair_is_refused(self):
        """Two separate issuances is the likely mistake, and OpenSSL's own
        message for it says nothing about what to do."""
        self.make_pair()
        self.make_pair(crt="unused.crt", key="server.key")
        with self.assertRaises(SystemExit) as e:
            self.boot_it()
        self.assertEqual(e.exception.code, 2)

    def test_the_messages_name_the_file_and_the_remedy(self):
        with self.assertLogs("ops.main", level="ERROR") as cm:
            with self.assertRaises(SystemExit):
                self.boot_it()
        text = " ".join(cm.output)
        self.assertIn("server.crt", text)
        self.assertIn("internal CA", text)
        self.assertIn("OPS_TLS=off", text)

    # ---- material delivered through the environment (release JSON)
    def env_pair(self):
        """Issue a pair elsewhere, base64 it, and remove the files, so the
        only way the boot can find them is through the environment."""
        import base64
        self.make_pair(crt="issued.crt", key="issued.key")
        out = []
        for name in ("issued.crt", "issued.key"):
            path = os.path.join(self.dir, "tls", name)
            with open(path, "rb") as f:
                out.append(base64.b64encode(f.read()).decode())
            os.unlink(path)
        return out

    def cfg_from_env(self, **extra):
        return Config(data_dir=self.dir, tls=True, port=0,
                      oidc_client_id="cid",
                      oidc_redirect_uri="https://ops.test/auth/callback",
                      **extra)

    def boot_with(self, cfg):
        return boot(cfg=cfg,
                    env={"OPS_SECRETS_PATH":
                         os.path.join(self.dir, "secrets", "store.json")},
                    serve=False)

    def test_an_env_delivered_pair_boots_and_lands_on_the_volume(self):
        crt, key = self.env_pair()
        cfg = self.cfg_from_env(tls_cert_b64=crt, tls_key_b64=key)
        db, server, sched = self.boot_with(cfg)
        try:
            self.assertTrue(hasattr(server.socket, "context"))
        finally:
            sched.stop()
            server.server_close()
            db.close()
        self.assertTrue(os.path.exists(cfg.tls_cert))
        self.assertTrue(os.path.exists(cfg.tls_key))
        if os.name != "nt":
            self.assertEqual(os.stat(cfg.tls_key).st_mode & 0o777, 0o600)

    def test_env_material_replaces_a_stale_pair_on_the_volume(self):
        """A renewal is a new release. The old files must not win."""
        self.make_pair()                      # stale pair on the volume
        with open(os.path.join(self.dir, "tls", "server.crt"), "rb") as f:
            stale = f.read()
        crt, key = self.env_pair()
        db, server, sched = self.boot_with(
            self.cfg_from_env(tls_cert_b64=crt, tls_key_b64=key))
        sched.stop(); server.server_close(); db.close()
        with open(os.path.join(self.dir, "tls", "server.crt"), "rb") as f:
            self.assertNotEqual(f.read(), stale)

    def test_half_a_pair_is_refused_and_named(self):
        crt, _ = self.env_pair()
        with self.assertLogs("ops.main", level="ERROR") as cm:
            with self.assertRaises(SystemExit) as e:
                self.boot_with(self.cfg_from_env(tls_cert_b64=crt))
        self.assertEqual(e.exception.code, 2)
        self.assertIn("OPS_TLS_KEY", " ".join(cm.output))

    def test_garbage_material_is_refused_and_named(self):
        with self.assertLogs("ops.main", level="ERROR") as cm:
            with self.assertRaises(SystemExit) as e:
                self.boot_with(self.cfg_from_env(tls_cert_b64="not base64!",
                                                 tls_key_b64="bm9wZQ=="))
        self.assertEqual(e.exception.code, 2)
        self.assertIn("OPS_TLS_CERT", " ".join(cm.output))

    def test_raw_pem_is_accepted_too(self):
        """Someone will paste the file rather than base64 it."""
        import base64
        crt, key = self.env_pair()
        db, server, sched = self.boot_with(self.cfg_from_env(
            tls_cert_b64=base64.b64decode(crt).decode(),
            tls_key_b64=base64.b64decode(key).decode()))
        sched.stop(); server.server_close(); db.close()

    def test_it_fails_before_the_server_is_built(self):
        """A bad certificate must fail the DEPLOY, not the first request."""
        import socket as socket_mod
        before = len(socket_mod.socket.__subclasses__())
        with self.assertRaises(SystemExit):
            self.boot_it()
        self.assertEqual(len(socket_mod.socket.__subclasses__()), before)


class TestStaticIcons(unittest.TestCase):
    """A 404 favicon is invisible: the browser shows a generic page icon and
    nobody investigates. So it is asserted rather than assumed."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        secrets_path = os.path.join(self.dir, "secrets", "store.json")
        LocalProvider(secrets_path).set("OIDC_CLIENT_SECRET", "x")
        cfg = Config(data_dir=self.dir, tls=False, port=0,
                     oidc_client_id="cid", oidc_redirect_uri="http://x/cb")
        logging.getLogger("ops.http").setLevel(logging.CRITICAL)
        self.db, self.server, self.sched = boot(
            cfg=cfg, env={"OPS_SECRETS_PATH": secrets_path}, serve=False)
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

    def fetch(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, r.getheader("Content-Type"), body

    def test_static_files_are_never_stored(self):
        """`no-store`, not `no-cache`: the latter permits STORING and only
        requires revalidation, and with no ETag or Last-Modified there is
        nothing to revalidate against -- an ambiguous state browsers resolve
        differently. It cost a round trip, with a module correct on disk and
        an old copy still running in the tab."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/static/app.js")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        c.close()

    def test_the_favicon_is_served_as_an_image(self):
        status, kind, body = self.fetch("/static/favicon.png")
        self.assertEqual(status, 200)
        self.assertEqual(kind, "image/png")
        self.assertTrue(body.startswith(b"\x89PNG"))

    def test_the_touch_icon_is_served(self):
        self.assertEqual(self.fetch("/static/apple-touch-icon.png")[0], 200)

    def test_the_page_actually_references_them(self):
        """Serving an icon nothing links to is the same as not having one."""
        _s, _k, page = self.fetch("/")
        self.assertIn(b"/static/favicon.png", page)
        self.assertIn(b"/static/apple-touch-icon.png", page)

    def test_the_icons_stay_small(self):
        """They are fetched on every cold visit. The source artwork was
        58 KB; a tab icon has no business costing that."""
        for name in ("favicon.png", "apple-touch-icon.png"):
            size = os.path.getsize(os.path.join(ROOT, "ops", "static", name))
            self.assertLess(size, 8 * 1024, f"{name} is {size} bytes")


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


class TestStaticFiles(Stp0Case):
    def raw(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path, headers={"Sec-Fetch-Site": "same-origin"})
        r = c.getresponse()
        body = r.read()
        headers = dict(r.getheaders())
        c.close()
        return r.status, headers, body

    def test_index_is_served_at_root(self):
        status, headers, body = self.raw("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"<html", body)

    def test_assets_are_served_with_correct_types(self):
        for path, expected in [("/static/app.js", "javascript"),
                               ("/static/base.css", "css"),
                               ("/static/tokens.css", "css")]:
            status, headers, _ = self.raw(path)
            self.assertEqual(status, 200, path)
            self.assertIn(expected, headers["Content-Type"], path)

    def test_path_traversal_is_refused(self):
        """The router pattern already excludes `/`, but the containment
        check is what survives a refactor of the router."""
        for attack in ("/static/..%2f..%2fops.db", "/static/%2e%2e%2fdb.py",
                       "/static/....//db.py"):
            self.assertEqual(self.raw(attack)[0], 404, attack)

    def test_source_files_are_not_served(self):
        """Only known asset extensions. A .py under static/ would otherwise
        be readable by anyone who can reach the login page."""
        for path in ("/static/main.py", "/static/db.py", "/static/ops.db"):
            self.assertEqual(self.raw(path)[0], 404, path)

    def test_index_is_public_but_the_api_is_not(self):
        """The shell loads for an anonymous browser; main.js then calls
        /api/me, gets 401 and sends them to /login."""
        self.assertEqual(self.raw("/")[0], 200)
        self.assertEqual(self.raw("/api/me")[0], 401)


class TestStp0ExitCriteria(Stp0Case):
    def test_healthz_is_200_after_boot(self):
        status, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["schema"]["missing"], [])

    def test_healthz_reports_the_assets_on_disk(self):
        """A different question from `code`: static files are read per
        request, so the running server is never stale with respect to them
        -- but the DISK can be behind what was delivered, and that gap cost
        a round trip when `-Stale` reported current while claims.js was two
        versions old."""
        _s, body = self.request("GET", "/healthz")
        self.assertEqual(body["assets"], main_mod.asset_fingerprint())
        self.assertEqual(len(body["assets"]), 12)

    def test_the_asset_fingerprint_moves_when_a_static_file_changes(self):
        import tempfile as tf
        d = tf.mkdtemp()
        try:
            with open(os.path.join(d, "app.js"), "w") as f:
                f.write("export const a = 1;\n")
            before = main_mod.asset_fingerprint(d)
            with open(os.path.join(d, "app.js"), "w") as f:
                f.write("export const a = 2;\n")
            self.assertNotEqual(before, main_mod.asset_fingerprint(d))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_the_two_fingerprints_are_independent(self):
        """Changing a stylesheet must not look like changing the server."""
        self.assertNotEqual(main_mod.code_fingerprint(),
                            main_mod.asset_fingerprint())

    def test_healthz_reports_the_code_it_is_running(self):
        """One request answers "is this server running what I just wrote".
        Compare against `python -m ops.main --fingerprint`."""
        _s, body = self.request("GET", "/healthz")
        self.assertEqual(body["code"], main_mod.code_fingerprint())
        self.assertEqual(len(body["code"]), 12)

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

    def test_projects_payload_carries_type_and_client_for_filtering(self):
        """Type is a live taxonomy and one of STP-5's rollup axes, so it has
        to survive as data, not just as a column heading."""
        user = auth.sign_in(self.db, {"sub": "s1", "email": "r@x", "name": "R"})
        self.db.grant_role(user["id"], 1, "viewer", user["id"])
        with self.db._tx() as c:
            c.execute("INSERT INTO client (entity_id, name) VALUES (1,'Hines')")
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,
                             type_id,client_id,project_lead,created_ts)
                         VALUES (1,'Typed','JN-1','Active',
                             (SELECT id FROM project_type WHERE code='ICN'),
                             (SELECT id FROM client WHERE name='Hines'),
                             'Joshua Koch',0)""")
        _, body = self.request("GET", "/api/projects", self.session_for(user))
        row = body["projects"][0]
        self.assertEqual(row["type"], "ICN")
        self.assertEqual(row["client"], "Hines")
        self.assertEqual(row["project_lead"], "Joshua Koch")

    def test_a_project_with_no_type_still_appears(self):
        """LEFT JOIN, not INNER: an unmatched type is a data problem worth
        seeing on screen. An inner join would hide it by dropping the row --
        the register would silently be short and still look complete."""
        user = auth.sign_in(self.db, {"sub": "s1", "email": "r@x", "name": "R"})
        self.db.grant_role(user["id"], 1, "viewer", user["id"])
        with self.db._tx() as c:
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,created_ts)
                         VALUES (1,'Untyped','JN-2','Active',0)""")
        _, body = self.request("GET", "/api/projects", self.session_for(user))
        self.assertEqual(len(body["projects"]), 1)
        self.assertEqual(body["projects"][0]["type"], "(untyped)")
        self.assertEqual(body["projects"][0]["client"], "(no client)")

    def test_projects_payload_carries_the_flag_the_screen_renders(self):
        """projects.js reads needs_resolution to mark flagged rows. It comes
        from `project`, not the view, so a join is doing the work -- worth a
        test, because dropping it degrades silently to 'nothing is flagged'."""
        user = auth.sign_in(self.db, {"sub": "s1", "email": "r@x", "name": "R"})
        self.db.grant_role(user["id"], 1, "viewer", user["id"])
        with self.db._tx() as c:
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,
                             needs_resolution,created_ts)
                         VALUES (1,'Flagged','TBA','Active',1,0)""")
        _, body = self.request("GET", "/api/projects", self.session_for(user))
        self.assertEqual(body["projects"][0]["needs_resolution"], 1)

    def test_forged_cookie_is_refused_as_401_not_403(self):
        """A bad signature means we do not know who this is -- authenticate.
        403 would tell the browser the user is known and not allowed, which
        gives it no reason to send them to sign in."""
        forged = auth.mint_session(b"z" * 32, 1, 1)
        status, _ = self.request("GET", "/api/projects",
                                 f"{auth.COOKIE_NAME}={forged}")
        self.assertEqual(status, 401)

    def test_a_known_user_without_the_role_is_403(self):
        """The other side of the distinction: identity established, answer
        still no."""
        user = auth.sign_in(self.db, {"sub": "s9", "email": "n@x", "name": "N"})
        status, _ = self.request("GET", "/api/projects",
                                 self.session_for(user))
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
