"""Entrypoint (CS-OP-ARCH-002 §0, §3, §4, §12).

Boot order is load-bearing:

    config -> resolve secrets -> snapshot -> migrate -> serve

The snapshot happens BEFORE migrate so every rollback has a matching
pre-migration file. Note the asymmetry the fleet manager creates: image
rollback is automatic, database rollback is NOT -- the snapshot exists, but
restoring it is a deliberate operator act that discards writes since. That
is why N-1 compatibility and a forward-compatible /healthz are load-bearing
rather than tidy.

Anything that fails here exits non-zero. Non-zero = unhealthy = automatic
rollback, so a misconfiguration self-reports instead of running degraded.
"""

import json
import logging
import os
import ssl
import sys
import time

from ops import auth, backup, config as config_mod
from ops.db import Db
from ops.http_util import HttpError, Router, make_server
from ops.secrets import SecretError, build_provider, resolve_config

log = logging.getLogger("ops.main")

MIGRATIONS = os.path.join(os.path.dirname(__file__), "migrations")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}
MODULES = []          # §6. Registered here, explicitly. Empty at STP-0.
CERT_WARN_DAYS = 30


def setup_logging():
    logging.basicConfig(
        level=os.environ.get("OPS_LOG_LEVEL", "INFO"),
        format="%(message)s", stream=sys.stdout)


# ------------------------------------------------------------------ certs
def _der_tlv(data, offset=0):
    """One DER tag-length-value. Returns (tag, value_bytes, next_offset)."""
    tag = data[offset]
    i = offset + 1
    length = data[i]
    i += 1
    if length & 0x80:                       # long form
        count = length & 0x7F
        length = int.from_bytes(data[i:i + count], "big")
        i += count
    return tag, data[i:i + length], i + length


def _der_children(value):
    out, off = [], 0
    while off < len(value):
        tag, child, off = _der_tlv(value, off)
        out.append((tag, child))
    return out


def cert_not_after(pem_bytes):
    """Certificate expiry, in unix seconds, using only public stdlib.

    Python exposes no supported API for reading a certificate file's dates
    without a live connection. The obvious shortcut is `ssl._ssl._test_decode_cert`,
    but it is private, undocumented and named for testing -- a load-bearing
    dependency on it would break silently on a base-image bump, which is
    exactly the class of failure the digest pin exists to prevent.

    So walk the DER instead. X.509 is:
        Certificate ::= SEQUENCE { tbsCertificate, sigAlg, sigValue }
        tbsCertificate ::= SEQUENCE { [0] version?, serial, sigAlg,
                                      issuer, validity, ... }
        Validity ::= SEQUENCE { notBefore, notAfter }
    """
    der = ssl.PEM_cert_to_DER_cert(pem_bytes.decode())
    _, cert_body, _ = _der_tlv(der)
    tbs_tag, tbs_body = _der_children(cert_body)[0]
    if tbs_tag != 0x30:
        raise ValueError("tbsCertificate is not a SEQUENCE")
    items = _der_children(tbs_body)
    idx = 1 if items[0][0] == 0xA0 else 0    # optional explicit version
    validity_tag, validity = items[idx + 3]
    if validity_tag != 0x30:
        raise ValueError("validity is not a SEQUENCE")
    not_after_tag, not_after = _der_children(validity)[1]
    text = not_after.decode("ascii")
    if not_after_tag == 0x17:                # UTCTime, YYMMDDHHMMSSZ
        return ssl.cert_time_to_seconds(
            time.strftime("%b %d %H:%M:%S %Y GMT",
                          time.strptime(text, "%y%m%d%H%M%SZ")))
    if not_after_tag == 0x18:                # GeneralizedTime
        return ssl.cert_time_to_seconds(
            time.strftime("%b %d %H:%M:%S %Y GMT",
                          time.strptime(text, "%Y%m%d%H%M%SZ")))
    raise ValueError(f"unexpected time tag {not_after_tag:#x}")


def check_cert_expiry(cert_path, warn_days=CERT_WARN_DAYS, now=None):
    """An internal CA cert dying takes the whole app down and nothing else
    will notice (§3). Returns days remaining, or None if unreadable."""
    if not os.path.exists(cert_path):
        return None
    try:
        with open(cert_path, "rb") as f:
            expires = cert_not_after(f.read())
    except Exception as e:
        log.warning("could not read TLS certificate expiry: %s", type(e).__name__)
        return None
    days = int((expires - (now or time.time())) / 86400)
    stamp = time.strftime("%Y-%m-%d", time.gmtime(expires))
    if days < 0:
        log.error("TLS CERTIFICATE HAS EXPIRED (%s)", stamp)
    elif days < warn_days:
        log.warning("TLS CERTIFICATE EXPIRES IN %d DAYS (%s) -- renew now",
                    days, stamp)
    return days


# ----------------------------------------------------------------- routes
def build_router(db, oidc, key, cfg):
    r = Router()

    def _send_static(handler, name):
        """Static files, no-cache (§3). The asset set is tiny and internal,
        so fingerprinting would be complexity without a benefit.

        The router pattern already excludes `/`, but the containment check
        stays: relying on a regex elsewhere in the file to keep this path
        safe is how traversal bugs survive a refactor.
        """
        path = os.path.normpath(os.path.join(STATIC_DIR, name))
        if os.path.commonpath([path, STATIC_DIR]) != STATIC_DIR:
            raise HttpError(404, "not found")
        if not os.path.isfile(path):
            raise HttpError(404, "not found")
        ext = os.path.splitext(path)[1]
        if ext not in MIME:
            raise HttpError(404, "not found")
        with open(path, "rb") as f:
            body = f.read()
        handler._send(200, body, content_type=MIME[ext],
                      extra_headers={"Cache-Control": "no-cache"})

    @r.route("/", role="public")
    def index(handler, user):
        _send_static(handler, "index.html")
        return None

    @r.route("/static/{name}", role="public")
    def static_file(handler, user, name):
        _send_static(handler, name)
        return None

    @r.route("/healthz", role="public")
    def healthz(handler, user):
        report = db.health()
        return (200 if report["ok"] else 503), report

    @r.route("/login", role="public")
    def login(handler, user):
        url, _state = oidc.start()
        handler._send(302, b"", content_type="text/plain",
                      extra_headers={"Location": url})
        return None

    @r.route("/auth/callback", role="public")
    def callback(handler, user):
        from urllib.parse import parse_qs, urlparse
        params = parse_qs(urlparse(handler.path).query)
        code = (params.get("code") or [None])[0]
        state = (params.get("state") or [None])[0]
        if not code or not state:
            raise HttpError(400, "missing code or state")
        try:
            oidc.consume_state(state)
            claims = oidc.claims(oidc.exchange(code))
        except auth.AuthError as e:
            # The reason is logged; the browser is told only that it failed.
            log.warning("sign-in refused: %s", e)
            raise HttpError(403, "sign-in refused")
        u = auth.sign_in(db, claims)
        token = auth.mint_session(key, u["id"], u["token_version"])
        handler._send(302, b"", content_type="text/plain", extra_headers={
            "Location": "/",
            "Set-Cookie": auth.cookie_header(token, cfg.tls)})
        return None

    @r.route("/logout", role="public", method="POST")
    def logout(handler, user):
        handler._send(204, b"", content_type="text/plain",
                      extra_headers={"Set-Cookie": auth.clear_cookie_header()})
        return None

    @r.route("/api/me", role="any")
    def me(handler, user):
        return 200, {"id": user["id"], "email": user["email"],
                     "display_name": user["display_name"],
                     "roles": user["roles"]}

    @r.route("/api/projects", role="viewer")
    def projects(handler, user):
        entity_ids = sorted({r["entity_id"] for r in user["roles"]})
        if not entity_ids:
            return 200, {"projects": []}
        marks = ",".join("?" * len(entity_ids))
        # Column names come from the VIEW (project_id/project_name), not the
        # table. SELECT names are the JSON field names are the JS property
        # names (§5), so the aliases are the API contract.
        return 200, {"projects": db.query(
            f"""SELECT v.project_id   AS id,
                       v.project_name AS name,
                       v.job_code, v.status, v.purchase_order_cents,
                       v.invoiced_prior_cents, v.orders_in_hand_cents,
                       p.needs_resolution
                FROM v_project_orders_in_hand v
                JOIN project p ON p.id = v.project_id
                WHERE v.entity_id IN ({marks})
                ORDER BY v.project_name""", tuple(entity_ids))}

    for module in MODULES:
        module.register(r, db)
    return r


def make_auth_hook(db, key):
    def hook(handler, role):
        try:
            return auth.authorise(db, key, handler, role)
        except auth.AuthError as e:
            raise HttpError(401 if "required" in str(e) or "revoked" in str(e)
                            else 403, str(e))
    return hook


# ------------------------------------------------------------------- boot
def boot(cfg=None, env=None, serve=True):
    setup_logging()
    cfg = cfg or config_mod.from_env(env)
    log.info(json.dumps({"event": "boot", "config": cfg.redacted()}))

    os.environ.setdefault("OPS_SECRETS_PATH", cfg.secrets_path)
    try:
        resolved = resolve_config(cfg.secret_refs(),
                                  provider=build_provider(env))
    except SecretError as e:
        # A service must never start with a blank credential.
        log.error("%s", e)
        raise SystemExit(2)

    db = Db(cfg.db_path, MIGRATIONS)

    # Snapshot BEFORE migrating: every rollback needs a matching file.
    try:
        if os.path.exists(cfg.db_path) and db.scalar(
                "SELECT COUNT(*) FROM sqlite_master") :
            backup.snapshot(db, cfg.backup_dir)
    except Exception as e:
        log.warning("pre-migration snapshot skipped: %s", type(e).__name__)

    applied = db.migrate()
    if applied:
        log.info(json.dumps({"event": "migrated", "versions": applied}))

    key = auth.load_or_create_key(cfg.session_key_path)
    oidc = auth.Oidc(cfg.oidc_client_id, resolved["oidc_client_secret"],
                     cfg.oidc_redirect_uri, cfg.hosted_domain)

    if cfg.tls:
        check_cert_expiry(cfg.tls_cert)

    scheduler = backup.Scheduler(db, cfg.backup_dir, cfg.backup_interval_s,
                                 cfg.backup_keep).start()

    server = make_server(
        (cfg.bind, cfg.effective_port),
        build_router(db, oidc, key, cfg),
        auth_hook=make_auth_hook(db, key),
        tls_enabled=cfg.tls,
        limit=cfg.max_connections,
        read_timeout=cfg.read_timeout)

    if cfg.tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cfg.tls_cert, cfg.tls_key)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        server.socket = ctx.wrap_socket(server.socket, server_side=True)

    log.info(json.dumps({"event": "listening", "port": cfg.effective_port,
                         "tls": cfg.tls}))
    if not serve:
        return db, server, scheduler
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        scheduler.stop()
        server.server_close()
        db.close()


def main(argv=None):
    try:
        boot()
    except SystemExit:
        raise
    except Exception:
        log.exception("boot failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
