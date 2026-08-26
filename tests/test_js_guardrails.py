"""Frontend guardrails (CS-OP-ARCH-002 §7).

Pure-Python static checks over `ops/static/`. Written as tests so they run
on `make test` and on Windows, not only in CI.

Each of these guards a property that is invisible while it holds and
expensive once it does not. The `innerHTML` ban is the sharpest: there is no
blessed exception anywhere, including inside `h()`, so the check is a flat
grep that cannot be argued with. A rule with one exception becomes a rule
with three.
"""

import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATIC = os.path.join(ROOT, "ops", "static")

JS_PAGE_BUDGET = 50 * 1024      # uncompressed, per page


def static_files(exts=(".js", ".css", ".html")):
    for name in sorted(os.listdir(STATIC)):
        if name.endswith(exts):
            yield name, os.path.join(STATIC, name)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def code_only(text):
    """Source with comments blanked out, line numbering preserved.

    The checks below scan CODE. Scanning comments too would mean the rules
    could not be explained in the files they govern -- the comment in app.js
    saying `innerHTML` appears nowhere would itself trip the `innerHTML`
    check. Blanking rather than deleting keeps line numbers honest so a
    failure still points at the right line.
    """
    out, i, n = [], 0, len(text)
    while i < n:
        two = text[i:i + 2]
        if two == "/*":
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append("".join("\n" if c == "\n" else " " for c in text[i:end]))
            i = end
        elif text[i:i + 4] == "<!--":
            end = text.find("-->", i + 4)
            end = n if end == -1 else end + 3
            out.append("".join("\n" if c == "\n" else " " for c in text[i:end]))
            i = end
        elif two == "//" and text[i - 1:i] != ":":     # not a URL scheme
            end = text.find("\n", i)
            end = n if end == -1 else end
            out.append(" " * (end - i))
            i = end
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


class TestNoInnerHtml(unittest.TestCase):
    def test_innerhtml_appears_nowhere(self):
        """Not in components, not in h(), not anywhere. h() builds elements
        with createElement/textContent, so a project called `<script>`
        renders as those nine characters."""
        offenders = []
        for name, path in static_files((".js", ".html")):
            for n, line in enumerate(code_only(read(path)).splitlines(), 1):
                if "innerHTML" in line or "outerHTML" in line:
                    offenders.append(f"{name}:{n}")
        self.assertEqual(offenders, [])

    def test_no_other_html_injection_sinks(self):
        for sink in ("insertAdjacentHTML", "document.write", "eval("):
            for name, path in static_files((".js", ".html")):
                self.assertNotIn(sink, code_only(read(path)), f"{sink} in {name}")


class TestFetchIsCentralised(unittest.TestCase):
    def test_fetch_appears_only_inside_api(self):
        """One network call site means one place that attaches credentials,
        one place that raises on non-2xx, and one place to change when any
        of that changes."""
        offenders = []
        for name, path in static_files((".js",)):
            for n, line in enumerate(code_only(read(path)).splitlines(), 1):
                if re.search(r"\bfetch\s*\(", line) and name != "app.js":
                    offenders.append(f"{name}:{n}")
        self.assertEqual(offenders, [])

    def test_app_js_has_exactly_one_fetch(self):
        hits = re.findall(r"\bfetch\s*\(",
                          code_only(read(os.path.join(STATIC, "app.js"))))
        self.assertEqual(len(hits), 1)

    def test_no_xmlhttprequest(self):
        for name, path in static_files((".js",)):
            self.assertNotIn("XMLHttpRequest", code_only(read(path)), name)


class TestNoExternalCode(unittest.TestCase):
    def test_no_cdn_or_external_urls(self):
        """ZERO npm, no build step, no CDN. An external script tag is also a
        third party who can change what your finance system executes."""
        pattern = re.compile(r"""(src|href|from|import)\s*[=(]?\s*["']https?://""")
        offenders = []
        for name, path in static_files():
            for n, line in enumerate(code_only(read(path)).splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{name}:{n}")
        self.assertEqual(offenders, [])

    def test_imports_are_relative(self):
        for name, path in static_files((".js",)):
            for m in re.finditer(r"""from\s+["']([^"']+)["']""",
                                 code_only(read(path))):
                self.assertTrue(m.group(1).startswith("./"),
                                f"{name}: non-relative import {m.group(1)!r}")


class TestOneMoneyConversion(unittest.TestCase):
    """The browser has ONE money conversion, in app.js, mirroring
    ops/money.py being the one on the server.

    Two places that convert money is two places to disagree, and they always
    eventually do -- usually one of them truncating while the other rounds.
    """

    def test_no_other_file_converts_between_cents_and_dollars(self):
        offenders = []
        for name, path in static_files((".js",)):
            if name == "app.js":
                continue
            body = code_only(read(path))
            for n, line in enumerate(body.splitlines(), 1):
                if re.search(r"[/*]\s*100\b", line):
                    offenders.append(f"{name}:{n} {line.strip()[:60]}")
        self.assertEqual(offenders, [])

    def test_app_js_exports_the_conversion_for_others_to_use(self):
        body = code_only(read(os.path.join(STATIC, "app.js")))
        self.assertIn("export function toCents", body)
        self.assertIn("export function moneyInput", body)

    def test_money_parsing_goes_through_strings_not_arithmetic(self):
        """19.99 * 100 is 1998.9999999999998 at double precision."""
        body = code_only(read(os.path.join(STATIC, "app.js")))
        self.assertIn('.split(".")', body)


class TestTablesUseTheScreen(unittest.TestCase):
    """A table with eight columns on a wide screen should use the screen.

    A fixed `max-width` on the content block forced a sideways scroll to
    read columns that would otherwise have fitted -- and the scroll was the
    only symptom, so it read as "the table is too wide" rather than "the
    page is artificially narrow"."""

    def test_the_content_block_is_not_capped(self):
        css = code_only(read(os.path.join(STATIC, "base.css")))
        block = css.split(".content")[1].split("}")[0]
        self.assertNotIn("max-width", block)

    def test_horizontal_scrolling_is_still_available_for_narrow_screens(self):
        """Removing the cap must not remove the fallback: on a phone the
        table genuinely does not fit, and scrolling beats crushing it."""
        css = code_only(read(os.path.join(STATIC, "base.css")))
        self.assertIn("overflow-x: auto", css)

    def test_free_text_columns_are_truncated_not_left_to_run(self):
        """One long detail used to push every column after it off screen."""
        css = code_only(read(os.path.join(STATIC, "base.css")))
        self.assertIn("text-overflow: ellipsis", css)

    def test_a_truncated_cell_keeps_its_full_value_on_hover(self):
        """Narrowing a column must not lose information."""
        body = code_only(read(os.path.join(STATIC, "datatable.js")))
        self.assertIn("title", body)


def static_imports(name):
    """Which modules a file pulls in with a STATIC import.

    Dynamic `import()` is deliberately not followed: that is the mechanism
    that keeps a screen off every other screen's page.
    """
    body = code_only(read(os.path.join(STATIC, name)))
    return set(re.findall(r'^\s*import[^;]*?from\s+"\./([\w.]+\.js)"',
                          body, re.M))


def bundle(entry, seen=None):
    """Everything the browser must fetch to run `entry`."""
    seen = seen if seen is not None else set()
    if entry in seen:
        return seen
    seen.add(entry)
    for dep in static_imports(entry):
        bundle(dep, seen)
    return seen


class TestFilterValueOrder(unittest.TestCase):
    """A list of months in alphabetical order is a list nobody can scan.

    `Apr-27, Aug-26, Dec-26, Feb-27` is technically sorted and practically
    useless, and it is what you get by default because the values are
    strings.
    """

    def test_filter_values_sort_by_the_column_sort_key(self):
        """Scoped to the multiselect function, not the whole file.

        A first version of this test looked for `col.sortKey` anywhere in
        datatable.js -- which the header-sort code also contains, so
        breaking the FILTER order left the test green. Mutation-testing it
        is what showed that; a guardrail nobody has tried to break is a
        guarantee nobody has checked.
        """
        body = code_only(read(os.path.join(STATIC, "datatable.js")))
        start = body.index("function multiselect")
        fn = body[start:body.index("const filterEls", start)]
        self.assertIn("col.sortKey", fn)
        self.assertNotIn("]).sort()", fn)

    def test_a_column_may_sort_on_a_different_field_than_it_shows(self):
        """`Sep-26` reads well and sorts alphabetically; the month sorts on
        its start date instead."""
        body = code_only(read(os.path.join(STATIC, "datatable.js")))
        self.assertIn("col.sortKey || col.key", body)

    def test_the_month_column_declares_one(self):
        body = code_only(read(os.path.join(STATIC, "claims.js")))
        self.assertIn('sortKey: "month_start"', body)


class TestCsvExport(unittest.TestCase):
    def test_it_exports_what_is_visible_not_the_whole_table(self):
        """Handing someone a file that does not match the figures they were
        just looking at is worse than not offering the download."""
        body = code_only(read(os.path.join(STATIC, "datatable.js")))
        self.assertIn("toCsv(visible())", body)

    def test_money_leaves_as_a_number(self):
        """`$1,234.56` arrives in Excel as text that will not sum."""
        body = code_only(read(os.path.join(STATIC, "datatable.js")))
        self.assertIn("fmt.plain", body)
        self.assertNotIn("fmt.money(raw)", body)

    def test_the_cents_conversion_still_lives_only_in_app_js(self):
        """fmt.plain divides by 100; it belongs with the other money code,
        not scattered into whatever needed it."""
        body = code_only(read(os.path.join(STATIC, "app.js")))
        self.assertIn("plain(cents)", body)

    def test_values_containing_commas_and_quotes_are_escaped(self):
        """A project named `Smith, Jones & Co` would otherwise shift every
        column after it by one -- silently, in a file someone reconciles."""
        body = code_only(read(os.path.join(STATIC, "datatable.js")))
        self.assertIn('replace(/"/g', body)


class TestBudgets(unittest.TestCase):
    """The budget is what ONE PAGE weighs, not what the folder weighs.

    Screens are loaded dynamically, so the register does not pay for the
    invoicing grid it never shows. If that stops being true the sum creeps
    back and this gate has to catch it, which is why it walks the import
    graph rather than adding up files.
    """

    SHELL = "main.js"
    SCREENS = ("projects.js", "claims.js", "worklist.js")

    def size(self, names):
        return sum(os.path.getsize(os.path.join(STATIC, n)) for n in names)

    def test_each_js_file_is_under_the_page_budget(self):
        for name, path in static_files((".js",)):
            self.assertLess(os.path.getsize(path), JS_PAGE_BUDGET, name)

    def test_the_heaviest_page_is_under_the_budget(self):
        shell = bundle(self.SHELL)
        worst, worst_name = 0, None
        for screen in self.SCREENS:
            weight = self.size(shell | bundle(screen))
            if weight > worst:
                worst, worst_name = weight, screen
        self.assertLess(worst, JS_PAGE_BUDGET,
                        f"{worst_name} page is {worst} bytes")

    def test_screens_are_loaded_dynamically_not_statically(self):
        """The mechanism the budget depends on. A static import of a screen
        in the shell puts it on every page again, and the only symptom would
        be a slow first paint that nobody attributes to this."""
        shell_deps = static_imports(self.SHELL)
        for screen in self.SCREENS:
            self.assertNotIn(screen, shell_deps)
        body = code_only(read(os.path.join(STATIC, self.SHELL)))
        for screen in self.SCREENS:
            self.assertIn(f'import("./{screen}")', body)

    def test_no_screen_imports_another_screen(self):
        """Otherwise opening one drags in a second, and the graph stops
        describing what a page costs."""
        for screen in self.SCREENS:
            for other in self.SCREENS:
                if other != screen:
                    self.assertNotIn(other, static_imports(screen))


class TestTokensAreTheOnlySourceOfColour(unittest.TestCase):
    def test_no_colour_literals_outside_tokens_css(self):
        """A literal in a component is a review reject: it is the value that
        gets missed when the palette changes, and it will not be found by
        looking at tokens.css."""
        hexes = re.compile(r"#[0-9a-fA-F]{3,8}\b")
        funcs = re.compile(r"\b(rgb|rgba|hsl|hsla)\s*\(")
        offenders = []
        for name, path in static_files((".css",)):
            if name == "tokens.css":
                continue
            for n, line in enumerate(code_only(read(path)).splitlines(), 1):
                if hexes.search(line) or funcs.search(line):
                    offenders.append(f"{name}:{n}")
        self.assertEqual(offenders, [])

    def test_no_font_family_literals_outside_tokens_css(self):
        for name, path in static_files((".css",)):
            if name == "tokens.css":
                continue
            for n, line in enumerate(code_only(read(path)).splitlines(), 1):
                if "font-family" in line:
                    self.assertIn("var(--font-", line, f"{name}:{n}")

    def test_money_uses_the_data_face_with_tabular_numerals(self):
        """Money in a proportional face does not line up, and a column of
        figures that does not line up cannot be scanned."""
        base = read(os.path.join(STATIC, "base.css"))
        self.assertIn("font-variant-numeric: tabular-nums", base)
        self.assertIn("var(--font-data)", base)

    def test_geometry_is_flat_and_square(self):
        """§7: 0 radius, 1px borders, no shadows. Elevation is a different
        design language and mixing the two reads as accident."""
        for name, path in static_files((".css",)):
            if name == "tokens.css":
                continue
            body = code_only(read(path))
            for n, line in enumerate(body.splitlines(), 1):
                if "box-shadow" in line and "inset" not in line:
                    self.fail(f"{name}:{n} drop shadow")
                if "border-radius" in line:
                    self.assertIn("var(--radius)", line, f"{name}:{n}")


class TestAccessibilityFloor(unittest.TestCase):
    def test_focus_is_visible(self):
        self.assertIn(":focus-visible", read(os.path.join(STATIC, "base.css")))

    def test_reduced_motion_respected(self):
        self.assertIn("prefers-reduced-motion",
                      read(os.path.join(STATIC, "tokens.css")))

    def test_html_declares_a_language(self):
        self.assertRegex(read(os.path.join(STATIC, "index.html")), r"<html[^>]+lang=")

    def test_every_input_gets_an_accessible_name(self):
        """A control with no accessible name is unusable with a screen
        reader and ambiguous without one. Each `h("input"` must either
        carry aria-label or sit inside an h("label", ...) wrapper."""
        body = code_only(read(os.path.join(STATIC, "datatable.js")))
        for m in re.finditer(r'h\("input"', body):
            window = body[max(0, m.start() - 200):m.start() + 300]
            self.assertTrue(
                "aria-label" in window or 'h("label"' in window,
                f"unlabelled input near offset {m.start()}")

    def test_the_filter_popup_is_announced(self):
        """A button that opens a panel has to say so, and say whether it is
        currently open, or keyboard users get a button that appears to do
        nothing."""
        body = code_only(read(os.path.join(STATIC, "datatable.js")))
        for attr in ('"aria-haspopup"', '"aria-expanded"',
                     'role: "group"', '"aria-label"'):
            self.assertIn(attr, body, f"filter popup missing {attr}")

    def test_the_popup_closes_on_escape(self):
        """Anything that opens over the page must close from the keyboard."""
        body = code_only(read(os.path.join(STATIC, "datatable.js")))
        self.assertIn('"Escape"', body)

    def test_sortable_headers_announce_their_state(self):
        body = code_only(read(os.path.join(STATIC, "datatable.js")))
        self.assertIn('"aria-sort"', body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
