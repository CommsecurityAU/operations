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

import base64
import binascii
import hashlib
import json
import logging
import os
import ssl
import sys
import time

from ops import auth, backup, config as config_mod
from ops.modules import claims as claims_module
from ops.modules import procurement as procurement_module
from ops.modules import projects as projects_module
from ops.modules import access as access_module
from ops.modules import claimplan as claimplan_module
from ops.modules import dashboard as dashboard_module
from ops.modules import expenses as expenses_module
from ops.modules import schedules as schedules_module
from ops.modules import worklist as worklist_module
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
    ".png": "image/png",
}
MODULES = [projects_module, worklist_module, claims_module,
           schedules_module, claimplan_module, access_module,
           procurement_module, expenses_module, dashboard_module]   # §6. Explicit, in order.
CERT_WARN_DAYS = 30


def code_fingerprint(package_dir=None):
    """Short hash of the Python and SQL this process would load.

    Python imports a module once and never re-reads it, so a running server
    can be several edits behind the working tree while looking entirely
    healthy -- every test passes, the browser disagrees, and nothing says
    why. Comparing this value on disk against the one `/healthz` reports
    answers "is the server running what I just wrote" in one request.

    Deliberately NOT including static/: those files are read per request, so
    the RUNNING SERVER is never stale with respect to them. See
    `asset_fingerprint` for the disk, which is a different question and the
    one that actually caught someone out.
    """
    root = package_dir or os.path.dirname(__file__)
    digest = hashlib.sha256()
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if name.endswith((".py", ".sql")):
                paths.append(os.path.join(dirpath, name))
    for path in sorted(paths):
        digest.update(os.path.relpath(path, root).replace("\\", "/").encode())
        with open(path, "rb") as f:
            digest.update(f.read())
    return digest.hexdigest()[:12]


def asset_fingerprint(static_dir=None):
    """Short hash of everything served from static/.

    `code_fingerprint` answers "is the running server the code on disk".
    This answers a different question -- "is the disk what was delivered" --
    and that gap cost a round trip: `-Stale` reported current while
    claims.js was two versions behind, because Python code was up to date
    and the fingerprint only covered Python.
    """
    root = static_dir or os.path.join(os.path.dirname(__file__), "static")
    digest = hashlib.sha256()
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        paths.extend(os.path.join(dirpath, n) for n in filenames)
    for path in sorted(paths):
        digest.update(os.path.relpath(path, root).replace("\\", "/").encode())
        with open(path, "rb") as f:
            digest.update(f.read())
    return digest.hexdigest()[:12]


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


def _decode_pem(label, value):
    """Base64 of a PEM file, or -- because someone will paste the file
    itself -- the PEM text as-is. Anything else names the variable."""
    if value.lstrip().startswith("-----BEGIN"):
        return value.encode()
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        data = b""
    if not data.lstrip().startswith(b"-----BEGIN"):
        log.error(
            "%s is set but is not base64 of a PEM file. Produce it with: "
            "base64 -w0 server.%s", label, "crt" if "CERT" in label else "key")
        raise SystemExit(2)
    return data


def materialise_tls_from_env(cfg):
    """Write environment-delivered TLS material to data/tls/ before the
    file checks run, so a release carries its own certificate instead of
    depending on a pair someone copied onto the volume by hand.

    Both or neither: half a pair is a mistake, not a fallback, and it must
    say so rather than silently use a stale key from the volume alongside
    a new certificate.
    """
    if not cfg.tls_cert_b64 and not cfg.tls_key_b64:
        return False
    if not (cfg.tls_cert_b64 and cfg.tls_key_b64):
        missing = "OPS_TLS_KEY" if cfg.tls_cert_b64 else "OPS_TLS_CERT"
        log.error("OPS_TLS_CERT and OPS_TLS_KEY must be set together; "
                  "%s is missing.", missing)
        raise SystemExit(2)
    cert = _decode_pem("OPS_TLS_CERT", cfg.tls_cert_b64)
    key = _decode_pem("OPS_TLS_KEY", cfg.tls_key_b64)

    tls_dir = os.path.dirname(cfg.tls_cert)
    os.makedirs(tls_dir, mode=0o700, exist_ok=True)
    for path, data, mode in ((cfg.tls_cert, cert, 0o644),
                             (cfg.tls_key, key, 0o600)):
        # Write-then-rename: a crash mid-write leaves the old pair intact
        # rather than an empty key the next boot refuses.
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    log.info(json.dumps({"event": "tls_material", "source": "env",
                         "path": tls_dir}))
    return True


def verify_data_dir(cfg):
    """/data must exist and be writable by THIS process before anything
    touches it. Otherwise the first thing to touch it is sqlite, and it
    says `unable to open database file` with no path, no uid and no fix --
    which is what the first deploy failed with, twice.

    The usual cause: /data is bind-mounted from a host directory that was
    created root-owned (`mkdir -p`), and the container runs as a non-root
    user that cannot write there.
    """
    uid = os.getuid() if hasattr(os, "getuid") else None
    gid = os.getgid() if hasattr(os, "getgid") else None
    who = f"uid {uid}, gid {gid}" if uid is not None else "the app user"
    fix = (f"sudo chown -R {uid}:{gid} <host directory> && "
           f"sudo chmod 750 <host directory>") if uid is not None else \
        "chown the host directory to the app user"

    if not os.path.isdir(cfg.data_dir):
        log.error(
            "%s does not exist or is not a directory. It is bind-mounted "
            "from the host (see docker-compose.yml); create it there, owned "
            "by %s, before the first boot: sudo install -d -o %s -g %s -m 750 "
            "<host directory>", cfg.data_dir, who, uid, gid)
        raise SystemExit(2)

    probe = os.path.join(cfg.data_dir, ".write-probe")
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        os.unlink(probe)
    except PermissionError:
        try:
            st = os.stat(cfg.data_dir)
            owner = f"owned by uid {st.st_uid}, gid {st.st_gid}, mode {oct(st.st_mode & 0o777)}"
        except OSError:
            owner = "owner unreadable"
        log.error(
            "%s is not writable by the app (%s; the directory is %s). It is "
            "bind-mounted from the host, so fix it there: %s",
            cfg.data_dir, who, owner, fix)
        raise SystemExit(2)
    except FileExistsError:
        os.unlink(probe)


def verify_tls_material(cfg):
    """Fail loudly, and say which file and what to do about it.

    Without this, the three ways a first deploy goes wrong all surface as
    `FileNotFoundError: [Errno 2] No such file or directory` with no path,
    or as a raw OpenSSL string. Those are the messages someone reads at 2am
    while the service is down, so they have to name the file and the fix.

    Exits 2 rather than raising, matching the secrets path: non-zero =
    unhealthy = automatic rollback, so a misconfigured deploy self-reports
    instead of half-starting.
    """
    for label, path in (("certificate", cfg.tls_cert), ("private key", cfg.tls_key)):
        if not os.path.exists(path):
            log.error(
                "TLS is on but there is no %s at %s. Issue one from the "
                "internal CA and place it there, or set OPS_TLS=off for "
                "local development.", label, path)
            raise SystemExit(2)
        try:
            with open(path, "rb") as f:
                f.read(1)
        except PermissionError:
            # The container runs as the non-root `ops` user. A key copied in
            # as root with mode 600 is unreadable to it, and this is the
            # most likely way a first deploy fails.
            log.error(
                "TLS %s at %s cannot be read. The app runs as the non-root "
                "'ops' user, so the file must be readable by it: "
                "chown ops:ops %s && chmod 640 %s", label, path, path, path)
            raise SystemExit(2)

    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cfg.tls_cert, cfg.tls_key)
    except ssl.SSLError as e:
        log.error(
            "TLS certificate and key at %s do not go together (%s). They "
            "are probably from different issuances -- reissue both from the "
            "internal CA as a pair.", os.path.dirname(cfg.tls_cert), e)
        raise SystemExit(2)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


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
    r = Router(session_key=key)

    def _send_static(handler, name):
        """Static files, no-store (§3). The asset set is tiny and internal,
        so fingerprinting the filenames would be complexity without a
        benefit.

        `no-store`, not `no-cache`: the latter permits STORING and merely
        requires revalidation, and with no ETag or Last-Modified there is
        nothing to revalidate against -- an ambiguous state browsers resolve
        differently. It cost a round trip, with a module correct on disk and
        an old one still running in the tab.

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
                      extra_headers={"Cache-Control": "no-store"})

    @r.route("/", role="public")
    def index(handler, user):
        _send_static(handler, "index.html")
        return None

    @r.route("/static/{name}", role="public")
    def static_file(handler, user, name):
        _send_static(handler, name)
        return None

    # Computed ONCE, at router construction, so it describes the code this
    # process actually loaded -- not what is on disk right now.
    running_code = code_fingerprint()

    @r.route("/healthz", role="public")
    def healthz(handler, user):
        report = db.health()
        report["code"] = running_code
        # Read fresh: static files can change under a running server, which
        # is the whole reason they are not in `code`.
        report["assets"] = asset_fingerprint()
        report["release"] = os.environ.get("OPS_RELEASE") or None
        return (200 if report["ok"] else 503), report

    @r.route("/login", role="public")
    def login(handler, user):
        url, _state = oidc.start()
        handler._send(302, b"", content_type="text/plain",
                      extra_headers={"Location": url})
        return None

    @r.route("/auth/elevate", role="viewer")
    def elevate(handler, user):
        """Re-authenticate, to see something that costs something.

        There is no password in this system -- sign-in is Google -- so
        demanding one means demanding a FRESH Google authentication.
        `prompt=login` makes Google ask again rather than waving through
        the live session.
        """
        url, _state = oidc.start(force_login=True)
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
            kind = oidc.consume_state(state)
            claims = oidc.claims(oidc.exchange(code))
        except auth.AuthError as e:
            # The reason is logged; the browser is told only that it failed.
            log.warning("sign-in refused: %s", e)
            raise HttpError(403, "sign-in refused")
        u = auth.sign_in(db, claims)
        token = auth.mint_session(key, u["id"], u["token_version"])
        # A SECOND cookie with its own short life when this was an
        # elevation. Two `Set-Cookie` headers need two values, and joining
        # them with a comma would send one malformed cookie rather than two
        # good ones -- so the sender takes a list here.
        cookies: list[str] = [auth.cookie_header(token, cfg.tls)]
        if kind == "elevate":
            cookies.append(auth.elevation_cookie_header(
                auth.mint_elevation(key, u["id"]), cfg.tls))
        headers: dict[str, str | list[str]] = {
            "Location": "/#expenses" if kind == "elevate" else "/",
            "Set-Cookie": cookies,
        }
        handler._send(302, b"", content_type="text/plain",
                      extra_headers=headers)
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

    for module in MODULES:
        module.register(r, db)
    return r


def make_auth_hook(db, key):
    def hook(handler, role):
        try:
            return auth.authorise(db, key, handler, role)
        except auth.AuthError as e:
            # The error states its own status. Inferring one from the message
            # text returned 403 for an expired session, which tells a browser
            # "not allowed" when the truth is "sign in again".
            raise HttpError(e.status, str(e))
    return hook


# ------------------------------------------------------------------- boot
def boot(cfg=None, env=None, serve=True):
    setup_logging()
    cfg = cfg or config_mod.from_env(env)
    log.info(json.dumps({"event": "boot", "code": code_fingerprint(),
                         "release": os.environ.get("OPS_RELEASE") or None,
                         "config": cfg.redacted()}))

    os.environ.setdefault("OPS_SECRETS_PATH", cfg.secrets_path)
    try:
        resolved = resolve_config(cfg.secret_refs(),
                                  provider=build_provider(env))
    except SecretError as e:
        # A service must never start with a blank credential.
        log.error("%s", e)
        raise SystemExit(2)

    verify_data_dir(cfg)
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

    tls_context = None
    if cfg.tls:
        # Before the server is built, so a bad certificate fails the deploy
        # rather than a request.
        materialise_tls_from_env(cfg)
        tls_context = verify_tls_material(cfg)
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

    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)

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
    argv = sys.argv[1:] if argv is None else argv
    if "--fingerprint" in argv:
        # Prints the ON-DISK fingerprint. Compare with the `code` field from
        # /healthz: if they differ, the server is stale, restart it.
        print(code_fingerprint())
        return 0
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
