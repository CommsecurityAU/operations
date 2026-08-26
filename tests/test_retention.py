"""Retention — withholding, the cap, and release (migration 004).

Worked from the example given on 25 Aug: $700k PO, 10% withheld per claim,
capped at 2.5%, released either at DLP end or split between practical
completion and DLP end.

All amounts ex-GST.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from ops import money  # noqa: E402
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.join(ROOT, "ops", "migrations")

CONTRACT = 70000000        # $700,000
RATE_BP = 1000             # 10% per claim
CAP_BP = 250               # 2.5% of the contract
CAP_CENTS = 1750000        # $17,500
CLAIM = 10000000           # $100,000


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Db(os.path.join(self.dir, "ops.db"), MIGRATIONS)
        self.db.migrate()
        self.user = self.db.upsert_user("s1", "r@x", "R")
        with self.db._tx() as c:
            c.execute("INSERT INTO client (entity_id,name) VALUES (1,'Hines')")
            c.execute(
                """INSERT INTO project (entity_id,name,job_code,status,created_ts,
                       practical_completion_date, dlp_end_date)
                   VALUES (1,'Retained','JN-9900','Active',0,
                           '2027-03-31','2028-03-31')""")
        self.project_id = self.db.scalar(
            "SELECT id FROM project WHERE job_code='JN-9900'")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def make_po(self, amount=CONTRACT, applies=1, rate=RATE_BP, cap=CAP_BP,
                policy="dlp", split=None):
        with self.db._tx() as c:
            c.execute(
                """INSERT INTO customer_po (entity_id,project_id,amount_cents,
                       retention_applies,retention_rate_bp,retention_cap_bp,
                       release_policy,release_split_bp,created_ts)
                   VALUES (1,?,?,?,?,?,?,?,0)""",
                (self.project_id, amount, applies, rate, cap, policy, split))
        return self.db.scalar(
            "SELECT id FROM customer_po WHERE project_id=? ORDER BY id DESC LIMIT 1",
            (self.project_id,))

    def claim(self, po_id, amount, status="invoiced", release=0):
        withheld = 0 if release else self.db.retention_for_claim(po_id, amount)
        with self.db._tx() as c:
            c.execute(
                """INSERT INTO claim_line (entity_id,project_id,customer_po_id,
                       status,amount_cents,retention_cents,
                       is_retention_release,created_ts)
                   VALUES (1,?,?,?,?,?,?,0)""",
                (self.project_id, po_id, status, amount, withheld, release))
        return withheld

    def position(self, po_id):
        return self.db.query_one(
            "SELECT * FROM v_po_retention_position WHERE customer_po_id = ?",
            (po_id,))


class TestWithholding(Case):
    def test_the_cap_is_two_and_a_half_percent_of_the_po(self):
        po = self.make_po()
        self.assertEqual(self.position(po)["cap_cents"], CAP_CENTS)

    def test_the_worked_example(self):
        """$700k, 10%, capped at 2.5%: 10,000 then 7,500 then nothing."""
        po = self.make_po()
        self.assertEqual(self.claim(po, CLAIM), 1000000)   # $10,000
        self.assertEqual(self.claim(po, CLAIM), 750000)    # $7,500, capped
        self.assertEqual(self.claim(po, CLAIM), 0)
        self.assertEqual(self.position(po)["withheld_cents"], CAP_CENTS)

    def test_a_po_without_retention_withholds_nothing(self):
        """Scope run as a separate PO often carries no retention at all."""
        po = self.make_po(applies=0)
        self.assertEqual(self.claim(po, CLAIM), 0)

    def test_two_pos_on_one_project_are_independent(self):
        """The reason retention lives on the PO and not the project."""
        with_ret = self.make_po()
        without = self.make_po(applies=0)
        self.assertEqual(self.claim(with_ret, CLAIM), 1000000)
        self.assertEqual(self.claim(without, CLAIM), 0)
        self.assertEqual(self.position(without)["withheld_cents"], 0)

    def test_a_variation_raising_the_po_reopens_capacity(self):
        """A variation that updates the PO raises the cap with it."""
        po = self.make_po()
        self.claim(po, CLAIM)
        self.claim(po, CLAIM)
        self.assertEqual(self.position(po)["remaining_to_withhold_cents"], 0)
        with self.db._tx() as c:
            c.execute("UPDATE customer_po SET amount_cents = ? WHERE id = ?",
                      (100000000, po))
        self.assertEqual(self.position(po)["cap_cents"], 2500000)
        self.assertEqual(self.claim(po, CLAIM), 750000)

    def test_only_invoiced_and_paid_claims_count_toward_the_cap(self):
        """A forecast has not withheld anything -- the customer has not held
        money back on an invoice that does not exist."""
        po = self.make_po()
        self.claim(po, CLAIM, status="forecast")
        self.assertEqual(self.position(po)["withheld_cents"], 0)
        self.assertEqual(self.position(po)["remaining_to_withhold_cents"],
                         CAP_CENTS)

    def test_recosting_a_claim_does_not_shrink_its_own_capacity(self):
        """Its withholding is already counted, so it has to be given back
        before the cap is applied -- otherwise editing a claim silently
        reduces what it may withhold."""
        po = self.make_po()
        self.claim(po, CLAIM)
        line = self.db.query_one(
            "SELECT id FROM claim_line WHERE customer_po_id = ?", (po,))
        again = self.db.retention_for_claim(po, CLAIM,
                                            exclude_claim_id=line["id"])
        self.assertEqual(again, 1000000)

    def test_rounding_goes_through_the_one_money_function(self):
        """10% of an odd number lands on a half cent; ADR-15 says half away
        from zero, and Python's round() would say otherwise."""
        po = self.make_po(amount=100000000)
        self.assertEqual(self.claim(po, 5), money.apply_rate(5, RATE_BP))

    def test_a_claim_larger_than_the_whole_cap_is_capped(self):
        po = self.make_po()
        self.assertEqual(self.claim(po, CONTRACT), CAP_CENTS)


class TestRelease(Case):
    def test_dlp_policy_releases_everything_at_dlp_end(self):
        po = self.make_po(policy="dlp")
        self.claim(po, CLAIM)
        self.claim(po, CLAIM)
        schedule = self.db.retention_release_schedule(po)
        self.assertEqual(len(schedule), 1)
        self.assertEqual(schedule[0]["stage"], "dlp_end")
        self.assertEqual(schedule[0]["amount_cents"], CAP_CENTS)
        self.assertEqual(schedule[0]["due_date"], "2028-03-31")

    def test_split_policy_divides_between_the_two_milestones(self):
        po = self.make_po(policy="split", split=5000)
        self.claim(po, CLAIM)
        self.claim(po, CLAIM)
        schedule = self.db.retention_release_schedule(po)
        self.assertEqual([s["stage"] for s in schedule],
                         ["practical_completion", "dlp_end"])
        self.assertEqual([s["amount_cents"] for s in schedule],
                         [875000, 875000])
        self.assertEqual(schedule[0]["due_date"], "2027-03-31")
        self.assertEqual(schedule[1]["due_date"], "2028-03-31")

    def test_the_split_need_not_be_even(self):
        """It varies by contract."""
        po = self.make_po(policy="split", split=6000)
        self.claim(po, CLAIM)
        self.claim(po, CLAIM)
        schedule = self.db.retention_release_schedule(po)
        self.assertEqual(schedule[0]["amount_cents"], 1050000)   # 60%
        self.assertEqual(schedule[1]["amount_cents"], 700000)    # the rest
        self.assertEqual(sum(s["amount_cents"] for s in schedule), CAP_CENTS)

    def test_a_split_never_loses_a_cent(self):
        """The second stage takes the remainder rather than its own
        percentage, so an odd amount cannot vanish in rounding."""
        po = self.make_po(policy="split", split=3333)
        self.claim(po, 333333)
        schedule = self.db.retention_release_schedule(po)
        self.assertEqual(sum(s["amount_cents"] for s in schedule),
                         self.position(po)["held_cents"])

    def test_a_missing_dlp_date_is_DERIVED_from_practical_completion(self):
        """A DLP typically ends 12 months after practical completion, so a
        release date can be estimated rather than left blank -- but it is
        flagged `estimated`, and never written to the project. A date nobody
        agreed to becomes a fact the moment it is stored."""
        with self.db._tx() as c:
            c.execute("UPDATE project SET dlp_end_date = NULL WHERE id = ?",
                      (self.project_id,))
        po = self.make_po(policy="dlp")
        self.claim(po, CLAIM)
        stage = self.db.retention_release_schedule(po)[0]
        self.assertEqual(stage["due_date"], "2028-03-31")   # PC + 12 months
        self.assertTrue(stage["estimated"])
        self.assertIsNone(self.db.scalar(
            "SELECT dlp_end_date FROM project WHERE id = ?", (self.project_id,)))

    def test_a_real_dlp_date_is_never_flagged_as_estimated(self):
        po = self.make_po(policy="dlp")
        self.claim(po, CLAIM)
        self.assertFalse(self.db.retention_release_schedule(po)[0]["estimated"])

    def test_with_neither_date_the_release_is_still_unknown(self):
        """Nothing to derive from. An unknown release date is information;
        inventing one puts a number in a cash forecast nobody can defend."""
        with self.db._tx() as c:
            c.execute("""UPDATE project SET dlp_end_date = NULL,
                             practical_completion_date = NULL WHERE id = ?""",
                      (self.project_id,))
        po = self.make_po(policy="dlp")
        self.claim(po, CLAIM)
        stage = self.db.retention_release_schedule(po)[0]
        self.assertIsNone(stage["due_date"])
        self.assertFalse(stage["estimated"])

    def test_month_arithmetic_clamps_to_the_end_of_the_month(self):
        """31 January plus one month is 28 February, not an error."""
        self.assertEqual(self.db.add_months("2027-01-31", 1), "2027-02-28")
        self.assertEqual(self.db.add_months("2028-01-31", 1), "2028-02-29")
        self.assertEqual(self.db.add_months("2027-03-31", 12), "2028-03-31")
        self.assertEqual(self.db.add_months("2026-12-15", 12), "2027-12-15")
        self.assertIsNone(self.db.add_months(None, 12))


class TestRetentionTerms(Case):
    """The register states retention per PROJECT; the model holds it per PO."""

    def test_terms_apply_to_every_po_on_the_project(self):
        a = self.make_po(applies=0)
        b = self.make_po(applies=0, amount=20000000)
        self.db.set_retention_terms(self.project_id, 500, 1000, "split", 5000,
                                    self.user["id"])
        for po in (a, b):
            row = self.position(po)
            self.assertTrue(row["retention_applies"])
            self.assertEqual(row["cap_bp"], 500)
            self.assertEqual(row["rate_bp"], 1000)

    def test_each_po_still_caps_independently(self):
        """Which is what happens in practice when scope is split across
        orders."""
        a = self.make_po(applies=0)                      # $700k -> cap $35,000
        b = self.make_po(applies=0, amount=20000000)     # $200k -> cap $10,000
        self.db.set_retention_terms(self.project_id, 500, 1000, "split", 5000,
                                    self.user["id"])
        self.assertEqual(self.position(a)["cap_cents"], 3500000)
        self.assertEqual(self.position(b)["cap_cents"], 1000000)

    def test_a_zero_cap_removes_retention(self):
        po = self.make_po()
        self.db.set_retention_terms(self.project_id, 0, None, None, None,
                                    self.user["id"])
        row = self.position(po)
        self.assertFalse(row["retention_applies"])
        self.assertEqual(row["cap_cents"], 0)

    def test_setting_the_same_terms_twice_changes_nothing(self):
        self.make_po(applies=0)
        first = self.db.set_retention_terms(self.project_id, 500, 1000,
                                            "split", 5000, self.user["id"])
        second = self.db.set_retention_terms(self.project_id, 500, 1000,
                                             "split", 5000, self.user["id"])
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_it_is_audited(self):
        self.make_po(applies=0)
        self.db.set_retention_terms(self.project_id, 500, 1000, "split", 5000,
                                    self.user["id"])
        row = self.db.query_one(
            "SELECT detail FROM audit_log WHERE action='retention_terms'")
        self.assertIn("500bp", row["detail"])

    def test_the_five_percent_split_matches_the_agreed_terms(self):
        """5% cap, 10% per claim, half released at practical completion and
        half at DLP end. On a $710,000 contract that is $35,500 held,
        $17,750 at each milestone."""
        po = self.make_po(applies=0, amount=71000000)
        self.db.set_retention_terms(self.project_id, 500, 1000, "split", 5000,
                                    self.user["id"])
        self.assertEqual(self.position(po)["cap_cents"], 3550000)
        for _ in range(4):
            self.claim(po, 10000000)          # $100k claims, 10% each
        self.assertEqual(self.position(po)["withheld_cents"], 3550000)
        stages = self.db.retention_release_schedule(po)
        self.assertEqual([s["amount_cents"] for s in stages],
                         [1775000, 1775000])

    def test_releasing_reduces_what_is_held(self):
        """A release is an ordinary claim line, flagged -- so it forecasts,
        ages and invoices through machinery that already exists."""
        po = self.make_po(policy="dlp")
        self.claim(po, CLAIM)
        self.claim(po, CLAIM)
        self.assertEqual(self.position(po)["held_cents"], CAP_CENTS)
        self.claim(po, 875000, release=1)
        self.assertEqual(self.position(po)["held_cents"], 875000)

    def test_a_release_does_not_itself_withhold_retention(self):
        po = self.make_po()
        self.claim(po, CLAIM)
        self.assertEqual(self.claim(po, 500000, release=1), 0)

    def test_nothing_held_means_nothing_to_release(self):
        self.assertEqual(self.db.retention_release_schedule(self.make_po()), [])


class TestProjectRollup(Case):
    def test_it_sums_across_pos_and_carries_the_dates(self):
        """Each PO caps independently: $700k caps at $17,500 so a $100k
        claim withholds the full $10,000, but the $200k PO caps at $5,000 so
        the same claim withholds only that."""
        a = self.make_po()
        b = self.make_po(amount=20000000)
        self.assertEqual(self.claim(a, CLAIM), 1000000)
        self.assertEqual(self.claim(b, CLAIM), 500000)
        row = self.db.query_one(
            "SELECT * FROM v_project_retention WHERE project_id = ?",
            (self.project_id,))
        self.assertEqual(row["withheld_cents"], 1500000)
        self.assertEqual(row["practical_completion_date"], "2027-03-31")
        self.assertEqual(row["dlp_end_date"], "2028-03-31")

    def test_a_po_without_retention_reports_a_cap_of_zero(self):
        """Not 2.5% of the PO. A cap on money nobody is holding is the same
        species of lie as a dashboard cell reading #N/A, and the view
        computed it from cap_bp regardless of whether retention applied."""
        self.make_po(applies=0)
        row = self.db.query_one(
            "SELECT * FROM v_project_retention WHERE project_id = ?",
            (self.project_id,))
        for key in ("cap_cents", "withheld_cents", "released_cents", "held_cents"):
            self.assertEqual(row[key], 0, key)


class TestMigrationIsExpandOnly(unittest.TestCase):
    def test_004_only_adds(self):
        with open(os.path.join(MIGRATIONS, "004_retention.sql"),
                  encoding="utf-8") as f:
            body = f.read().upper()
        self.assertNotIn("DROP TABLE", body)
        self.assertNotIn("DROP COLUMN", body)
        for statement in body.split(";"):
            if "ALTER TABLE" in statement:
                self.assertIn("ADD COLUMN", statement)


if __name__ == "__main__":
    unittest.main(verbosity=2)
