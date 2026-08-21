"""HTTP plumbing (CS-OP-ARCH-002 §3).

The stdlib's `ThreadingHTTPServer` defaults are development defaults. Four
hardening settings are applied here and are REVIEW-BLOCKING if removed:

  * daemon_threads          -- a hung thread must not block shutdown
  * socket read timeout     -- a half-open connection must not hold a thread
                               forever, which is how the process runs out of
                               threads without any traffic to explain it
  * request body cap        -- enforced BEFORE reading, or an oversized POST
                               is buffered into RSS on its way to being
                               rejected, and the 128 MB budget is fiction
  * concurrent-connection cap -- 503 beyond it, never unbounded thread
                               creation

Handlers are thin: parse -> auth -> call db/module -> respond. Logic in a
handler is a review reject.
"""

import json
import logging
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

log = logging.getLogger("ops.http")

READ_TIMEOUT = 15          # seconds; a client with nothing to say loses its thread
MAX_JSON_BODY = 1 << 20    # 1 MB
MAX_UPLOAD_BODY = 25 << 20  # 25 MB
MAX_CONNECTIONS = 64       # threads are per-connection; this bounds them
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'self'",
    "Referrer-Policy": "same-origin",
}


class HttpError(Exception):
    """Raised anywhere in a handler; becomes a JSON error response."""

    def __init__(self, status, message, detail=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


class Router:
    """Method + path-pattern -> handler, with an explicit role declaration.

    A route registered WITHOUT a role fails at boot (§9). Forgetting to
    declare one must not silently produce a public endpoint, so there is no
    default value and no way to omit it.
    """

    def __init__(self):
        self._routes = []

    def add(self, method, pattern, fn, role):
        if role is None:
            raise ValueError(
                f"route {method} {pattern} has no role declaration; "
                "every route states its required role explicitly")
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
        self._routes.append((method.upper(), regex, fn, role))

    def route(self, pattern, role, method="GET"):
        def deco(fn):
            self.add(method, pattern, fn, role)
            return fn
        return deco

    def match(self, method, path):
        allowed = False
        for m, regex, fn, role in self._routes:
            hit = regex.match(path)
            if hit:
                if m == method.upper():
                    return fn, role, hit.groupdict()
                allowed = True
        if allowed:
            raise HttpError(405, "method not allowed")
        return None, None, None


class ConnectionLimiter:
    """Bounds live connections. Beyond the cap the answer is 503 immediately,
    not a new thread and a slower 503 for everyone."""

    def __init__(self, limit=MAX_CONNECTIONS):
        self.limit = limit
        self._n = 0
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            if self._n >= self.limit:
                return False
            self._n += 1
            return True

    def release(self):
        with self._lock:
            self._n = max(0, self._n - 1)

    @property
    def active(self):
        with self._lock:
            return self._n


def same_origin(headers, host):
    """CSRF belt-and-braces (§3).

    `SameSite=Lax` already blocks cross-site form posts; this rejects the
    residue. `Sec-Fetch-Site` is checked first because it is sent even when
    `Origin` is not, and a request carrying NEITHER header is refused rather
    than waved through -- otherwise stripping a header is the bypass.
    """
    fetch_site = headers.get("Sec-Fetch-Site")
    if fetch_site is not None:
        return fetch_site in ("same-origin", "none")
    origin = headers.get("Origin")
    if origin is not None:
        return origin.split("://")[-1].lower() == (host or "").lower()
    return False


class Handler(BaseHTTPRequestHandler):
    """Thin. Parse, authorise, dispatch, respond."""

    server_version = "ops"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = READ_TIMEOUT

    # Bound by make_server() on a per-server subclass. Annotated as optional
    # so the checker knows the shape; _dispatch refuses rather than crashing
    # if a server is ever constructed without them.
    router: "Router | None" = None
    limiter: "ConnectionLimiter | None" = None
    tls_enabled: bool = False
    auth_hook: "Callable[..., Any] | None" = None  # (handler, role) -> user

    # ------------------------------------------------------------- output
    def _send(self, status, body=b"", content_type="application/json",
              extra_headers=None):
        # Record what was ACTUALLY sent. Handlers that write their own
        # response (redirects, 204s) return None, and inferring 200 for them
        # makes the access log lie about exactly the responses you most want
        # to see during an incident.
        self._status = status
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        if self.tls_enabled:
            self.send_header("Strict-Transport-Security",
                             "max-age=31536000; includeSubDomains")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def json(self, status, payload, extra_headers=None):
        self._send(status, json.dumps(payload).encode(), extra_headers=extra_headers)

    def error(self, exc):
        payload = {"error": exc.message}
        if exc.detail:
            payload["detail"] = exc.detail
        self.json(exc.status, payload)

    # -------------------------------------------------------------- input
    def read_json(self, limit=MAX_JSON_BODY):
        """Cap enforced from Content-Length BEFORE reading a byte. Reading
        first and measuring after is how an oversized POST costs you the
        memory anyway."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise HttpError(411, "length required")
        try:
            length = int(raw)
        except ValueError:
            raise HttpError(400, "bad content-length")
        if length < 0:
            raise HttpError(400, "bad content-length")
        if length > limit:
            raise HttpError(413, "request body too large",
                            f"limit {limit} bytes")
        if length == 0:
            return {}
        body = self.rfile.read(length)
        if len(body) != length:
            raise HttpError(400, "truncated request body")
        try:
            return json.loads(body)
        except ValueError:
            raise HttpError(400, "body is not valid JSON")

    # ----------------------------------------------------------- dispatch
    def _dispatch(self):
        started = time.monotonic()
        status = 500
        try:
            path = self.path.split("?", 1)[0]
            if self.router is None:
                raise HttpError(500, "no router configured")
            fn, role, params = self.router.match(self.command, path)
            if fn is None:
                raise HttpError(404, "not found")
            if self.command not in SAFE_METHODS:
                if not same_origin(self.headers, self.headers.get("Host")):
                    raise HttpError(403, "cross-origin request refused")
            user = None
            if role != "public":
                if self.auth_hook is None:
                    raise HttpError(500, "no auth hook configured")
                user = self.auth_hook(self, role)
            result = fn(self, user, **params)
            if result is not None:
                sent, payload = result
                self.json(sent, payload)
                status = sent
            else:
                status = getattr(self, "_status", 200)
        except HttpError as e:
            status = e.status
            self.error(e)
        except Exception:
            log.exception("unhandled error")
            status = 500
            self.error(HttpError(500, "internal error"))
        finally:
            log.info(json.dumps({
                "method": self.command,
                "path": self.path.split("?", 1)[0],
                "status": status,
                "ms": round((time.monotonic() - started) * 1000, 1),
            }))

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = do_HEAD = _dispatch

    def log_message(self, format, *args):  # noqa: A002 - base class name
        """Silence BaseHTTPRequestHandler's stderr logging; we emit one JSON
        line per request in _dispatch instead. The parameter keeps the base
        class's name so the override stays substitutable."""


class Server(ThreadingHTTPServer):
    daemon_threads = True          # hardening 1 of 4
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, addr, handler_cls, limiter):
        super().__init__(addr, handler_cls)
        self.limiter = limiter

    def process_request(self, request, client_address):
        """Hardening 4 of 4: refuse beyond the cap instead of spawning a
        thread. Written before the socket is handed to a thread, so an
        overloaded server answers immediately rather than more slowly."""
        if not self.limiter.acquire():
            self._refuse(request)
            return
        super().process_request(request, client_address)

    @staticmethod
    def _refuse(request):
        """Answer 503 and close CLEANLY.

        Closing a socket that still has unread data in its receive buffer
        makes the OS send an RST, and an RST discards whatever we just
        queued -- so the client gets a connection reset instead of the 503.
        We never read the request here, so that buffer is always non-empty.
        Drain it (bounded, so a slowloris cannot make us read forever), then
        shut down the write side and close.
        """
        try:
            request.sendall(
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        try:
            request.settimeout(0.25)
            drained = 0
            while drained < 64 * 1024:
                chunk = request.recv(4096)
                if not chunk:
                    break
                drained += len(chunk)
        except OSError:
            pass
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            request.close()
        except OSError:
            pass

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            pass

    def close_request(self, request):
        try:
            super().close_request(request)
        finally:
            self.limiter.release()

    def handle_error(self, request, client_address):
        """A client that hangs up mid-response is normal, not an incident.
        Log it at debug and move on rather than dumping a traceback to
        stderr, which trains people to ignore stderr."""
        log.debug("connection error from %s", client_address, exc_info=True)


def make_server(addr, router, auth_hook=None, tls_enabled=False,
                limit=MAX_CONNECTIONS, read_timeout=READ_TIMEOUT):
    limiter = ConnectionLimiter(limit)

    class Bound(Handler):
        router: "Router | None" = None
        limiter: "ConnectionLimiter | None" = None
        auth_hook: "Callable[..., Any] | None" = None
        tls_enabled: bool = False

    Bound.router = router
    Bound.limiter = limiter
    Bound.auth_hook = staticmethod(auth_hook) if auth_hook else None
    Bound.tls_enabled = tls_enabled
    # Hardening 2 of 4. This MUST be the handler class attribute, not
    # server.timeout -- StreamRequestHandler.setup() applies it to the
    # connection socket, whereas server.timeout is only the handle_request()
    # poll interval and gives no per-connection guarantee at all. Asserting
    # the wrong one is a test that passes while the protection is absent.
    Bound.timeout = read_timeout
    return Server(addr, Bound, limiter)
