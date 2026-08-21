"""ops.secrets -- references, providers, store, CLI.

The tests that matter most are the negative ones: no fallback chain, no
values in messages, no values in argv, and a hard boot failure on a missing
reference. Each is a property that is invisible when working and expensive
when not.
"""

import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from ops import secrets  # noqa: E402
from ops.secrets import (LocalProvider, SecretError, build_provider,  # noqa: E402
                         is_ref, resolve_config)

VALUE = "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g"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "secrets", "store.json")
        self.p = LocalProvider(self.path)

    def tearDown(self):
        for root, _dirs, files in os.walk(self.dir, topdown=False):
            for f in files:
                os.unlink(os.path.join(root, f))
            if root != self.dir:
                os.rmdir(root)
        os.rmdir(self.dir)


class TestReferences(unittest.TestCase):
    def test_only_the_scheme_is_a_reference(self):
        self.assertTrue(is_ref("secret://OIDC_CLIENT_SECRET"))
        for plain in ("https://example.com", "OIDC_CLIENT_SECRET", "", None, 42):
            self.assertFalse(is_ref(plain), plain)

    def test_non_references_pass_through_untouched(self):
        cfg = {"OPS_TLS": "on", "PORT": 8443, "NOPE": None}
        self.assertEqual(resolve_config(cfg, provider=LocalProvider("/nope")), cfg)


class TestLocalStore(Base):
    def test_roundtrip(self):
        self.p.set("OIDC_CLIENT_SECRET", VALUE)
        self.assertEqual(self.p.get("OIDC_CLIENT_SECRET"), VALUE)

    def test_list_returns_names_only(self):
        self.p.set("A", VALUE)
        self.p.set("B", "another")
        self.assertEqual(self.p.names(), ["A", "B"])

    @unittest.skipUnless(os.name == "posix", "POSIX file modes")
    def test_store_is_created_0600(self):
        self.p.set("A", VALUE)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes")
    def test_refuses_to_read_a_loose_store(self):
        """A store someone chmod'ed to 0644 is not trustworthy. Failing to
        read is better than serving credentials from a world-readable file."""
        self.p.set("A", VALUE)
        os.chmod(self.path, 0o644)
        with self.assertRaises(SecretError) as e:
            self.p.get("A")
        self.assertIn("0600", str(e.exception))

    @unittest.skipUnless(os.name == "posix", "POSIX file modes")
    def test_never_world_readable_even_briefly(self):
        """Write-then-chmod leaves a window where the value is readable by
        anyone. The temp file must be created 0600 from the outset."""
        self.p.set("A", VALUE)
        tmp = self.path + ".tmp"
        self.assertFalse(os.path.exists(tmp))
        self.p.set("B", VALUE)  # second write goes through the same path
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_missing_secret_names_it_without_a_value(self):
        self.p.set("A", VALUE)
        with self.assertRaises(SecretError) as e:
            self.p.get("MISSING")
        self.assertIn("MISSING", str(e.exception))
        self.assertNotIn(VALUE, str(e.exception))

    def test_refuses_an_empty_value(self):
        """An empty credential is worse than a missing one: it starts, then
        fails at the auth boundary with an opaque error."""
        with self.assertRaises(SecretError):
            self.p.set("A", "")

    def test_corrupt_store_does_not_leak_contents(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            f.write('{"A": "' + VALUE + '"  <<< not json')
        if os.name == "posix":
            os.chmod(self.path, 0o600)
        with self.assertRaises(SecretError) as e:
            self.p.get("A")
        self.assertNotIn(VALUE, str(e.exception))

    def test_delete(self):
        self.p.set("A", VALUE)
        self.p.delete("A")
        self.assertEqual(self.p.names(), [])


class TestProviderSelection(unittest.TestCase):
    def test_defaults_to_local(self):
        self.assertEqual(build_provider({}).name, "local")

    def test_both_remote_vars_selects_remote(self):
        p = build_provider({"OPS_SECRETS_URL": "https://v", "OPS_SECRETS_TOKEN": "t"})
        self.assertEqual(p.name, "remote")

    def test_half_configured_remote_is_a_boot_error_not_a_fallback(self):
        """THE point of the explicit switch. A half-configured remote means
        someone intended remote; quietly serving local secrets instead is the
        worst available answer."""
        for env, missing in (
            ({"OPS_SECRETS_URL": "https://v"}, "OPS_SECRETS_TOKEN"),
            ({"OPS_SECRETS_TOKEN": "t"}, "OPS_SECRETS_URL"),
        ):
            with self.assertRaises(SecretError) as e:
                build_provider(env)
            self.assertIn(missing, str(e.exception))
            self.assertIn("no fallback", str(e.exception))


class TestResolveConfig(Base):
    def test_resolves_references_into_a_new_mapping(self):
        self.p.set("OIDC_CLIENT_SECRET", VALUE)
        cfg = {"OIDC_CLIENT_SECRET": "secret://OIDC_CLIENT_SECRET", "PORT": 8443}
        out = resolve_config(cfg, provider=self.p)
        self.assertEqual(out["OIDC_CLIENT_SECRET"], VALUE)
        self.assertEqual(out["PORT"], 8443)

    def test_original_config_keeps_its_references(self):
        """The original may be logged or republished, so it must never be
        mutated into holding values (§10)."""
        self.p.set("OIDC_CLIENT_SECRET", VALUE)
        cfg = {"OIDC_CLIENT_SECRET": "secret://OIDC_CLIENT_SECRET"}
        resolve_config(cfg, provider=self.p)
        self.assertEqual(cfg["OIDC_CLIENT_SECRET"], "secret://OIDC_CLIENT_SECRET")

    def test_missing_reference_fails_boot(self):
        cfg = {"OIDC_CLIENT_SECRET": "secret://OIDC_CLIENT_SECRET"}
        with self.assertRaises(SecretError) as e:
            resolve_config(cfg, provider=self.p)
        self.assertIn("cannot start", str(e.exception))
        self.assertIn("OIDC_CLIENT_SECRET", str(e.exception))

    def test_reports_every_failure_at_once(self):
        """Boot, fix one secret, boot, fail on the next is a miserable loop."""
        cfg = {"A": "secret://A", "B": "secret://B", "C": "plain"}
        with self.assertRaises(SecretError) as e:
            resolve_config(cfg, provider=self.p)
        self.assertIn("A", str(e.exception))
        self.assertIn("B", str(e.exception))

    def test_failure_message_carries_no_values(self):
        self.p.set("A", VALUE)
        cfg = {"A": "secret://A", "B": "secret://B"}
        with self.assertRaises(SecretError) as e:
            resolve_config(cfg, provider=self.p)
        self.assertNotIn(VALUE, str(e.exception))


class TestCli(Base):
    def run_cli(self, argv, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        old_stdin, old_path = sys.stdin, os.environ.get("OPS_SECRETS_PATH")
        os.environ["OPS_SECRETS_PATH"] = self.path
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = secrets.main(argv)
        finally:
            sys.stdin = old_stdin
            if old_path is None:
                os.environ.pop("OPS_SECRETS_PATH", None)
            else:
                os.environ["OPS_SECRETS_PATH"] = old_path
        return code, out.getvalue(), err.getvalue()

    def test_set_reads_the_value_from_stdin_not_argv(self):
        """argv lands in shell history, the process list, and any `ps` a
        colleague runs while it is in flight."""
        code, out, _ = self.run_cli(["set", "OIDC_CLIENT_SECRET"], stdin=VALUE + "\n")
        self.assertEqual(code, 0)
        self.assertNotIn(VALUE, out)          # confirmation reports length only
        self.assertIn(str(len(VALUE)), out)
        self.assertEqual(LocalProvider(self.path).get("OIDC_CLIENT_SECRET"), VALUE)

    def test_list_prints_names_only(self):
        self.run_cli(["set", "OIDC_CLIENT_SECRET"], stdin=VALUE)
        code, out, _ = self.run_cli(["list"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "OIDC_CLIENT_SECRET")
        self.assertNotIn(VALUE, out)

    def test_empty_stdin_is_refused(self):
        code, _, err = self.run_cli(["set", "A"], stdin="\n")
        self.assertEqual(code, 1)
        self.assertIn("empty", err)

    def test_unknown_command_exits_nonzero(self):
        code, _, _ = self.run_cli(["frobnicate"])
        self.assertEqual(code, 2)

    def test_delete_roundtrip(self):
        self.run_cli(["set", "A"], stdin=VALUE)
        code, _, _ = self.run_cli(["delete", "A"])
        self.assertEqual(code, 0)
        _, out, _ = self.run_cli(["list"])
        self.assertEqual(out.strip(), "")


class TestNoValuesOnFile(Base):
    def test_the_stored_file_is_the_only_place_a_value_exists(self):
        """The store holds values; nothing else may. This mirrors the CI
        grep in §10 and fails loudly if someone adds, say, a debug dump."""
        self.p.set("OIDC_CLIENT_SECRET", VALUE)
        with open(self.path, encoding="utf-8") as f:
            self.assertIn(VALUE, json.load(f)["OIDC_CLIENT_SECRET"])
        src = os.path.join(ROOT, "ops", "secrets.py")
        with open(src, encoding="utf-8") as f:
            body = f.read()
        for leaky in ("print(value", "print(f\"{value", "repr(value", "%s\" % value"):
            self.assertNotIn(leaky, body, f"secrets.py may leak a value: {leaky}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
