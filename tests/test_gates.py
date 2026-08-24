"""CI gates (CS-OP-ARCH-002 §10, §13, §14).

These are tests, not a shell script in ci.yml, so they run on every `make
test` and on Windows too. A gate that only exists in CI is a gate you
discover you have broken after pushing.
"""

import ast
import os
import re
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OPS = os.path.join(ROOT, "ops")
ALLOWLIST = os.path.join(os.path.dirname(__file__), "secret_allowlist.txt")

# Names that, if found holding a literal value, are a build failure.
SECRET_NAMES = ("OIDC_CLIENT_SECRET", "OPS_SECRETS_TOKEN", "CLIENT_SECRET",
                "PASSWORD", "PRIVATE_KEY", "API_KEY", "TOKEN")
PLACEHOLDERS = ("secret://", "CHANGE_ME", "test-secret", "demo-secret", "sekrit")

SCAN_EXT = (".py", ".sql", ".yml", ".yaml", ".md", ".env", ".example",
            ".json", ".html", ".js", ".css")
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", "data",
             "fixtures", ".github"}


def source_files(root=ROOT, exts=SCAN_EXT):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(exts) or name in ("Dockerfile", "Makefile"):
                yield os.path.join(dirpath, name)


def load_allowlist():
    if not os.path.exists(ALLOWLIST):
        return set()
    out = set()
    with open(ALLOWLIST, encoding="utf-8") as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line:
                out.add(line)
    return out


class TestZeroRuntimeDependencies(unittest.TestCase):
    """ADR-08 traded compile-time types for zero dependencies. That trade is
    only worth anything while the count is actually zero."""

    def test_ops_imports_only_stdlib(self):
        stdlib = set(sys.stdlib_module_names)
        offenders = []
        for path in source_files(OPS, (".py",)):
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), path)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:      # relative import, ours by definition
                        continue
                    mods = [(node.module or "").split(".")[0]]
                else:
                    continue
                for m in mods:
                    if m and m not in stdlib and m != "ops":
                        offenders.append(f"{os.path.relpath(path, ROOT)}: {m}")
        self.assertEqual(offenders, [],
                         "non-stdlib import in ops/; a new dependency is an "
                         "ADR, not an import")

    def test_no_runtime_requirements_file(self):
        for name in ("requirements.txt", "Pipfile", "poetry.lock",
                     "pyproject.toml"):
            self.assertFalse(
                os.path.exists(os.path.join(ROOT, name)),
                f"{name} exists; runtime pip deps must stay at zero")

    def test_no_npm(self):
        for name in ("package.json", "package-lock.json", "node_modules"):
            self.assertFalse(os.path.exists(os.path.join(ROOT, name)),
                             f"{name} exists; ZERO npm (§1)")


class TestNoSecretValuesOnFile(unittest.TestCase):
    """§10. References -- never values -- are what files may carry."""

    def test_no_secret_name_holds_a_literal(self):
        allow = load_allowlist()
        pattern = re.compile(
            r"(" + "|".join(SECRET_NAMES) + r")\s*[:=]\s*[\"']([^\"']{6,})[\"']")
        offenders = []
        for path in source_files():
            rel = os.path.relpath(path, ROOT)
            with open(path, encoding="utf-8", errors="replace") as f:
                for n, line in enumerate(f, 1):
                    for name, value in pattern.findall(line):
                        if value in allow or any(p in value for p in PLACEHOLDERS):
                            continue
                        offenders.append(f"{rel}:{n} {name}")
        self.assertEqual(offenders, [], "secret value on file")

    def test_allowlist_has_no_wildcards(self):
        """Each entry is one exact literal with a reason. A wildcard entry is
        a review reject -- it turns the gate off for a whole shape of value
        rather than for one known-safe string."""
        for entry in load_allowlist():
            for meta in ("*", "?", ".*", "[", "]"):
                self.assertNotIn(meta, entry,
                                 f"wildcard in allowlist entry: {entry!r}")


class TestBaseImageIsPinned(unittest.TestCase):
    """§13. An unpinned base means two CI runs of one commit can ship
    different bytes -- and different SQLite versions, against a schema that
    requires >= 3.37."""

    dockerfile = os.path.join(ROOT, "Dockerfile")

    def from_lines(self):
        with open(self.dockerfile, encoding="utf-8") as f:
            return [l.strip() for l in f
                    if l.strip().upper().startswith("FROM ")]

    def test_dockerfile_exists(self):
        self.assertTrue(os.path.exists(self.dockerfile))

    @unittest.skipIf(os.environ.get("OPS_ALLOW_UNPINNED_BASE") == "1",
                     "explicitly allowed for local dev")
    def test_base_image_is_digest_pinned(self):
        """Skipped locally with OPS_ALLOW_UNPINNED_BASE=1; CI never sets it,
        so a release cannot ship on a floating tag.

        Fix:
          docker pull python:3.12-alpine
          docker inspect --format='{{index .RepoDigests 0}}' python:3.12-alpine
        then replace the FROM line with the digest form.
        """
        for line in self.from_lines():
            self.assertIn("@sha256:", line,
                          f"base image is not digest-pinned: {line}")

    def test_no_shell_form_cmd(self):
        """Shell-form CMD puts /bin/sh at PID 1, which does not forward
        SIGTERM -- so every deploy waits out the 10 s kill timeout."""
        with open(self.dockerfile, encoding="utf-8") as f:
            body = f.read()
        for line in body.splitlines():
            s = line.strip()
            if s.upper().startswith("CMD "):
                self.assertTrue(s[4:].strip().startswith("["),
                                "CMD must use exec form")


class TestDevRunnersAgree(unittest.TestCase):
    """The Makefile and dev.ps1 do the same job on two platforms. If they
    disagree about the port, one of them silently produces an OIDC redirect
    URI that will not match what is registered."""

    def test_default_port_matches(self):
        with open(os.path.join(ROOT, "Makefile"), encoding="utf-8") as f:
            mk = re.search(r"^PORT\s*\?=\s*(\d+)", f.read(), re.M)
        with open(os.path.join(ROOT, "dev.ps1"), encoding="utf-8") as f:
            ps = re.search(r"\[int\]\$Port\s*=\s*(\d+)", f.read())
        self.assertIsNotNone(mk)
        self.assertIsNotNone(ps)
        self.assertEqual(mk.group(1), ps.group(1))

    def test_dev_script_never_hardcodes_a_secret_value(self):
        with open(os.path.join(ROOT, "dev.ps1"), encoding="utf-8") as f:
            body = f.read()
        self.assertIn("dev-not-a-real-secret", body)   # placeholder, by name
        self.assertNotIn("GOCSPX", body)


class TestMigrationsAreForwardOnly(unittest.TestCase):
    def test_numbered_and_unique(self):
        d = os.path.join(OPS, "migrations")
        files = sorted(f for f in os.listdir(d) if f.endswith(".sql"))
        self.assertTrue(files)
        seen = set()
        for f in files:
            prefix = f.split("_")[0]
            self.assertTrue(prefix.isdigit(), f"{f} is not numbered")
            self.assertNotIn(prefix, seen, f"duplicate migration number {prefix}")
            seen.add(prefix)

    def test_no_transaction_control_in_migration_files(self):
        """The runner supplies BEGIN/COMMIT; a migration that opens its own
        transaction silently breaks the runner's atomicity, and the failure
        only shows up when a migration fails."""
        d = os.path.join(OPS, "migrations")
        for name in sorted(os.listdir(d)):
            if not name.endswith(".sql"):
                continue
            with open(os.path.join(d, name), encoding="utf-8") as f:
                body = f.read()
            stripped = re.sub(r"--[^\n]*", "", body)
            # BEGIN ... END inside a trigger is fine; a bare BEGIN; is not.
            self.assertIsNone(re.search(r"(?im)^\s*BEGIN\s*;", stripped),
                              f"{name} contains its own BEGIN")
            self.assertIsNone(re.search(r"(?im)^\s*COMMIT\s*;", stripped),
                              f"{name} contains its own COMMIT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
