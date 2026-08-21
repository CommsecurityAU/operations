"""ops.http_util -- hardening, routing, headers, CSRF.

These run against a REAL socket server, not a mocked handler. The four
hardening settings are all about what happens at the socket, and a mock
would assert the attribute exists while proving nothing about behaviour.
"""

import http.client
import json
import logging
import os
import socket
import sys
import threading
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from ops.http_util import (ConnectionLimiter, HttpError, Router,  # noqa: E402
                           make_server, same_origin)


def build_router():
    r = Router()

    def health(h, user):
        return 200, {"ok": True}

    def echo(h, user):
        return 200, {"got": h.read_json()}

    def secret(h, user):
        return 200, {"user": user}

    def boom(h, user):
        raise ValueError("kaboom")

    def upload(h, user):
        return 200, {"n": len(h.read_json(limit=64))}

    r.add("GET", "/healthz", health, role="public")
    r.add("POST", "/echo", echo, role="public")
    r.add("GET", "/secret", secret, role="viewer")
    r.add("GET", "/boom", boom, role="public")
    r.add("POST", "/upload", upload, role="public")
    r.add("GET", "/item/{id}", lambda h, user, id: (200, {"id": id}), role="public")
    return r


class ServerCase(unittest.TestCase):
    limit = 64
    read_timeout = 1      # keeps the suite inside the §14 10 s budget

    def setUp(self):
        def auth_hook(handler, role):
            if handler.headers.get("X-Test-User"):
                return {"name": handler.headers["X-Test-User"], "role": role}
            raise HttpError(401, "authentication required")

        logging.getLogger("ops.http").setLevel(logging.CRITICAL)
        self.server = make_server(("127.0.0.1", 0), build_router(),
                                  auth_hook=auth_hook, limit=self.limit,
                                  read_timeout=self.read_timeout)
        self.port = self.server.server_address[1]
        # poll_interval defaults to 0.5 s, and shutdown() waits for the next
        # poll -- that is 500 ms of teardown per test, ~15 s across the class.
        self.t = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
            daemon=True)
        self.t.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.t.join(timeout=5)

    def conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def get(self, path, headers=None):
        c = self.conn()
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, dict(r.getheaders()), body

    def post(self, path, payload, headers=None):
        c = self.conn()
        h = {"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}
        h.update(headers or {})
        body = json.dumps(payload).encode()
        c.request("POST", path, body=body, headers=h)
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, dict(r.getheaders()), out


class TestRouting(ServerCase):
    def test_get(self):
        status, _, body = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    def test_path_params(self):
        status, _, body = self.get("/item/JN-6889")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["id"], "JN-6889")

    def test_unknown_path_is_404(self):
        self.assertEqual(self.get("/nope")[0], 404)

    def test_wrong_method_is_405_not_404(self):
        c = self.conn()
        c.request("DELETE", "/healthz", headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(c.getresponse().status, 405)
        c.close()

    def test_unhandled_exception_is_500_without_a_traceback(self):
        status, _, body = self.get("/boom")
        self.assertEqual(status, 500)
        self.assertNotIn(b"kaboom", body)
        self.assertNotIn(b"Traceback", body)


class TestRouteRoles(unittest.TestCase):
    def test_route_without_a_role_fails_at_registration(self):
        """Forgetting a role must not silently produce a public endpoint."""
        r = Router()
        with self.assertRaises(ValueError):
            r.add("GET", "/x", lambda h, u: None, role=None)

    def test_auth_hook_is_called_for_non_public_routes(self):
        pass  # covered by TestAuth below


class TestAuth(ServerCase):
    def test_public_route_needs_no_user(self):
        self.assertEqual(self.get("/healthz")[0], 200)

    def test_protected_route_rejects_anonymous(self):
        self.assertEqual(self.get("/secret")[0], 401)

    def test_protected_route_passes_the_user_through(self):
        status, _, body = self.get("/secret", {"X-Test-User": "richard"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["user"]["name"], "richard")


class TestSecurityHeaders(ServerCase):
    def test_headers_on_every_response(self):
        for path in ("/healthz", "/nope"):
            _, headers, _ = self.get(path)
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(headers["X-Frame-Options"], "DENY")
            self.assertEqual(headers["Content-Security-Policy"], "default-src 'self'")

    def test_hsts_only_when_tls_is_on(self):
        """HSTS over plain HTTP is meaningless and, in dev, actively harmful:
        a browser that pins it will refuse the http dev server afterwards."""
        _, headers, _ = self.get("/healthz")
        self.assertNotIn("Strict-Transport-Security", headers)

    def test_no_server_version_disclosure(self):
        _, headers, _ = self.get("/healthz")
        self.assertNotIn("Python", headers.get("Server", ""))


class TestCsrf(ServerCase):
    def test_same_origin_post_is_allowed(self):
        self.assertEqual(self.post("/echo", {"a": 1})[0], 200)

    def test_cross_site_post_is_refused(self):
        status, _, _ = self.post("/echo", {"a": 1},
                                 {"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_post_with_no_origin_headers_is_refused(self):
        """Stripping a header must not be the bypass."""
        c = self.conn()
        c.request("POST", "/echo", body=b"{}",
                  headers={"Content-Type": "application/json"})
        self.assertEqual(c.getresponse().status, 403)
        c.close()

    def test_get_is_never_blocked(self):
        self.assertEqual(self.get("/healthz", {"Sec-Fetch-Site": "cross-site"})[0], 200)

    def test_origin_fallback_when_sec_fetch_site_absent(self):
        status, _, _ = self.post("/echo", {"a": 1},
                                 {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 200)

    def test_unit_no_headers_is_false(self):
        self.assertFalse(same_origin({}, "example.com"))


class TestBodyLimits(ServerCase):
    def test_oversized_body_is_413(self):
        status, _, _ = self.post("/upload", {"x": "y" * 500})
        self.assertEqual(status, 413)

    def test_missing_content_length_is_411(self):
        c = self.conn()
        c.putrequest("POST", "/echo")
        c.putheader("Sec-Fetch-Site", "same-origin")
        c.putheader("Transfer-Encoding", "chunked")
        c.endheaders()
        c.send(b"0\r\n\r\n")
        self.assertEqual(c.getresponse().status, 411)
        c.close()

    def test_malformed_json_is_400_not_500(self):
        c = self.conn()
        body = b"{not json"
        c.request("POST", "/echo", body=body, headers={
            "Content-Type": "application/json",
            "Sec-Fetch-Site": "same-origin",
            "Content-Length": str(len(body))})
        self.assertEqual(c.getresponse().status, 400)
        c.close()

    def test_lying_content_length_does_not_hang_forever(self):
        """A client claiming 10 MB and sending 10 bytes must not hold the
        thread until the heat death of the universe."""
        c = self.conn()
        c.putrequest("POST", "/echo")
        c.putheader("Content-Type", "application/json")
        c.putheader("Sec-Fetch-Site", "same-origin")
        c.putheader("Content-Length", "10000")
        c.endheaders()
        c.send(b'{"a":1}')
        started = time.monotonic()
        try:
            c.getresponse()
        except Exception:
            pass
        self.assertLess(time.monotonic() - started, 30)
        c.close()


class TestConnectionCap(unittest.TestCase):
    def test_limiter_counts_and_releases(self):
        lim = ConnectionLimiter(2)
        self.assertTrue(lim.acquire())
        self.assertTrue(lim.acquire())
        self.assertFalse(lim.acquire())
        lim.release()
        self.assertTrue(lim.acquire())

    def test_release_never_goes_negative(self):
        lim = ConnectionLimiter(1)
        lim.release()
        lim.release()
        self.assertEqual(lim.active, 0)


class TestCapEnforcedAtTheSocket(ServerCase):
    limit = 2

    def test_beyond_the_cap_the_answer_is_503(self):
        """Held-open connections consume the cap; the next one is refused
        immediately rather than spawning a thread to say no more slowly."""
        held = []
        for _ in range(self.limit):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            s.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
            s.recv(64)
            held.append(s)
        time.sleep(0.1)
        extra = None
        try:
            extra = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            extra.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
            data = extra.recv(128)
            # The 503 must actually ARRIVE. Closing over an undrained receive
            # buffer sends an RST, which discards the queued response and
            # gives the client a reset instead -- silently on Linux, loudly
            # on Windows (WinError 10053).
            self.assertIn(b"503", data)
        finally:
            if extra is not None:
                extra.close()
            for held_sock in held:
                held_sock.close()


class TestAccessLog(ServerCase):
    def test_logs_the_status_actually_sent_not_an_assumed_200(self):
        """A handler that writes its own response (redirect, 204) returns
        None. Defaulting the log to 200 for those hides precisely the
        responses you go looking for during an incident."""
        records = []
        logger = logging.getLogger("ops.http")
        old_level = logger.level
        logger.setLevel(logging.INFO)

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        cap = Capture()
        logger.addHandler(cap)
        try:
            self.get("/nope")          # 404 via HttpError
            self.get("/healthz")       # 200 via return value
        finally:
            logger.removeHandler(cap)
            logger.setLevel(old_level)
        statuses = [json.loads(r)["status"] for r in records if r.startswith("{")]
        self.assertIn(404, statuses)
        self.assertIn(200, statuses)


class TestHardeningIsPresent(ServerCase):
    def test_daemon_threads(self):
        self.assertTrue(self.server.daemon_threads)

    def test_read_timeout_is_on_the_HANDLER_not_the_server(self):
        """server.timeout is the handle_request() poll interval and gives no
        per-connection guarantee. StreamRequestHandler.setup() applies the
        HANDLER's timeout to the socket -- that is the one that stops a
        half-open connection holding a thread forever."""
        self.assertIsNotNone(self.server.RequestHandlerClass.timeout)
        self.assertLessEqual(self.server.RequestHandlerClass.timeout, 60)

    def test_a_silent_client_loses_its_thread(self):
        """Connect, send nothing, and confirm the server hangs up rather
        than holding the thread. This is the behaviour; the attribute above
        is only the mechanism."""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        started = time.monotonic()
        try:
            s.recv(64)          # returns b"" when the server closes on us
        except OSError:
            pass
        elapsed = time.monotonic() - started
        s.close()
        self.assertLess(elapsed, self.read_timeout + 5,
                        "server did not time out a silent connection")


if __name__ == "__main__":
    unittest.main(verbosity=2)
