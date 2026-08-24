"""Mint a local session so the UI can be used before OIDC is registered.

    python3 tools/dev_session.py --data ./data

DEV ONLY, and enforced rather than trusted: this refuses to run unless TLS
is explicitly off, because a tool that mints a valid session for an
arbitrary user is exactly what you do not want sitting next to a production
volume. It also refuses if the hostname it is pointed at is not local.

It creates (or reuses) a user, grants viewer + admin on entity 1, and prints
the cookie to paste into the browser. It does NOT bypass authentication --
the cookie it prints is an ordinary HMAC session that `auth.verify_session`
validates like any other, so nothing in the auth path is stubbed or
weakened for development.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ops import auth  # noqa: E402
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "ops", "migrations"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", default="./data")
    ap.add_argument("--email", default="dev@commsecurity.com.au")
    ap.add_argument("--name", default="Dev User")
    ap.add_argument("--sub", default="dev-local-000")
    ap.add_argument("--port", default="8080")
    args = ap.parse_args(argv)

    if os.environ.get("OPS_TLS", "off").lower() not in ("off", "0", "false", "no"):
        print("REFUSED: dev_session only runs with OPS_TLS=off.", file=sys.stderr)
        return 2

    db_path = os.path.join(args.data, "ops.db")
    if not os.path.exists(db_path):
        print(f"REFUSED: {db_path} does not exist. Seed it first.", file=sys.stderr)
        return 2

    db = Db(db_path, MIGRATIONS)
    try:
        user = db.upsert_user(args.sub, args.email, args.name)
        # Both roles, deliberately: no role implies another (§9), so an
        # admin who is not also a viewer cannot read the project list -- as
        # the restore rehearsal discovered the hard way.
        for role in ("viewer", "admin"):
            db.grant_role(user["id"], 1, role, user["id"])
        key = auth.load_or_create_key(
            os.path.join(args.data, "secrets", "session.key"))
        token = auth.mint_session(key, user["id"], user["token_version"])
        roles = ", ".join(sorted(r["role"] for r in db.roles_for(user["id"])))
    finally:
        db.close()

    print(f"""
  user     {args.name} <{args.email}>
  roles    {roles} on entity 1

  In the browser at http://localhost:{args.port}, open DevTools (F12) and run:

    document.cookie = "{auth.COOKIE_NAME}={token}; path=/"

  Then reload. The cookie is an ordinary signed session and expires in 12
  hours; run this again for a new one.
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
