"""Secrets. NO VALUES ON FILE (CS-OP-ARCH-002 §10).

`secret://NAME` is a REFERENCE. Any other string passes through unchanged.
References -- never values -- are what config, env vars, release manifests,
logs and API responses may carry.

Two rules that shape everything here:

1. An unresolvable reference FAILS BOOT LOUDLY. A service must never start
   with a blank credential, because the failure then surfaces later as a
   confusing auth error rather than a refusal to start. Boot failure = failed
   health gate = automatic rollback, so a missing secret self-reports (§13).
2. Providers are selected EXPLICITLY, never by fallback chain. A chain means
   that when the intended provider is misconfigured, something else quietly
   answers -- and you find out from the audit log.

Nothing here ever logs, prints or reprs a value. Errors name the secret only.

CLI:
    docker exec ops python -m ops.secrets set OIDC_CLIENT_SECRET   # stdin
    docker exec ops python -m ops.secrets list                     # names only
"""

import json
import os
import stat
import sys
import urllib.error
import urllib.request

PREFIX = "secret://"
DEFAULT_STORE = "/data/secrets/store.json"
MODE = 0o600


class SecretError(Exception):
    """Names a secret, never carries a value."""


def is_ref(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def ref_name(value):
    return value[len(PREFIX):]


# ------------------------------------------------------------------ local
class LocalProvider:
    """JSON map on the data volume, 0600, app user."""

    name = "local"

    def __init__(self, path=None):
        self.path = path or os.environ.get("OPS_SECRETS_PATH", DEFAULT_STORE)

    def _load(self):
        if not os.path.exists(self.path):
            return {}
        self._check_mode()
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            # Never interpolate the file contents into the message.
            raise SecretError(f"secret store at {self.path} is unreadable: {type(e).__name__}")
        if not isinstance(data, dict):
            raise SecretError(f"secret store at {self.path} is not a JSON object")
        return data

    def _check_mode(self):
        """POSIX only. On Windows this is a no-op BY DESIGN, and says so --
        the dev store works, but the permission guarantee exists only in the
        container. Silently skipping the check would imply a protection that
        is not there.
        """
        if os.name != "posix":
            return
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        if mode & 0o077:
            raise SecretError(
                f"secret store at {self.path} is mode {mode:04o}; expected 0600. "
                "Refusing to read a store that is group- or world-readable."
            )

    def get(self, name):
        value = self._load().get(name)
        if value is None:
            raise SecretError(f"secret {name!r} is not in the local store")
        return value

    def names(self):
        return sorted(self._load())

    def set(self, name, value):
        if not name or not name.strip():
            raise SecretError("secret name must not be empty")
        if value == "":
            raise SecretError(f"refusing to store an empty value for {name!r}")
        data = self._load()
        data[name] = value
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        # Create with 0600 from the outset via os.open -- writing then
        # chmod'ing leaves a window where the value is world-readable.
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, MODE)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
        except Exception:
            os.unlink(tmp)
            raise
        os.replace(tmp, self.path)
        if os.name == "posix":
            os.chmod(self.path, MODE)

    def delete(self, name):
        data = self._load()
        if name not in data:
            raise SecretError(f"secret {name!r} is not in the local store")
        del data[name]
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)


# ----------------------------------------------------------------- remote
class RemoteProvider:
    """Dormant option. Moving to a central secrets service later is an env
    change and zero code."""

    name = "remote"

    def __init__(self, url, token, ca_file=None, timeout=5):
        self.url = url.rstrip("/")
        self._token = token
        self.ca_file = ca_file
        self.timeout = timeout

    def get(self, name):
        import ssl
        req = urllib.request.Request(
            f"{self.url}/secret/{name}",
            headers={"Authorization": f"Bearer {self._token}"})
        ctx = ssl.create_default_context(cafile=self.ca_file)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            raise SecretError(f"secret {name!r}: provider returned HTTP {e.code}")
        except Exception as e:
            raise SecretError(f"secret {name!r}: provider unreachable ({type(e).__name__})")
        value = body.get("value")
        if not value:
            raise SecretError(f"secret {name!r}: provider returned no value")
        return value

    def names(self):
        raise SecretError("remote provider does not enumerate secrets")


def build_provider(env=None):
    """EXPLICIT selection. Setting one of the remote pair without the other
    is a boot error, not a silent fall back to local -- a half-configured
    remote means someone intended remote, and quietly serving local secrets
    instead is the worst possible answer.
    """
    env = os.environ if env is None else env
    url, token = env.get("OPS_SECRETS_URL"), env.get("OPS_SECRETS_TOKEN")
    if bool(url) != bool(token):
        missing = "OPS_SECRETS_TOKEN" if url else "OPS_SECRETS_URL"
        raise SecretError(
            f"remote secrets provider is half-configured: {missing} is not set. "
            "Set both or neither; there is no fallback to local."
        )
    if url:
        return RemoteProvider(url, token, env.get("OPS_SECRETS_CA"))
    return LocalProvider(env.get("OPS_SECRETS_PATH"))


def resolve_config(config, provider=None):
    """Resolve every `secret://` reference in a config mapping, ONCE at
    startup, into a NEW dict. The original keeps its references, because it
    may be logged or republished (§10).

    Collects every failure before raising: booting, failing on one missing
    secret, being fixed, then failing on the next is a miserable loop.
    """
    provider = provider or build_provider()
    resolved, failures = {}, []
    for key, value in config.items():
        if not is_ref(value):
            resolved[key] = value
            continue
        name = ref_name(value)
        try:
            resolved[key] = provider.get(name)
        except SecretError as e:
            failures.append(f"{key}: {e}")
    if failures:
        raise SecretError(
            "cannot start: unresolved secret references via "
            f"{provider.name} provider:\n  - " + "\n  - ".join(failures))
    return resolved


# -------------------------------------------------------------------- CLI
def _cmd_set(argv):
    if len(argv) != 1:
        print("usage: python -m ops.secrets set NAME   (value on stdin)",
              file=sys.stderr)
        return 2
    name = argv[0]
    if sys.stdin.isatty():
        print(f"Reading value for {name} from stdin; end with Ctrl-D.",
              file=sys.stderr)
    # Value comes from STDIN, never argv -- argv lands in shell history, the
    # process list, and any `ps` a colleague runs while it is in flight.
    value = sys.stdin.read().strip()
    try:
        LocalProvider().set(name, value)
    except SecretError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"stored {name} ({len(value)} chars)")  # length, never the value
    return 0


def _cmd_list(argv):
    try:
        for name in LocalProvider().names():
            print(name)
    except SecretError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _cmd_delete(argv):
    if len(argv) != 1:
        print("usage: python -m ops.secrets delete NAME", file=sys.stderr)
        return 2
    try:
        LocalProvider().delete(argv[0])
    except SecretError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"deleted {argv[0]}")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmds = {"set": _cmd_set, "list": _cmd_list, "delete": _cmd_delete}
    if not argv or argv[0] not in cmds:
        print("usage: python -m ops.secrets {set NAME|list|delete NAME}",
              file=sys.stderr)
        return 2
    return cmds[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
