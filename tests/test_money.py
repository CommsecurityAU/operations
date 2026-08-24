"""ops.money -- ADR-15, half away from zero, integers only.

The mode only shows itself at the half-way boundary, which is exactly where
it is never tested by accident. Everything here targets that boundary.
"""

import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from ops import money  # noqa: E402


class TestRoundingMode(unittest.TestCase):
    def test_half_rounds_away_from_zero(self):
        """The whole of ADR-15 in four assertions. Python's own round() is
        banker's and would give 0, 2, 0, -2 for these."""
        self.assertEqual(money.divide(1, 2), 1)      # 0.5 -> 1
        self.assertEqual(money.divide(3, 2), 2)      # 1.5 -> 2
        self.assertEqual(money.divide(-1, 2), -1)    # -0.5 -> -1
        self.assertEqual(money.divide(-3, 2), -2)    # -1.5 -> -2

    def test_it_is_not_bankers_rounding(self):
        """Stated as its own test because 'round half to even' is what you
        get by reaching for round() without thinking."""
        self.assertNotEqual(money.divide(1, 2), round(0.5))
        self.assertEqual(money.divide(1, 2), 1)
        self.assertEqual(round(0.5), 0)

    def test_it_is_not_truncation(self):
        self.assertEqual(money.divide(7, 2), 4)      # not 3
        self.assertEqual(money.divide(-7, 2), -4)    # not -3

    def test_below_and_above_the_boundary(self):
        self.assertEqual(money.divide(4, 10), 0)     # 0.4
        self.assertEqual(money.divide(5, 10), 1)     # 0.5
        self.assertEqual(money.divide(6, 10), 1)     # 0.6

    def test_exact_division_is_untouched(self):
        for n, d in ((100, 10), (0, 7), (-250, 5)):
            self.assertEqual(money.divide(n, d), n // d)

    def test_negative_denominator(self):
        self.assertEqual(money.divide(1, -2), -1)
        self.assertEqual(money.divide(-1, -2), 1)

    def test_division_by_zero_raises(self):
        with self.assertRaises(ZeroDivisionError):
            money.divide(1, 0)

    def test_no_float_or_builtin_round_in_the_module(self):
        """A single float reintroduces exactly what this module exists to
        remove, and builtin round() is banker's -- silently ADR-15's
        opposite.

        Checked with `ast` rather than by grep: the docstrings here have to
        be able to SAY "round()" and "float" while explaining why neither is
        used. A string search cannot tell the difference between code and the
        comment describing it.
        """
        import ast
        with open(os.path.join(ROOT, "ops", "money.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in ("float", "round"):
                    offenders.append(f"{node.func.id}() at line {node.lineno}")
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                offenders.append(f"float literal {node.value} at line {node.lineno}")
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                offenders.append(f"true division at line {node.lineno}")
        self.assertEqual(offenders, [])


class TestRates(unittest.TestCase):
    def test_gst(self):
        self.assertEqual(money.apply_rate(10_000, 1000), 1_000)     # $100 -> $10

    def test_gst_at_the_half_cent_boundary(self):
        """10% of 5c is exactly half a cent. Half-up gives 1, banker's gives
        0 -- this is the case that decides which mode is in force."""
        self.assertEqual(money.apply_rate(5, 1000), 1)

    def test_payroll_rates_are_exact_integers(self):
        """4.85% payroll tax is 485 bp. As a percentage it would be a
        decimal needing rounding before it was even applied."""
        self.assertEqual(money.apply_rate(100_000, 485), 4_850)
        self.assertEqual(money.apply_rate(100_000, 178), 1_780)   # WorkCover
        self.assertEqual(money.apply_rate(100_000, 39), 390)      # iCare NSW

    def test_negative_amounts_round_away_from_zero_too(self):
        self.assertEqual(money.apply_rate(-5, 1000), -1)

    def test_a_rate_never_loses_a_cent_silently(self):
        """Truncation would drift downward on every line. Over the FY27
        register that is a real, one-directional shortfall."""
        truncating = sum((c * 1000) // 10_000 for c in range(1, 500))
        rounding = sum(money.apply_rate(c, 1000) for c in range(1, 500))
        self.assertGreater(rounding, truncating)


class TestParse(unittest.TestCase):
    def test_the_shapes_the_workbooks_actually_contain(self):
        for text, want in [
            ("$1,234.56", 123456), ("1234.56", 123456), ("$0.00", 0),
            ("-", 0), ("", 0), (None, 0), ("$700,000", 70000000),
            ("(1,234.56)", -123456), ("-$550.00", -55000),
            ("$3,520,041.73", 352004173), ("$45,361", 4536100),
        ]:
            self.assertEqual(money.parse(text), want, text)

    def test_sub_cent_values_are_ROUNDED_not_truncated(self):
        """The live defect this module was written to fix: the importer took
        the first two decimals and dropped the rest, so $1,234.565 became
        $1,234.56. Small, always downward, and invisible."""
        self.assertEqual(money.parse("1234.565"), 123457)
        self.assertEqual(money.parse("1234.564"), 123456)
        self.assertEqual(money.parse("0.005"), 1)
        self.assertEqual(money.parse("0.004"), 0)
        self.assertEqual(money.parse("-0.005"), -1)

    def test_no_float_error_on_the_classic_case(self):
        self.assertEqual(money.parse("19.99"), 1999)
        self.assertEqual(money.parse("0.29"), 29)
        self.assertEqual(money.parse("1.005"), 101)

    def test_long_decimals(self):
        self.assertEqual(money.parse("1.23456789"), 123)
        self.assertEqual(money.parse("0.999"), 100)

    def test_garbage_raises_rather_than_guessing(self):
        for text in ("about $500", "1.2.3", "$--5", "twelve", "1,2x4.00"):
            with self.assertRaises(money.MoneyError, msg=text):
                money.parse(text)

    def test_very_large_amounts_stay_exact(self):
        """Integers do not lose precision; a float would, above about $90bn."""
        self.assertEqual(money.parse("999999999999.99"), 99999999999999)


class TestFormat(unittest.TestCase):
    def test_round_trip(self):
        for text in ("$1,234.56", "$0.00", "$3,520,041.73", "$700,000.00"):
            self.assertEqual(money.format(money.parse(text)), text)

    def test_negatives_use_parentheses_like_the_source(self):
        self.assertEqual(money.format(-123456), "($1,234.56)")


class TestAgainstThePinnedRegister(unittest.TestCase):
    """ADR-15 asked for the three pinned FY27 totals to be recomputed under
    both modes before STP-1 closes. They agree, because nothing in the
    register requires rounding -- every source value is already exact to the
    cent. Recorded here so the question is answered rather than remembered.
    """

    def setUp(self):
        import csv
        path = os.path.join(ROOT, "tests", "fixtures",
                            "project_register_fy27.csv")
        with open(path, newline="", encoding="utf-8-sig") as f:
            self.rows = [r for r in csv.DictReader(f) if r["Project"].strip()]

    def test_no_source_value_needs_rounding(self):
        for r in self.rows:
            for col in ("Purchase Order", "Invoiced Prior",
                        "Contract Value FY27"):
                raw = (r[col] or "").strip().replace("$", "").replace(",", "")
                if "." in raw:
                    self.assertLessEqual(len(raw.split(".")[1]), 2,
                                         f"{r['Project']} {col}={raw}")

    def test_the_pins_reproduce_through_the_money_module(self):
        self.assertEqual(
            sum(money.parse(r["Purchase Order"]) for r in self.rows), 723190700)
        self.assertEqual(
            sum(money.parse(r["Invoiced Prior"]) for r in self.rows), 371186527)
        self.assertEqual(
            sum(money.parse(r["Contract Value FY27"]) for r in self.rows),
            352004173)


if __name__ == "__main__":
    unittest.main(verbosity=2)
