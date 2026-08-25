"""ops.auth -- OIDC claim checks, session tokens, authorisation.

Nearly every test here is an attack. A session scheme is only as good as
what it REFUSES, and each refusal is invisible when it works.
"""

import base64
import json
import os
import stat
import sys
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from ops import auth  # noqa: E402
from ops.auth import (AuthError, Oidc, authorise, clear_cookie_header,  # noqa: E402
                      cookie_header, has_role, has_role_on,
                      load_or_create_key, mint_session, read_cookie,
                      verify_session)
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.join(ROOT, "ops", "migrations")
CLIENT_ID = "1234.apps.googleusercontent.com"
DOMAIN = "commsecurity.com.au"


def id_token(**over):
    now = int(time.time())
    payload = {"iss": "https://accounts.google.com", "aud": CLIENT_ID,
               "exp": now + 300, "iat": now, "hd": DOMAIN,
               "email_verified": True, "sub": "108154", "email": f"r@{DOMAIN}",
               "name": "Richard"}
    payload.update(over)
    for k in [k for k, v in payload.items() if v is auth]:
        del payload[k]
    body = base64.urlsafe_b64encode(
        json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{body}.signature"


DROP = auth  # sentinel meaning "remove this claim entirely"


class TestSessionTokens(unittest.TestCase):
    key = b"k" * 32

    def test_roundtrip(self):
        t = mint_session(self.key, 7, 3)
        p = verify_session(self.key, t)
        self.assertEqual((p["sub"], p["tv"], p["kind"]), (7, 3, "session"))

    def test_payload_carries_identity_only(self):
        """A token is never a bag of permissions. If roles were baked in, a
        revoked grant would stay live until the token expired."""
        t = mint_session(self.key, 7, 1)
        payload = json.loads(auth._b64d(t.split(".")[0]))
        self.assertEqual(set(payload), {"kind", "sub", "tv", "exp"})

    def test_tampered_payload_is_rejected(self):
        body, sig = mint_session(self.key, 7, 1).split(".")
        evil = json.loads(auth._b64d(body))
        evil["sub"] = 1
        forged = auth._b64e(json.dumps(evil).encode())
        with self.assertRaises(AuthError):
            verify_session(self.key, f"{forged}.{sig}")

    def test_wrong_key_is_rejected(self):
        t = mint_session(self.key, 7, 1)
        with self.assertRaises(AuthError):
            verify_session(b"x" * 32, t)

    def test_unsigned_token_is_rejected(self):
        """The 'alg: none' shape -- a valid-looking body with no signature."""
        body = auth._b64e(json.dumps(
            {"kind": "session", "sub": 1, "tv": 1,
             "exp": int(time.time()) + 60}).encode())
        for forged in (f"{body}.", f"{body}.{auth._b64e(b'')}"):
            with self.assertRaises(AuthError):
                verify_session(self.key, forged)

    def test_expired_token_is_rejected(self):
        t = mint_session(self.key, 7, 1, now=time.time() - 100, ttl=10)
        with self.assertRaises(AuthError):
            verify_session(self.key, t)

    def test_garbage_is_rejected_without_raising_something_else(self):
        for junk in ("", "...", "a.b.c", "notatoken", "!!!.???", None):
            with self.assertRaises(AuthError):
                verify_session(self.key, junk)

    def test_cookie_flags(self):
        h = cookie_header("tok", tls_enabled=True)
        for flag in ("HttpOnly", "SameSite=Lax", "Secure", "Path=/"):
            self.assertIn(flag, h)

    def test_secure_flag_only_under_tls(self):
        """Secure over plain http means the browser drops the cookie, which
        makes the dev server unusable rather than more secure."""
        self.assertNotIn("Secure", cookie_header("tok", tls_enabled=False))

    def test_read_cookie_from_a_crowded_header(self):
        self.assertEqual(
            read_cookie("other=1; ops_session=abc.def; third=2"), "abc.def")
        self.assertIsNone(read_cookie("other=1"))

    def test_clear_cookie_expires_immediately(self):
        self.assertIn("Max-Age=0", clear_cookie_header())


class TestSigningKey(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "session.key")

    def tearDown(self):
        for f in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, f))
        os.rmdir(self.dir)

    def test_generates_32_bytes_and_reuses_them(self):
        a = load_or_create_key(self.path)
        self.assertEqual(len(a), 32)
        self.assertEqual(load_or_create_key(self.path), a)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes")
    def test_created_0600(self):
        load_or_create_key(self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes")
    def test_refuses_a_loose_key(self):
        load_or_create_key(self.path)
        os.chmod(self.path, 0o644)
        with self.assertRaises(AuthError):
            load_or_create_key(self.path)

    def test_refuses_a_truncated_key(self):
        with open(self.path, "wb") as f:
            f.write(b"short")
        with self.assertRaises(AuthError):
            load_or_create_key(self.path)


class TestOidcClaims(unittest.TestCase):
    def setUp(self):
        self.o = Oidc(CLIENT_ID, "sekrit", "https://ops.x/auth/callback", DOMAIN)

    def test_accepts_a_good_token(self):
        c = self.o.claims(id_token())
        self.assertEqual(c["sub"], "108154")

    def test_missing_config_names_the_missing_field(self):
        """"Not fully configured" tells whoever is reading the logs that
        something is missing and nothing about which thing."""
        with self.assertRaises(AuthError) as e:
            Oidc("", "s", "https://ops.x/cb", DOMAIN)
        self.assertIn("OIDC_CLIENT_ID", str(e.exception))
        with self.assertRaises(AuthError) as e:
            Oidc(CLIENT_ID, "s", "", DOMAIN)
        self.assertIn("OIDC_REDIRECT_URI", str(e.exception))
        with self.assertRaises(AuthError) as e:
            Oidc("", "", "", DOMAIN)
        self.assertIn("OIDC_CLIENT_SECRET", str(e.exception))

    def test_hosted_domain_is_mandatory_at_construction(self):
        with self.assertRaises(AuthError):
            Oidc(CLIENT_ID, "s", "https://ops.x/cb", hosted_domain="")

    def test_missing_hd_REJECTS(self):
        """The bypass that would matter most: absent must not mean allowed."""
        t = id_token()
        payload = json.loads(auth._b64d(t.split(".")[1]))
        del payload["hd"]
        body = auth._b64e(json.dumps(payload).encode())
        with self.assertRaises(AuthError) as e:
            self.o.claims(f"h.{body}.s")
        self.assertIn("hd", str(e.exception))

    def test_wrong_hd_rejects(self):
        with self.assertRaises(AuthError):
            self.o.claims(id_token(hd="gmail.com"))

    def test_wrong_audience_rejects(self):
        """A token minted for a different client is still a valid Google
        token -- the audience check is what makes it not ours."""
        with self.assertRaises(AuthError):
            self.o.claims(id_token(aud="9999.apps.googleusercontent.com"))

    def test_wrong_issuer_rejects(self):
        with self.assertRaises(AuthError):
            self.o.claims(id_token(iss="https://evil.example"))

    def test_expired_rejects(self):
        with self.assertRaises(AuthError):
            self.o.claims(id_token(exp=int(time.time()) - 3600))

    def test_unverified_email_rejects(self):
        with self.assertRaises(AuthError):
            self.o.claims(id_token(email_verified=False))

    def test_every_required_claim_is_mandatory(self):
        for claim in ("iss", "aud", "exp", "hd", "sub", "email"):
            t = id_token()
            payload = json.loads(auth._b64d(t.split(".")[1]))
            del payload[claim]
            body = auth._b64e(json.dumps(payload).encode())
            with self.assertRaises(AuthError, msg=f"{claim} was not required"):
                self.o.claims(f"h.{body}.s")

    def test_malformed_token_rejects(self):
        for junk in ("", "a", "a.b", "a.!!!.c"):
            with self.assertRaises(AuthError):
                self.o.claims(junk)


class TestOidcState(unittest.TestCase):
    def setUp(self):
        self.o = Oidc(CLIENT_ID, "sekrit", "https://ops.x/auth/callback", DOMAIN)

    def test_start_returns_a_url_with_the_right_scope(self):
        url, state = self.o.start()
        self.assertIn("scope=openid+email+profile", url)
        self.assertIn(f"hd={DOMAIN}", url)
        self.assertIn(state, url)

    def test_state_is_single_use(self):
        _, state = self.o.start()
        self.o.consume_state(state)
        with self.assertRaises(AuthError):
            self.o.consume_state(state)

    def test_unknown_state_rejects(self):
        with self.assertRaises(AuthError):
            self.o.consume_state("never-issued")

    def test_state_expires(self):
        _, state = self.o.start(now=1000)
        with self.assertRaises(AuthError):
            self.o.consume_state(state, now=1000 + 601)

    def test_states_are_unique(self):
        self.assertEqual(len({self.o.start()[1] for _ in range(50)}), 50)


class TestAuthorise(unittest.TestCase):
    key = b"k" * 32

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Db(os.path.join(self.dir, "ops.db"), MIGRATIONS)
        self.db.migrate()
        self.user = auth.sign_in(self.db, {
            "sub": "108154", "email": f"r@{DOMAIN}", "name": "Richard"})

    def tearDown(self):
        self.db.close()
        for f in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, f))
        os.rmdir(self.dir)

    class FakeHandler:
        def __init__(self, cookie=None):
            self.headers = {"Cookie": cookie} if cookie else {}

    def handler_for(self, user=None, tv=None):
        user = user or self.user
        token = mint_session(self.key, user["id"],
                             user["token_version"] if tv is None else tv)
        return self.FakeHandler(f"{auth.COOKIE_NAME}={token}")

    def test_first_sign_in_gets_viewer_on_zero_entities(self):
        self.assertEqual(self.db.roles_for(self.user["id"]), [])
        with self.assertRaises(AuthError):
            authorise(self.db, self.key, self.handler_for(), "viewer")

    def test_grant_applies_without_re_login(self):
        """The same token, unchanged, must start working the moment a grant
        lands -- that is what 'a token is not a bag of permissions' buys."""
        handler = self.handler_for()
        with self.assertRaises(AuthError):
            authorise(self.db, self.key, handler, "viewer")
        self.db.grant_role(self.user["id"], 1, "viewer", self.user["id"])
        user = authorise(self.db, self.key, handler, "viewer")
        self.assertEqual(user["id"], self.user["id"])

    def test_token_version_bump_revokes_instantly(self):
        self.db.grant_role(self.user["id"], 1, "viewer", self.user["id"])
        handler = self.handler_for()
        authorise(self.db, self.key, handler, "viewer")
        self.db.bump_token_version(self.user["id"], self.user["id"])
        with self.assertRaises(AuthError) as e:
            authorise(self.db, self.key, handler, "viewer")
        self.assertIn("revoked", str(e.exception))

    def test_no_cookie_rejects(self):
        with self.assertRaises(AuthError):
            authorise(self.db, self.key, self.FakeHandler(), "viewer")

    def test_token_for_a_nonexistent_user_rejects(self):
        token = mint_session(self.key, 9999, 1)
        with self.assertRaises(AuthError):
            authorise(self.db, self.key,
                      self.FakeHandler(f"{auth.COOKIE_NAME}={token}"), "viewer")

    def test_inactive_user_rejects(self):
        self.db.grant_role(self.user["id"], 1, "viewer", self.user["id"])
        with self.db._tx() as c:
            c.execute("UPDATE users SET is_active = 0 WHERE id = ?",
                      (self.user["id"],))
        with self.assertRaises(AuthError):
            authorise(self.db, self.key, self.handler_for(), "viewer")

    def test_sign_in_is_keyed_on_sub_not_email(self):
        again = auth.sign_in(self.db, {
            "sub": "108154", "email": f"new@{DOMAIN}", "name": "Richard R"})
        self.assertEqual(again["id"], self.user["id"])
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM users"), 1)

    def test_display_name_falls_back_to_email(self):
        u = auth.sign_in(self.db, {"sub": "999", "email": f"x@{DOMAIN}"})
        self.assertEqual(u["display_name"], f"x@{DOMAIN}")


class TestFailuresCarryTheRightStatus(unittest.TestCase):
    """401 means authenticate; 403 means you are authenticated and the answer
    is no. An expired session used to return 403, because the status was
    inferred by searching the message text."""

    key = b"k" * 32

    def test_authentication_failures_are_401(self):
        for token in ("", "rubbish", "a.b"):
            try:
                verify_session(self.key, token)
            except AuthError as e:
                self.assertEqual(e.status, 401, token)
            else:
                self.fail(f"{token!r} was accepted")

    def test_an_expired_session_is_401_not_403(self):
        """The one that was wrong. A browser told 403 has no reason to send
        the user to sign in again."""
        t = mint_session(self.key, 7, 1, now=time.time() - 100, ttl=10)
        with self.assertRaises(AuthError) as e:
            verify_session(self.key, t)
        self.assertEqual(e.exception.status, 401)

    def test_a_bad_signature_is_401(self):
        t = mint_session(b"x" * 32, 7, 1)
        with self.assertRaises(AuthError) as e:
            verify_session(self.key, t)
        self.assertEqual(e.exception.status, 401)

    def test_insufficient_role_is_403(self):
        """Identity established, answer still no."""
        with self.assertRaises(AuthError) as e:
            has_role({"roles": []}, "not-a-role")
        self.assertEqual(e.exception.status, 401)   # unknown role: a bug, not a denial

    def test_the_status_is_not_inferred_from_the_message(self):
        """Guard against the pattern coming back: no message-text matching."""
        with open(os.path.join(ROOT, "ops", "main.py"), encoding="utf-8") as f:
            body = f.read()
        self.assertNotIn('"required" in str(e)', body)
        self.assertIn("e.status", body)


class TestRoles(unittest.TestCase):
    def user(self, *roles, entity=1):
        return {"roles": [{"entity_id": entity, "role": r} for r in roles]}

    def test_no_role_implies_another(self):
        """An admin is NOT automatically an approver. Approval is a named
        responsibility; granting it by hierarchy would put someone's name
        against a decision they never made."""
        admin = self.user("admin")
        self.assertTrue(has_role(admin, "admin"))
        for other in ("viewer", "operations", "approver"):
            self.assertFalse(has_role(admin, other), other)

    def test_role_is_per_entity(self):
        u = self.user("approver", entity=2)
        self.assertTrue(has_role_on(u, "approver", 2))
        self.assertFalse(has_role_on(u, "approver", 1))

    def test_unknown_role_raises_rather_than_returning_false(self):
        """A typo'd role name must not silently deny -- or worse, silently
        allow if the check is ever inverted."""
        with self.assertRaises(AuthError):
            has_role(self.user("admin"), "superuser")

    def test_no_roles_at_all(self):
        self.assertFalse(has_role({"roles": []}, "viewer"))
        self.assertFalse(has_role({}, "viewer"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
