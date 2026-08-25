"""OIDC login + identity-only session tokens (CS-OP-ARCH-002 §8, §9).

Two ideas do most of the work here.

**A token is never a bag of permissions.** The payload carries identity and
nothing else: user id, token version, expiry. Roles are resolved from
`user_entity_role` on EVERY request, so a role edit applies on the next
click and revocation is a single UPDATE. No session table, no cleanup job.

**Every claim check fails closed.** An absent claim is a rejection, never an
unchecked pass. This matters most for `hd`: it is the only thing standing
between this system and every Gmail account, so "absent, so skip it" would
be a total authentication bypass.

ID token signatures are deliberately NOT verified. OIDC Core §3.1.3.7
permits this precisely when the token arrives over TLS directly from the
token endpoint, which is the only path here -- so the whole trust budget is
spent on TLS, and `_open()` refuses to run without certificate validation.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets as pysecrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("ops.auth")

ISSUERS = ("https://accounts.google.com", "accounts.google.com")
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
SCOPE = "openid email profile"

SESSION_TTL = 12 * 3600
CLOCK_SKEW = 60
KEY_PATH_DEFAULT = "/data/secrets/session.key"
COOKIE_NAME = "ops_session"


class AuthError(Exception):
    """Never carries a token, a secret, or a claim value.

    Carries its own HTTP status. The caller used to infer one by searching
    the message text for "required" or "revoked", which silently returned
    403 for an expired session -- telling the browser "you are not allowed"
    when the truth was "sign in again". Status is a property of the failure,
    so the failure states it.
    """

    def __init__(self, message, status=401):
        super().__init__(message)
        # 401 authentication: we do not know who you are, or not any more.
        # 403 authorisation:  we know exactly who you are, and no.
        self.status = status


# --------------------------------------------------------------- base64url
def _b64e(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text):
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


# ------------------------------------------------------------ signing key
def load_or_create_key(path=None):
    """32 random bytes, created 0600 via os.open with the mode set at
    creation. Writing then chmod'ing leaves a window where the key is
    world-readable -- brief, but the whole session scheme rests on it."""
    path = path or os.environ.get("OPS_SESSION_KEY", KEY_PATH_DEFAULT)
    if os.path.exists(path):
        if os.name == "posix":
            import stat
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode & 0o077:
                raise AuthError(
                    f"session key at {path} is mode {mode:04o}; expected 0600")
        with open(path, "rb") as f:
            key = f.read()
        if len(key) != 32:
            raise AuthError(f"session key at {path} is not 32 bytes")
        return key
    key = pysecrets.token_bytes(32)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    log.info("generated a new session signing key")
    return key


# --------------------------------------------------------------- sessions
def mint_session(key, user_id, token_version, now=None, ttl=SESSION_TTL):
    now = int(time.time() if now is None else now)
    body = json.dumps(
        {"kind": "session", "sub": int(user_id), "tv": int(token_version),
         "exp": now + ttl},
        separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(key, body, hashlib.sha256).digest()
    return f"{_b64e(body)}.{_b64e(sig)}"


def verify_session(key, token, now=None):
    """Returns the payload, or raises. Signature is checked BEFORE the
    payload is parsed -- parsing attacker-controlled JSON first would mean
    acting on unauthenticated input."""
    now = int(time.time() if now is None else now)
    if not token or token.count(".") != 1:
        raise AuthError("malformed session token")
    body_b64, sig_b64 = token.split(".")
    try:
        body, sig = _b64d(body_b64), _b64d(sig_b64)
    except Exception:
        raise AuthError("malformed session token")
    expected = hmac.new(key, body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):   # constant time
        raise AuthError("bad session signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise AuthError("malformed session payload")
    if payload.get("kind") != "session":
        raise AuthError("wrong token kind")
    if int(payload.get("exp", 0)) <= now:
        raise AuthError("session expired")
    return payload


def cookie_header(token, tls_enabled, ttl=SESSION_TTL):
    parts = [f"{COOKIE_NAME}={token}", "HttpOnly", "SameSite=Lax", "Path=/",
             f"Max-Age={ttl}"]
    if tls_enabled:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie_header():
    return f"{COOKIE_NAME}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"


def read_cookie(header_value):
    for part in (header_value or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME:
            return value
    return None


# ------------------------------------------------------------------ OIDC
def _context(ca_file=None):
    """Certificate validation is non-negotiable. Step 3 of the flow accepts
    an unsigned payload on the strength of TLS alone, so an unverified
    context would leave nothing checking anything."""
    ctx = ssl.create_default_context(cafile=ca_file)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


class Oidc:
    def __init__(self, client_id, client_secret, redirect_uri,
                 hosted_domain, ca_file=None, timeout=10):
        missing = [name for name, value in (
            ("OIDC_CLIENT_ID", client_id),
            ("OIDC_CLIENT_SECRET", client_secret),
            ("OIDC_REDIRECT_URI", redirect_uri)) if not value]
        if missing:
            # Fail loud AND say what is wrong. "Not fully configured" tells
            # whoever is reading the logs at 2am that something is missing
            # and nothing about which thing.
            raise AuthError(
                "OIDC is not fully configured: "
                + ", ".join(missing) + " not set")
        if not hosted_domain:
            raise AuthError("hosted_domain is required; without it any Google "
                            "account could sign in")
        self.client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.hosted_domain = hosted_domain.lower()
        self.ca_file = ca_file
        self.timeout = timeout
        self._states = {}

    # -- step 1 -------------------------------------------------------
    def start(self, now=None):
        """Returns (url, state). State is single use and time-boxed."""
        now = time.time() if now is None else now
        state = pysecrets.token_urlsafe(32)
        self._states[state] = now
        self._expire_states(now)
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "state": state,
            "hd": self.hosted_domain,
            "prompt": "select_account",
        }
        return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}", state

    def _expire_states(self, now, ttl=600):
        for s, born in list(self._states.items()):
            if now - born > ttl:
                del self._states[s]

    def consume_state(self, state, now=None):
        now = time.time() if now is None else now
        self._expire_states(now)
        if state not in self._states:
            raise AuthError("invalid or reused state")
        del self._states[state]          # single use, burned on sight

    # -- step 2 -------------------------------------------------------
    def exchange(self, code, opener=None):
        data = urllib.parse.urlencode({
            "code": code,
            "client_id": self.client_id,
            "client_secret": self._client_secret,
            "redirect_uri": self.redirect_uri,
            "grant_type": "authorization_code",
        }).encode()
        req = urllib.request.Request(
            TOKEN_ENDPOINT, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            if opener is not None:
                body = opener(req)
            else:
                with urllib.request.urlopen(
                        req, timeout=self.timeout,
                        context=_context(self.ca_file)) as r:
                    body = json.load(r)
        except urllib.error.HTTPError as e:
            raise AuthError(f"token endpoint returned HTTP {e.code}")
        except Exception as e:
            raise AuthError(f"token exchange failed ({type(e).__name__})")
        id_token = body.get("id_token")
        if not id_token:
            raise AuthError("token endpoint returned no id_token")
        return id_token

    # -- steps 3 and 4 ------------------------------------------------
    def claims(self, id_token, now=None):
        """Parse the payload and check every claim, ALL FAIL-CLOSED.

        The token is only ever accepted from our own token-endpoint response
        (see exchange). It is never read from the browser, a redirect
        fragment, or a header.
        """
        now = int(time.time() if now is None else now)
        parts = id_token.split(".")
        if len(parts) != 3:
            raise AuthError("malformed id_token")
        try:
            payload = json.loads(_b64d(parts[1]))
        except Exception:
            raise AuthError("malformed id_token payload")

        def need(name):
            value = payload.get(name)
            if value is None or value == "":
                raise AuthError(f"id_token is missing required claim {name!r}")
            return value

        if need("iss") not in ISSUERS:
            raise AuthError("id_token issuer is not Google")
        if need("aud") != self.client_id:
            raise AuthError("id_token audience is not this client")
        if int(need("exp")) + CLOCK_SKEW <= now:
            raise AuthError("id_token has expired")
        if "iat" in payload and int(payload["iat"]) - CLOCK_SKEW > now:
            raise AuthError("id_token is not yet valid")
        # A MISSING hd rejects. This claim is the only thing between this
        # system and every Gmail account in the world.
        if str(need("hd")).lower() != self.hosted_domain:
            raise AuthError("id_token is not from the expected Workspace domain")
        verified = payload.get("email_verified")
        if verified is not True and str(verified).lower() != "true":
            raise AuthError("id_token email is not verified")
        need("sub")
        need("email")
        return payload


# ---------------------------------------------------------- provisioning
def sign_in(db, claims):
    """Identity is keyed on `sub`, NEVER email: Workspace addresses get
    reassigned and renamed, so an email-keyed row hands a departed
    employee's grants to their replacement.

    First sign-in provisions `viewer` with ZERO entity grants (ADR-18). The
    `hd` check proves someone is staff; it says nothing about whether they
    should see money, and shared mailboxes and service accounts all pass it.
    """
    return db.upsert_user(
        claims["sub"],
        claims["email"],
        claims.get("name") or claims["email"])


# --------------------------------------------------- request-time checks
def authorise(db, key, handler, role, now=None):
    """The auth hook wired into http_util. Returns a user dict or raises.

    Roles are read from the database on EVERY request, so a grant made a
    second ago applies without re-login and a revocation bites immediately.
    """
    token = read_cookie(handler.headers.get("Cookie"))
    if not token:
        raise AuthError("authentication required")
    payload = verify_session(key, token, now=now)
    user = db.query_one("SELECT * FROM users WHERE id = ?", (payload["sub"],))
    if user is None or not user["is_active"]:
        raise AuthError("no such user")
    # Instant revocation: one UPDATE invalidates every live session.
    if int(payload["tv"]) != int(user["token_version"]):
        raise AuthError("session revoked")
    user["roles"] = db.roles_for(user["id"])
    if role != "any" and not has_role(user, role):
        # The one genuinely 403 case in this module: identity is established
        # and the answer is still no.
        raise AuthError("insufficient role", status=403)
    return user


ROLES = ("viewer", "operations", "approver", "admin")


def has_role(user, role):
    """NO ROLE IMPLIES ANOTHER (§9). An admin is not automatically an
    approver; approval is a named responsibility, and quietly granting it by
    hierarchy would put someone's name against a decision they never made.
    """
    if role not in ROLES:
        raise AuthError(f"unknown role {role!r}")
    return any(r["role"] == role for r in user.get("roles", []))


def has_role_on(user, role, entity_id):
    return any(r["role"] == role and r["entity_id"] == entity_id
               for r in user.get("roles", []))
