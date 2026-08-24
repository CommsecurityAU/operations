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


class TestBudgets(unittest.TestCase):
    def test_each_js_file_is_under_the_page_budget(self):
        for name, path in static_files((".js",)):
            self.assertLess(os.path.getsize(path), JS_PAGE_BUDGET, name)

    def test_total_js_is_under_the_page_budget(self):
        """Every module loads on every page today, so the page weight is the
        sum, not the largest file."""
        total = sum(os.path.getsize(p) for _n, p in static_files((".js",)))
        self.assertLess(total, JS_PAGE_BUDGET, f"{total} bytes of JS")


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
