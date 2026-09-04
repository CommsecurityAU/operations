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


class TestStaticDirectoryHoldsAssetsOnly(unittest.TestCase):
    """`ops/static/` is published: every file in it is reachable at
    /static/<name>. A source file there is always a mistake, and catching it
    should not depend on someone reading `git status` carefully -- one was
    committed and only spotted by eye."""

    ALLOWED = (".html", ".css", ".js", ".svg", ".ico", ".png", ".woff2")

    def test_no_source_or_data_files_under_static(self):
        static = os.path.join(OPS, "static")
        offenders = []
        for dirpath, dirnames, filenames in os.walk(static):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                if not name.endswith(self.ALLOWED):
                    offenders.append(os.path.relpath(
                        os.path.join(dirpath, name), ROOT))
        self.assertEqual(offenders, [],
                         "non-asset file in the published static directory")

    def test_the_server_would_refuse_to_serve_one_anyway(self):
        """Defence in depth: even if a stray file lands, the MIME allowlist
        in main.py is what stops it being readable over HTTP."""
        with open(os.path.join(OPS, "main.py"), encoding="utf-8") as f:
            body = f.read()
        self.assertIn("if ext not in MIME:", body)
        self.assertNotIn('".py"', body.split("MIME = {")[1].split("}")[0])


class TestOneListOfRoles(unittest.TestCase):
    """The roles are written down in three places: the CHECK constraint on
    `user_entity_role`, `auth.ROLES`, and the list the access module
    offers. Adding `finance` to two of the three produced `unknown role
    'finance'` from a route that had already been granted it -- a 401 for a
    user who was, by every other account, authorised."""

    def test_auth_and_the_access_module_agree(self):
        from ops import auth
        from ops.modules import access
        self.assertEqual(sorted(auth.ROLES), sorted(access.ROLES))

    def test_the_schema_agrees(self):
        # The NEWEST migration carrying the CHECK, not a fixed filename:
        # adding a role rebuilds the table again, and pinning `020` here
        # would make this gate assert against a schema two roles out of
        # date.
        folder = os.path.join(ROOT, "ops", "migrations")
        sql = ""
        for name in sorted(os.listdir(folder), reverse=True):
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                body = f.read()
            if "CHECK (role IN" in body:
                sql = body
                break
        self.assertTrue(sql, "no migration defines the role CHECK")
        found = re.search(r"role\s+TEXT\s+NOT NULL CHECK \(role IN \(([^)]*)\)",
                          sql, re.S)
        self.assertIsNotNone(found, "no CHECK on role in the newest rebuild")
        from ops import auth
        in_schema = sorted(
            part.strip().strip("'") for part in found.group(1).split(","))
        self.assertEqual(in_schema, sorted(auth.ROLES))


class TestNothingIsAcceptedAndIgnored(unittest.TestCase):
    """A field the API accepts and the database drops is worse than one it
    refuses: the screen says it worked.

    It happened twice. `project_id` on a procurement line -- moving a cost
    to the right job appeared to succeed and did nothing. Then
    `threshold_annual_cents` on an expense line, the same week, in the
    module written to avoid it.
    """

    def test_a_procurement_line_writes_what_the_api_takes(self):
        from ops.db import Db
        from ops.modules import procurement
        offered = set(procurement.DATE_FIELDS) | {
            "project_id", "supplier_id", "supplier_po_id",
            "supplier_quote_id", "supplier_invoice_id", "period_id",
            "item", "description", "note", "quantity", "currency",
            "unit_cost_cents", "total_cents", "cancel_reason",
            "stated_state", "is_estimate"}
        self.assertEqual(sorted(offered - set(Db.LINE_MUTABLE)), [])

    def test_an_expense_line_writes_what_the_api_takes(self):
        from ops.db import Db
        offered = {"category_id", "name", "state", "is_forecast", "rate_bp",
                   "threshold_annual_cents", "note", "is_active"}
        self.assertEqual(sorted(offered - set(Db.EXPENSE_LINE_MUTABLE)), [])



class TestCiRunsTheSuiteOnce(unittest.TestCase):
    """The wall-time gate used to run the whole suite a SECOND time purely
    to measure it, so every build paid for two runs and the number
    described a run nobody looked at.

    A performance budget that costs the thing it measures is a budget
    working against itself.
    """

    def workflow(self):
        path = os.path.join(ROOT, ".github", "workflows", "ci.yml")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_the_test_job_runs_the_suite_once(self):
        body = self.workflow()
        # Up to the N-1 job, which legitimately runs it again against the
        # previous release.
        head = body.split("  n1:")[0]
        runs = [l for l in head.splitlines()
                if "unittest discover -s tests" in l]
        self.assertEqual(len(runs), 1,
                         f"the test job runs the suite {len(runs)} times")

    def test_the_budget_says_what_to_do_instead_of_raising_it(self):
        """A budget nobody knows how to meet gets raised. This one names
        the lever."""
        body = self.workflow()
        self.assertIn("Do NOT raise this number", body)

    def test_ci_switches_the_schema_cache_on(self):
        """Without it the suite is two and a half times longer, and the
        budget is set for the fast path. The switch is an environment
        variable because the obvious hook -- `tests/__init__.py` -- is
        never imported: `unittest discover` puts the test directory on
        `sys.path` and imports the modules top-level."""
        self.assertIn("OPS_SCHEMA_CACHE", self.workflow())

    def test_the_dev_script_switches_it_on_too(self):
        """So a local run and a CI run measure the same thing."""
        path = os.path.join(ROOT, "dev.ps1")
        with open(path, encoding="utf-8") as f:
            self.assertIn("OPS_SCHEMA_CACHE", f.read())


class TestAMigrationNeedsNoParticularRunner(unittest.TestCase):
    """Applied by a PLAIN runner: one transaction, foreign keys on, no view
    preservation, no `.nofk` handling.

    Migration `024` relied on the runner dropping and restoring views, and
    the N-1 gate refused it -- the previous release's runner has no such
    feature. The gate was right about something bigger than itself: **a
    migration that only works with one version of the runner is a hidden
    coupling**, and the version that has to apply it during a rollback is
    the one that does not have the feature.
    """

    def test_a_plain_runner_can_apply_every_migration(self):
        import shutil
        import sqlite3
        import tempfile
        folder = tempfile.mkdtemp()
        try:
            conn = sqlite3.connect(os.path.join(folder, "o.db"))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations "
                         "(version TEXT PRIMARY KEY, applied_ts INTEGER)")
            migrations = os.path.join(ROOT, "ops", "migrations")
            names = sorted(n for n in os.listdir(migrations)
                           if n.endswith(".sql"))
            for name in names:
                with open(os.path.join(migrations, name),
                          encoding="utf-8") as f:
                    sql = f.read()
                try:
                    conn.executescript(f"BEGIN;\n{sql}\nCOMMIT;")
                except Exception as e:
                    conn.rollback()
                    self.fail(f"{name} needs a runner feature: {e}")
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual(broken, [], "references do not resolve")
            conn.close()
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_a_rebuild_recreates_every_view_it_drops(self):
        """A view of a view breaks when its dependency goes.
        `v_upcoming_renewals` never mentions `project` and broke anyway,
        because it reads `v_schedule_coverage` which does."""
        folder = os.path.join(ROOT, "ops", "migrations")
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".nofk.sql"):
                continue
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                body = f.read()
            dropped = set(re.findall(r"DROP VIEW IF EXISTS (\w+)", body))
            created = set(re.findall(r"CREATE VIEW (\w+)", body))
            self.assertEqual(sorted(dropped - created), [],
                             f"{name}: drops views it never recreates")


class TestARebuildKeepsWhatItRebuilt(unittest.TestCase):
    """Recreating a table is three chances to lose something quietly.

    Writing migration `024` from memory dropped `project_no`,
    `needs_resolution`, `notes` and `source_row`; invented a UNIQUE index on
    the job code that would have refused data the platform already holds;
    and would have dropped the ADR-22 CHECK that a project cannot have been
    invoiced for more than its contract. Each was caught by a test, but only
    because the tests happened to cover it.

    So the rebuild is checked against what it replaced.
    """

    def schema(self, upto=None):
        import shutil
        import tempfile
        sys.path.insert(0, ROOT)
        from ops.db import Db
        folder = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(folder, "m"))
            for name in sorted(os.listdir(
                    os.path.join(ROOT, "ops", "migrations"))):
                if upto and name >= upto:
                    continue
                shutil.copy(
                    os.path.join(ROOT, "ops", "migrations", name),
                    os.path.join(folder, "m", name))
            db = Db(os.path.join(folder, "o.db"), os.path.join(folder, "m"))
            db.migrate()
            columns = {r["name"] for r in
                       db.query("PRAGMA table_info(project)")}
            indexes = {(r["name"], r["sql"]) for r in db.query(
                """SELECT name, sql FROM sqlite_master WHERE type = 'index'
                   AND tbl_name = 'project' AND sql IS NOT NULL""")}
            triggers = {r["name"] for r in db.query(
                """SELECT name FROM sqlite_master WHERE type = 'trigger'
                   AND tbl_name = 'project'""")}
            table = db.scalar(
                """SELECT sql FROM sqlite_master WHERE type = 'table'
                   AND name = 'project'""")
            views = {r["name"] for r in db.query(
                "SELECT name FROM sqlite_master WHERE type = 'view'")}
            db.close()
            return columns, indexes, triggers, table, views
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_the_rebuild_keeps_every_column(self):
        before, _i, _t, _s, _v = self.schema(upto="024")
        after, _i2, _t2, _s2, _v2 = self.schema()
        self.assertEqual(sorted(before - after), [])

    def test_it_keeps_every_index(self):
        """Compared on WHAT IS INDEXED, not on the name: `project_status`
        was legitimately renamed to `project_status_idx` because the lookup
        table took the old name. Comparing names would fail on a rename
        that changes nothing, and pass on an index that quietly lost a
        column."""
        def columns(indexes):
            return sorted(
                sql[sql.index("("):].replace(" ", "")
                for _name, sql in indexes)
        _c, before, _t, _s, _v = self.schema(upto="024")
        _c2, after, _t2, _s2, _v2 = self.schema()
        self.assertEqual(columns(before), columns(after))

    def test_it_does_not_tighten_a_constraint(self):
        """A rebuild that made the job code unique would refuse data the
        platform already holds: `Brennan Pl` has an implementation and a
        licence sharing `JN-6980`, deliberately, with the worklist tracking
        it."""
        _c, before, _t, _s, _v = self.schema(upto="024")
        _c2, after, _t2, _s2, _v2 = self.schema()
        was_unique = {n for n, q in before if "UNIQUE" in q.upper()}
        now_unique = {n for n, q in after if "UNIQUE" in q.upper()}
        self.assertEqual(sorted(now_unique - was_unique), [])

    def test_it_keeps_every_trigger(self):
        _c, _i, before, _s, _v = self.schema(upto="024")
        _c2, _i2, after, _s2, _v2 = self.schema()
        self.assertEqual(sorted(before - after), [])

    def test_it_keeps_every_view(self):
        """The runner drops them all and puts them back. If one went
        missing, everything reading it would fail at query time rather than
        at migrate time."""
        _c, _i, _t, _s, before = self.schema(upto="024")
        _c2, _i2, _t2, _s2, after = self.schema()
        self.assertEqual(sorted(before - after), [])

    def test_it_keeps_the_invoiced_cannot_exceed_contract_check(self):
        """ADR-22, and the easiest kind of thing to lose in a rebuild: a
        constraint nobody notices until data arrives that breaks it."""
        _c, _i, _t, table, _v = self.schema()
        self.assertIn("invoiced_prior_cents <= purchase_order_cents", table)


class TestTheDeploymentPinsWhatItRuns(unittest.TestCase):
    """`latest` moves on every push to main.

    On 3 September it moved from a build that had been tested to one that
    had not, within the hour, over a `.gitignore` commit that changed
    nothing in the image. A restart six weeks from now must bring back the
    bytes that were running, not whatever has been merged since.
    """

    def compose(self):
        path = os.path.join(ROOT, "docker-compose.yml")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_the_image_is_pinned_or_pinnable(self):
        """Raven-Fleet rewrites `repo:tag` to a digest when it creates the
        release (CS-OP-RUN-002 §1), so the file in git may carry a tag --
        the release still means exactly those bytes forever. What it may
        NOT carry: no tag at all (an implicit `latest` nobody chose), a
        registry CI does not push to, or a second image line the fleet
        manager would rewrite as well."""
        lines = [l.strip() for l in self.compose().splitlines()
                 if l.strip().startswith("image:")]
        self.assertEqual(len(lines), 1, "expected exactly one image line")
        ref = lines[0].split(":", 1)[1].strip()
        # The bare repository path, no registry: Raven-Fleet resolves it
        # against its own mirror (release 3 ran as
        # 100.64.0.1:5000/commsecurityau/cs-ops@sha256:...). A registry
        # prefix here is one more thing the fleet manager has to strip.
        self.assertTrue(ref.startswith("commsecurityau/cs-ops"),
                        "expected the bare repository path, got %r" % ref)
        self.assertRegex(ref, r"^commsecurityau/cs-ops(@sha256:[0-9a-f]{64}|:[A-Za-z0-9._-]+)$",
                         "the image needs an explicit tag or digest")

    DATA_HOST_PATH = "/var/lib/cs-ops"

    def test_the_data_volume_survives_the_next_release(self):
        """Raven-Fleet runs the compose file from a staging directory that
        is wiped on supersede. A relative bind mount puts the database in
        that directory: root-owned on the first deploy (`unable to open
        database file`), gone on the second. The mount must be an absolute
        host path, and the one the off-box sync, deploy.sh and the release's
        host-privilege grant all name."""
        body = self.compose()
        mounts = [l.strip() for l in body.splitlines()
                  if l.strip().startswith("- ") and ":/data" in l]
        self.assertEqual(mounts, ["- %s:/data" % self.DATA_HOST_PATH], mounts)

    def test_every_tool_agrees_on_where_the_data_lives(self):
        """Three scripts with three defaults is how a backup silently copies
        an empty directory."""
        for name in ("deploy.sh", "offbox_sync.sh"):
            with open(os.path.join(ROOT, "tools", name), encoding="utf-8") as f:
                self.assertIn('OPS_DATA:-%s}' % self.DATA_HOST_PATH, f.read(), name)

    def test_no_secret_value_is_in_the_compose_file(self):
        """The compose file is in git. Every secret-bearing variable must
        be a `${...}` reference filled from the generated .env, never a
        literal -- and the .env itself must be ignored, or the literal just
        moves one file over."""
        body = self.compose()
        self.assertNotIn("GOCSPX-", body)
        self.assertNotIn("-----BEGIN", body)
        for name in ("OIDC_CLIENT_SECRET", "OPS_TLS_KEY", "OPS_TLS_CERT"):
            lines = [l.strip() for l in body.splitlines()
                     if l.strip().startswith(name + ":")]
            self.assertEqual(len(lines), 1, name)
            self.assertRegex(lines[0], r"^%s: \$\{%s(:[?-])?\}$" % (name, name),
                             "%s must be a ${...} reference" % name)
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            ignored = f.read().splitlines()
        self.assertIn(".env", ignored)

    def test_the_deploy_script_checks_before_it_stops_anything(self):
        """A deployment that takes the service down and THEN finds the
        certificate missing has turned an upgrade into an outage."""
        path = os.path.join(ROOT, "tools", "deploy.sh")
        with open(path, encoding="utf-8") as f:
            body = f.read()
        checks = body.index("preflight")
        swap = body.index("compose up -d") if "compose up -d" in body \
            else body.index("up -d")
        self.assertLess(checks, swap,
                        "the preflight runs after the swap")
        for needed in ("server.crt", "server.key", "store.json",
                       "checkend", "--rollback"):
            self.assertIn(needed, body, f"deploy.sh does not check {needed}")


class TestNoBackupScriptCopiesTheLiveDatabase(unittest.TestCase):
    """A WAL database copied mid-transaction yields a `.db` and a `-wal`
    that disagree, and the copy fails only at RESTORE — on the day you
    need it.

    Both sync scripts copy `backups/` and `documents/` and nothing else.
    The snapshots come from `VACUUM INTO` and are consistent by
    construction; the blobs are immutable. This checks the absence, because
    the absence is the whole design and a well-meant line adding `ops.db`
    would look like an improvement.
    """

    def scripts(self):
        folder = os.path.join(ROOT, "tools")
        for name in ("offbox_sync.sh", "offbox_sync.ps1"):
            path = os.path.join(folder, name)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    yield name, f.read()

    def test_the_windows_script_does_not_depend_on_the_working_directory(self):
        """A scheduled task runs from `system32`.

        The first version defaulted `-Source` to the relative path `data`,
        which resolved to `C:\\Windows\\system32\\data` under the scheduler
        and failed every hour while the task showed as Ready. **A backup
        that never runs is worse than no backup, because it is believed.**
        """
        path = os.path.join(ROOT, "tools", "offbox_sync.ps1")
        with open(path, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("$PSScriptRoot", body,
                      "source path is not resolved from the script location")
        self.assertNotIn('$Source = "data"', body,
                         "a relative default resolves against the caller's "
                         "working directory")

    def test_both_scripts_exist(self):
        """The VM has one and the laptop has the other. The laptop is where
        the only copy currently lives."""
        self.assertEqual(sorted(n for n, _b in self.scripts()),
                         ["offbox_sync.ps1", "offbox_sync.sh"])

    def test_none_of_them_copies_the_live_database(self):
        for name, body in self.scripts():
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("<#"):
                    continue
                if re.search(r"\bops\.db\b", stripped) \
                        and ("rsync" in stripped or "robocopy" in stripped
                             or "Copy-Item" in stripped):
                    self.fail(f"{name}: copies the live database: {stripped}")

    def test_they_copy_only_backups_and_documents(self):
        for name, body in self.scripts():
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # An INVOCATION, not a mention. `command -v rsync` checks
                # the tool is installed and copies nothing -- flagging it
                # would be a check that cries wolf on its own script.
                if not re.match(r"^(rsync|robocopy|\$rc = robocopy)\b",
                                stripped):
                    continue
                self.assertTrue(
                    "backups" in stripped or "documents" in stripped,
                    f"{name}: copies something else: {stripped}")


class TestFilesAreWhereTheyBelong(unittest.TestCase):
    """Misplaced files have cost real time twice: `ops/static/main.py` (the
    app entrypoint copied into the PUBLISHED asset directory) and
    `tools/test_sync_register.py` (a test beside the tool it tests, which
    breaks discovery outright with an error naming neither file usefully).

    Both came from placing individually-downloaded files by hand. Catching
    it should not depend on someone reading a path table carefully.
    """

    #: Files that legitimately live at the repo root.
    ROOT_ALLOWED = {"dev.ps1", "Makefile", "Dockerfile", "check.py",
                    ".dockerignore", ".gitattributes", ".gitignore",
                    "pyrightconfig.json",
                    # Machine-specific and gitignored: a real OIDC client
                    # id, a different port. Belongs at the root because
                    # dev.ps1 dot-sources it from there.
                    "dev.local.ps1",
                    # `docker compose` looks for this beside itself and
                    # nowhere else, so the root is where it goes.
                    "docker-compose.yml",
                    # Written by tools/deploy.sh to record the digest to
                    # roll back to. Gitignored.
                    ".deploy-previous-digest",
                    # The local environment file for `--env-file`.
                    # Gitignored; the secret lives here rather than on a
                    # command line.
                    ".env.local"}

    def test_nothing_stray_at_the_repo_root(self):
        """Loose files copied to the root instead of into `ops/` or
        `tests/`. It happened with a flat download and robocopy: nine files
        landed beside the folders they belonged in, the working tree looked
        unchanged, and `test_*.py` at the root would have been collected as
        well."""
        strays = [
            name for name in os.listdir(ROOT)
            if os.path.isfile(os.path.join(ROOT, name))
            and name not in self.ROOT_ALLOWED
            and not name.endswith(".md")]
        self.assertEqual(sorted(strays), [],
                         "these belong in ops/, ops/static/, tools/ or tests/ "
                         "-- or are throwaway scripts to delete")

    def test_no_test_files_outside_the_tests_directory(self):
        strays = []
        for folder in ("ops", "tools"):
            for dirpath, dirnames, filenames in os.walk(
                    os.path.join(ROOT, folder)):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for name in filenames:
                    if name.startswith("test_") and name.endswith(".py"):
                        strays.append(os.path.relpath(
                            os.path.join(dirpath, name), ROOT))
        self.assertEqual(strays, [],
                         "test files belong in tests/ -- a copy elsewhere "
                         "breaks unittest discovery for the whole suite")

    def test_no_tool_or_module_files_inside_tests(self):
        """The mirror image: a tool copied into tests/ is imported as a test
        module and fails obscurely."""
        strays = [
            name for name in os.listdir(os.path.join(ROOT, "tests"))
            if name.endswith(".py") and not name.startswith("test_")
            and name != "__init__.py"]
        self.assertEqual(strays, [])


class TestDevSessionGrantsEveryRole(unittest.TestCase):
    """No role implies another, so a dev session missing one shows up as a
    button that is not there, with nothing on screen explaining why. That
    cost a round trip once already."""

    def test_dev_session_grants_all_four(self):
        with open(os.path.join(ROOT, "tools", "dev_session.py"),
                  encoding="utf-8") as f:
            body = f.read()
        for role in ("viewer", "operations", "approver", "admin"):
            self.assertIn(f'"{role}"', body, f"dev_session omits {role}")

    def test_the_roles_the_ui_gates_on_are_all_grantable(self):
        """If a screen checks a role, the dev tool has to be able to grant
        it -- otherwise the feature is untestable locally."""
        with open(os.path.join(ROOT, "ops", "static", "projects.js"),
                  encoding="utf-8") as f:
            ui = f.read()
        with open(os.path.join(ROOT, "tools", "dev_session.py"),
                  encoding="utf-8") as f:
            dev = f.read()
        for m in re.finditer(r'roles\.has\("(\w+)"\)', ui):
            self.assertIn(f'"{m.group(1)}"', dev,
                          f"UI gates on {m.group(1)} but dev_session cannot grant it")


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
