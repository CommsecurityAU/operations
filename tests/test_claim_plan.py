"""Claim planning — items, allocations, generation (migration 008).

Rebuilt from the real progress-claim workbooks. `720 Bourke St` is the
worked example throughout, because if the model cannot reproduce a sheet
that has been invoiced against, the model is wrong.
"""

import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from ops import money  # noqa: E402
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.join(ROOT, "ops", "migrations")

CONTRACT = 19861000        # $198,610.00
PHASES = [("Verification of Design", 5958300),
          ("Deployment of the ISP", 7944400),
          ("Commissioning & Site Acceptance Testing", 5958300)]
# item, month, percent bp, amount cents — straight from the sheet
PLAN = [("Verification of Design", "Mar-26", 2000, 1191660),
        ("Verification of Design", "Apr-26", 2000, 1191660),
        ("Verification of Design", "May-26", 6000, 3574980),
        ("Deployment of the ISP", "May-26", 3333, 2648133),
        ("Deployment of the ISP", "Jun-26", 3333, 2648133),
        ("Deployment of the ISP", "Aug-26", 1667, 1324067),
        ("Deployment of the ISP", "Sep-26", 1667, 1324067),
        ("Commissioning & Site Acceptance Testing", "Sep-26", 6000, 3574980),
        ("Commissioning & Site Acceptance Testing", "Oct-26", 4000, 2383320)]
MONTHS = {"Mar-26": 1191660, "Apr-26": 1191660, "May-26": 6223113,
          "Jun-26": 2648133, "Aug-26": 1324067, "Sep-26": 4899047,
          "Oct-26": 2383320}


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Db(os.path.join(self.dir, "ops.db"), MIGRATIONS)
        self.db.migrate()
        self.user = self.db.upsert_user("s1", "r@x", "R")
        with self.db._tx() as c:
            c.execute("INSERT INTO client (entity_id,name) VALUES (1,'Kapitol')")
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,
                             created_ts, contract_value_cents)
                         VALUES (1,'720 Bourke','JN-5749','Active',0,?)""",
                      (CONTRACT,))
        self.project_id = self.db.scalar(
            "SELECT id FROM project WHERE job_code='JN-5749'")
        with self.db._tx() as c:
            c.execute("""INSERT INTO customer_po (entity_id,project_id,
                             amount_cents,created_ts) VALUES (1,?,?,0)""",
                      (self.project_id, CONTRACT))

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def period(self, label):
        return self.db.scalar("SELECT id FROM period WHERE label = ?", (label,))

    def build(self, plan=PLAN):
        items = {}
        for name, value in PHASES:
            items[name] = self.db.create_claim_item(
                {"entity_id": 1, "project_id": self.project_id,
                 "name": name, "value_cents": value}, self.user["id"])["id"]
        for name, month, pct, amount in plan:
            self.db.set_allocation(items[name], self.period(month), pct,
                                   amount, self.user["id"])
        return items

    def invoice(self, label, number="INV-1"):
        claim = self.db.query_one(
            """SELECT cl.id FROM claim_line cl JOIN period pe ON pe.id = cl.period_id
               WHERE cl.project_id = ? AND pe.label = ?""",
            (self.project_id, label))
        for to, extra in (("due", {}), ("approved", {"approved_date": "2026-03-20"}),
                          ("invoiced", {"invoice_number": number,
                                        "invoiced_date": "2026-03-25"})):
            self.db.transition_claim(claim["id"], to, extra, None, self.user["id"])
        self.db.lock_plan_for_claim(claim["id"], self.user["id"])
        return claim["id"]


class TestItReproducesTheWorkbook(Case):
    def test_items_sum_to_the_contract(self):
        self.build()
        health = self.db.plan_health(self.project_id)
        self.assertEqual(health["item_value_cents"], CONTRACT)
        self.assertEqual(health["unitemised_cents"], 0)

    def test_every_item_is_fully_allocated(self):
        self.build()
        for item in self.db.plan_health(self.project_id)["items"]:
            self.assertEqual(item["unallocated_cents"], 0, item["name"])
            self.assertEqual(item["allocated_bp"], 10000, item["name"])

    def test_each_month_totals_the_sheet(self):
        """A month's claims SUM to the sheet's monthly figure. There may be
        several: `May-26` is Verification 60% and Deployment 33.33%, and the
        workbook lists them separately too. A claim line is one
        contribution; an INVOICE groups them, which is why `200 Victoria`
        has two Aug-26 rows sharing `Inv No. 6072/5`."""
        self.build()
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        for label, expected in MONTHS.items():
            self.assertEqual(self.db.scalar(
                """SELECT SUM(cl.amount_cents) FROM claim_line cl
                   JOIN period pe ON pe.id = cl.period_id
                   WHERE cl.project_id = ? AND pe.label = ?""",
                (self.project_id, label)), expected, label)

    def test_a_month_of_two_items_makes_two_claims(self):
        self.build()
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        self.assertEqual(self.db.scalar(
            """SELECT COUNT(*) FROM claim_line cl
               JOIN period pe ON pe.id = cl.period_id
               WHERE cl.project_id = ? AND pe.label = 'May-26'""",
            (self.project_id,)), 2)

    def test_the_claims_total_the_contract(self):
        self.build()
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        self.assertEqual(self.db.scalar(
            "SELECT SUM(amount_cents) FROM claim_line WHERE project_id = ?",
            (self.project_id,)), CONTRACT)

    def test_a_month_with_no_allocation_produces_no_claim(self):
        """`Jul-26` is absent from the plan, and the workbook has no row for
        it either -- not a zero, nothing. Sometimes a project is simply not
        claimed that month."""
        self.build()
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        self.assertIsNone(self.db.query_one(
            """SELECT cl.id FROM claim_line cl JOIN period pe ON pe.id = cl.period_id
               WHERE cl.project_id = ? AND pe.label = 'Jul-26'""",
            (self.project_id,)))

    def test_the_detail_says_which_item_and_what_share(self):
        """A claim that reads only as a number is a claim nobody can
        check."""
        self.build()
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        details = [r["detail"] for r in self.db.query(
            """SELECT cl.detail FROM claim_line cl
               JOIN period pe ON pe.id = cl.period_id
               WHERE cl.project_id = ? AND pe.label = 'May-26'""",
            (self.project_id,))]
        self.assertIn("Verification of Design, 60%", details)
        self.assertIn("Deployment of the ISP, 33.33%", details)


class TestTheAmountIsTheFact(Case):
    def test_the_percentage_is_provenance_not_arithmetic(self):
        """`33.33%` of $79,444 is $26,478.69, but the agreed figure is
        $26,481.33 -- a third, displayed rounded. Deriving the amount from
        the percentage would move money."""
        items = self.build()
        row = self.db.query_one(
            """SELECT amount_cents, percent_bp FROM claim_allocation
               WHERE claim_item_id = ? AND period_id = ?""",
            (items["Deployment of the ISP"], self.period("May-26")))
        self.assertEqual(row["amount_cents"], 2648133)
        self.assertNotEqual(money.divide(7944400 * row["percent_bp"], 10000),
                            row["amount_cents"])

    def test_setting_zero_removes_the_allocation(self):
        """A month an item no longer contributes to should not linger as a
        row saying nothing."""
        items = self.build()
        self.db.set_allocation(items["Verification of Design"],
                               self.period("Mar-26"), 0, 0, self.user["id"])
        self.assertIsNone(self.db.query_one(
            """SELECT id FROM claim_allocation
               WHERE claim_item_id = ? AND period_id = ?""",
            (items["Verification of Design"], self.period("Mar-26"))))

    def test_one_allocation_per_item_per_month(self):
        items = self.build()
        self.db.set_allocation(items["Verification of Design"],
                               self.period("Mar-26"), 2500, 1489575,
                               self.user["id"])
        self.assertEqual(self.db.scalar(
            """SELECT COUNT(*) FROM claim_allocation
               WHERE claim_item_id = ? AND period_id = ?""",
            (items["Verification of Design"], self.period("Mar-26"))), 1)


class TestGenerationIsSafe(Case):
    def test_generating_twice_creates_nothing_more(self):
        self.build()
        first = self.db.generate_plan_claims(self.project_id, self.user["id"])
        again = self.db.generate_plan_claims(self.project_id, self.user["id"])
        self.assertEqual(first["created"], len(PLAN))
        self.assertEqual(again["created"], 0)
        self.assertEqual(again["updated"], len(PLAN))

    def test_re_spreading_updates_the_claim(self):
        items = self.build()
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        self.db.set_allocation(items["Verification of Design"],
                               self.period("Apr-26"), 2500, 1489575,
                               self.user["id"])
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        self.assertEqual(self.db.scalar(
            """SELECT cl.amount_cents FROM claim_line cl
               JOIN period pe ON pe.id = cl.period_id
               WHERE cl.project_id = ? AND pe.label = 'Apr-26'""",
            (self.project_id,)), 1489575)


class TestInvoicedMonthsAreFixed(Case):
    def test_invoicing_locks_the_allocations_behind_it(self):
        self.build()
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        self.invoice("Mar-26")
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM claim_allocation WHERE locked_claim_id IS NOT NULL"),
            1)

    def test_a_locked_month_cannot_be_re_spread(self):
        """Re-forecasting must not move a month that has been billed -- the
        same boundary as the slippage rule."""
        items = self.build()
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        self.invoice("Mar-26")
        with self.assertRaises(ValueError) as e:
            self.db.set_allocation(items["Verification of Design"],
                                   self.period("Mar-26"), 3000, 1787490,
                                   self.user["id"])
        self.assertIn("amend the claim instead", str(e.exception))

    def test_unbilled_months_stay_free(self):
        items = self.build()
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        self.invoice("Mar-26")
        self.db.set_allocation(items["Verification of Design"],
                               self.period("Apr-26"), 2500, 1489575,
                               self.user["id"])
        result = self.db.generate_plan_claims(self.project_id, self.user["id"])
        self.assertEqual(result["locked"], 1)
        self.assertEqual(self.db.scalar(
            """SELECT cl.amount_cents FROM claim_line cl
               JOIN period pe ON pe.id = cl.period_id
               WHERE cl.project_id = ? AND pe.label = 'Mar-26'""",
            (self.project_id,)), 1191660)

    def test_regeneration_never_touches_an_invoiced_claim(self):
        self.build()
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        self.invoice("Mar-26")
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        self.assertEqual(self.db.scalar(
            """SELECT cl.status FROM claim_line cl
               JOIN period pe ON pe.id = cl.period_id
               WHERE cl.project_id = ? AND pe.label = 'Mar-26'""",
            (self.project_id,)), "invoiced")


class TestAmendingAnInvoicedClaim(Case):
    """Rare, and real. A system that makes it impossible forces the
    correction into a spreadsheet, which is what this replaces."""

    def test_it_records_what_the_invoice_said(self):
        """Reconciling to Xero means matching against the figure that was
        issued, not the one it was corrected to."""
        self.build()
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        claim = self.invoice("Mar-26", number="1405821")
        self.db.amend_invoiced_claim(claim, 1200000, "client agreed a change",
                                     self.user["id"])
        row = self.db.query_one("SELECT * FROM claim_amendment")
        self.assertEqual(row["invoice_number"], "1405821")
        self.assertEqual(row["invoiced_cents"], 1191660)
        self.assertEqual(row["amended_cents"], 1200000)

    def test_a_reason_is_required(self):
        self.build()
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        claim = self.invoice("Mar-26")
        with self.assertRaises(ValueError):
            self.db.amend_invoiced_claim(claim, 1, "   ", self.user["id"])

    def test_a_claim_that_is_not_invoiced_is_edited_not_amended(self):
        self.build()
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        claim = self.db.query_one(
            "SELECT id FROM claim_line WHERE project_id = ?", (self.project_id,))
        with self.assertRaises(ValueError) as e:
            self.db.amend_invoiced_claim(claim["id"], 1, "x", self.user["id"])
        self.assertIn("has not been invoiced", str(e.exception))


class TestPlanHealth(Case):
    def test_an_under_itemised_contract_shows_the_gap(self):
        """Work either unplanned or over-committed, and the workbook checks
        it by hand with `Left to Claim`."""
        self.db.create_claim_item(
            {"entity_id": 1, "project_id": self.project_id,
             "name": "Verification of Design", "value_cents": 5958300},
            self.user["id"])
        health = self.db.plan_health(self.project_id)
        self.assertEqual(health["unitemised_cents"], CONTRACT - 5958300)

    def test_a_partly_allocated_item_shows_short(self):
        items = self.build([("Verification of Design", "Mar-26", 2000, 1191660)])
        health = self.db.plan_health(self.project_id)
        design = [i for i in health["items"]
                  if i["name"] == "Verification of Design"][0]
        self.assertEqual(design["allocated_bp"], 2000)
        self.assertEqual(design["unallocated_cents"], 5958300 - 1191660)
        self.assertTrue(items)

    def test_variations_sit_outside_the_contract_total(self):
        """The workbooks keep them in a separate block for that reason."""
        self.build()
        self.db.create_claim_item(
            {"entity_id": 1, "project_id": self.project_id, "name": "VO-1",
             "value_cents": 500000, "is_variation": 1}, self.user["id"])
        health = self.db.plan_health(self.project_id)
        self.assertEqual(health["item_value_cents"], CONTRACT)
        self.assertEqual(health["variation_value_cents"], 500000)
        self.assertEqual(health["unitemised_cents"], 0)

    def test_an_item_behind_an_invoice_cannot_be_deleted(self):
        self.build()
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        self.invoice("Mar-26")
        item = self.db.scalar(
            "SELECT id FROM claim_item WHERE name = 'Verification of Design'")
        self.assertIn("history", self.db.claim_item_is_deletable(item))


class TestAPlanDescribesWhatIsLeft(Case):
    """`v_project_claim_plan` compared items against the WHOLE contract,
    which reported an under-itemised plan on every project already
    part-invoiced when the window opened.

    `720 Bourke`: a $198,610 contract with $112,545.67 claimed in FY26. The
    panel called that unitemised, but it is BILLED -- and no plan in this
    system could ever describe it.
    """

    def opening(self, cents):
        po = self.db.scalar(
            "SELECT id FROM customer_po WHERE project_id = ?", (self.project_id,))
        with self.db._tx() as c:
            c.execute(
                """INSERT INTO claim_line (entity_id, project_id, customer_po_id,
                       status, amount_cents, is_opening_balance, claim_date,
                       invoiced_date, created_ts)
                   VALUES (1,?,?, 'invoiced', ?, 1, '2026-06-30','2026-06-30',0)""",
                (self.project_id, po, cents))

    def test_the_opening_balance_is_not_unplanned(self):
        self.opening(11254567)
        for name, value in (("Deployment of the ISP", 2648134),
                            ("Commissioning & SAT", 5958300)):
            self.db.create_claim_item(
                {"entity_id": 1, "project_id": self.project_id,
                 "name": name, "value_cents": value}, self.user["id"])
        health = self.db.plan_health(self.project_id)
        self.assertEqual(health["opening_balance_cents"], 11254567)
        self.assertEqual(health["plannable_cents"], CONTRACT - 11254567)
        self.assertEqual(health["unitemised_cents"], -1)      # the known cent

    def test_with_no_opening_balance_plannable_is_the_contract(self):
        health = self.db.plan_health(self.project_id)
        self.assertEqual(health["opening_balance_cents"], 0)
        self.assertEqual(health["plannable_cents"], CONTRACT)

    def test_a_claim_invoiced_inside_the_window_stays_in_the_plan(self):
        """It was planned here. Removing it would make a completed project
        look unplanned."""
        self.build()
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        before = self.db.plan_health(self.project_id)["plannable_cents"]
        claim = self.db.query_one(
            """SELECT cl.id FROM claim_line cl JOIN period pe ON pe.id = cl.period_id
               WHERE cl.project_id = ? AND pe.label = 'Mar-26'""",
            (self.project_id,))
        for to, extra in (("due", {}), ("approved", {"approved_date": "2026-03-20"}),
                          ("invoiced", {"invoice_number": "X",
                                        "invoiced_date": "2026-03-25"})):
            self.db.transition_claim(claim["id"], to, extra, None, self.user["id"])
        self.assertEqual(
            self.db.plan_health(self.project_id)["plannable_cents"], before)
        self.assertEqual(
            self.db.plan_health(self.project_id)["unitemised_cents"], 0)


class TestAdoptingExistingClaims(Case):
    """Every project imported from the workbook arrived with its forecast
    already typed. A plan panel saying `no plan yet` beside thirteen
    forecast claims is lying by omission, and asking for the same forecast
    to be entered twice is how a tool stops being used."""

    def existing(self, rows):
        po = self.db.scalar(
            "SELECT id FROM customer_po WHERE project_id = ?", (self.project_id,))
        with self.db._tx() as c:
            for phase, month, amount, status in rows:
                c.execute(
                    """INSERT INTO claim_line (entity_id, project_id,
                           customer_po_id, period_id, status, amount_cents,
                           phase, created_ts)
                       VALUES (1,?,?,?,?,?,?,0)""",
                    (self.project_id, po, self.period(month), status, amount,
                     phase))

    def test_phases_become_items_and_claims_become_months(self):
        self.existing([("Verification of Design", "Mar-26", 1191660, "forecast"),
                       ("Verification of Design", "Apr-26", 1191660, "forecast"),
                       ("Deployment of the ISP", "May-26", 2648133, "forecast")])
        result = self.db.adopt_claims_into_plan(self.project_id, self.user["id"])
        self.assertEqual(result["items"], 2)
        self.assertEqual(result["allocations"], 3)
        health = self.db.plan_health(self.project_id)
        design = [i for i in health["items"]
                  if i["name"] == "Verification of Design"][0]
        self.assertEqual(design["value_cents"], 2383320)
        self.assertEqual(design["allocation_count"], 2)

    def test_a_claim_number_is_not_a_phase(self):
        """Of 147 forecast rows in the real register, 70 hold a claim
        NUMBER in the phase column. Grouping on those would produce an item
        per month rather than an item per part of the contract."""
        self.existing([("Progress Claim #1", "Mar-26", 1050000, "forecast"),
                       ("Progress Claim #2", "Apr-26", 175000, "forecast"),
                       ("Progress Claim #3", "May-26", 175000, "forecast")])
        result = self.db.adopt_claims_into_plan(self.project_id, self.user["id"])
        self.assertEqual(result["items"], 1)
        self.assertEqual(
            self.db.plan_health(self.project_id)["items"][0]["name"],
            "720 Bourke")

    def test_a_phase_behind_a_claim_number_is_kept(self):
        """`Progress Claim #4 / Deployment of the ISP` holds both."""
        self.assertEqual(
            Db.plan_group_name("Progress Claim #4\nDeployment of the ISP", "P"),
            "Deployment of the ISP")
        self.assertEqual(Db.plan_group_name("Monthly Claim", "P"), "P")
        self.assertEqual(Db.plan_group_name("Montly Claim", "P"), "P")
        self.assertEqual(Db.plan_group_name("Expected Aug-26", "P"), "P")
        self.assertEqual(Db.plan_group_name("", "P"), "P")

    def test_zero_rows_do_not_become_zero_items(self):
        """`Progress Claim #2` at $0.00 is a month nobody claimed, not a
        part of the contract."""
        self.existing([("Design", "Mar-26", 1191660, "forecast"),
                       ("Nothing", "Apr-26", 0, "forecast")])
        result = self.db.adopt_claims_into_plan(self.project_id, self.user["id"])
        self.assertEqual(result["items"], 1)
        self.assertEqual(result["skipped"], 1)

    def test_an_invoiced_month_is_fixed_on_adoption(self):
        """It was history before the plan existed."""
        self.existing([("Design", "Mar-26", 1191660, "invoiced")])
        self.db.adopt_claims_into_plan(self.project_id, self.user["id"])
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM claim_allocation WHERE locked_claim_id IS NOT NULL"),
            1)

    def test_several_claims_in_one_month_stay_separate(self):
        """`200 Victoria` has five Commissioning claims in Sep-26. Folding
        them into one share left generation unable to say which claim it had
        produced, and moving one relocated the whole month."""
        self.existing([("Design", "Mar-26", 1000000, "forecast"),
                       ("Design", "Mar-26", 191660, "forecast")])
        result = self.db.adopt_claims_into_plan(self.project_id, self.user["id"])
        self.assertEqual(result["allocations"], 2)
        self.assertEqual(self.db.scalar(
            "SELECT SUM(amount_cents) FROM claim_allocation"), 1191660)

    def test_each_adopted_claim_is_owned_by_its_allocation(self):
        self.existing([("Design", "Mar-26", 1000000, "forecast"),
                       ("Design", "Mar-26", 191660, "forecast")])
        self.db.adopt_claims_into_plan(self.project_id, self.user["id"])
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM claim_allocation WHERE claim_line_id IS NULL"),
            0)

    def test_a_rebuild_works_on_a_project_with_an_opening_balance(self):
        """Opening balances are immutable, and clearing `from_plan` across
        the project hit them -- so the rebuild threw on every project that
        had one, which is most of the register. The UI reported it as an
        internal error and the plan silently stayed as it was."""
        po = self.db.scalar(
            "SELECT id FROM customer_po WHERE project_id = ?", (self.project_id,))
        with self.db._tx() as c:
            c.execute(
                """INSERT INTO claim_line (entity_id, project_id, customer_po_id,
                       status, amount_cents, is_opening_balance, claim_date,
                       invoiced_date, created_ts)
                   VALUES (1,?,?, 'invoiced', 11254567, 1,
                           '2026-06-30','2026-06-30',0)""",
                (self.project_id, po))
        self.existing([("Design", "Mar-26", 1191660, "forecast")])
        self.db.adopt_claims_into_plan(self.project_id, self.user["id"])
        result = self.db.adopt_claims_into_plan(
            self.project_id, self.user["id"], rebuild=True)
        self.assertEqual(result["items"], 1)

    def test_a_rebuild_picks_up_a_task_added_afterwards(self):
        """The sequence that mattered: adopt with no tasks, backfill them,
        rebuild. The plan should describe the line items, not the phases."""
        self.existing([("Design", "Mar-26", 1191660, "forecast"),
                       ("Design", "Apr-26", 1191660, "forecast")])
        self.db.adopt_claims_into_plan(self.project_id, self.user["id"])
        self.assertEqual(len(self.db.plan_health(self.project_id)["items"]), 1)
        with self.db._tx() as c:
            c.execute("UPDATE claim_line SET task = 'SAT' WHERE amount_cents = ? "
                      "AND period_id = ?", (1191660, self.period("Mar-26")))
            c.execute("UPDATE claim_line SET task = 'Client Training' "
                      "WHERE amount_cents = ? AND period_id = ?",
                      (1191660, self.period("Apr-26")))
        self.db.adopt_claims_into_plan(self.project_id, self.user["id"],
                                       rebuild=True)
        names = sorted(i["name"] for i in
                       self.db.plan_health(self.project_id)["items"])
        self.assertEqual(names, ["Client Training", "SAT"])

    def test_it_refuses_when_a_plan_already_exists(self):
        """Adoption describes what is there; running it over a plan someone
        built would double it."""
        self.existing([("Design", "Mar-26", 1191660, "forecast")])
        self.db.adopt_claims_into_plan(self.project_id, self.user["id"])
        again = self.db.adopt_claims_into_plan(self.project_id, self.user["id"])
        self.assertEqual(again["items"], 0)
        self.assertIn("already has a plan", again["reason"])

    def test_generated_claims_are_not_adopted_back(self):
        """Otherwise generating and adopting in turn would compound."""
        self.build()
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        with self.db._tx() as c:
            c.execute("DELETE FROM claim_allocation")
            c.execute("DELETE FROM claim_item")
        result = self.db.adopt_claims_into_plan(self.project_id, self.user["id"])
        self.assertEqual(result["items"], 0)


class TestThePlanAndTheClaimsStayInStep(Case):
    """Two views of one forecast. Where they can disagree, they must not."""

    def existing(self, rows):
        po = self.db.scalar(
            "SELECT id FROM customer_po WHERE project_id = ?", (self.project_id,))
        with self.db._tx() as c:
            for phase, month, amount in rows:
                c.execute(
                    """INSERT INTO claim_line (entity_id, project_id,
                           customer_po_id, period_id, status, amount_cents,
                           phase, created_ts)
                       VALUES (1,?,?,?, 'forecast', ?,?,0)""",
                    (self.project_id, po, self.period(month), amount, phase))

    def total(self):
        return self.db.scalar(
            "SELECT COALESCE(SUM(amount_cents),0) FROM claim_line "
            "WHERE project_id = ?", (self.project_id,))

    def test_generating_after_adopting_does_not_duplicate(self):
        """Adoption builds the plan FROM the claims, so those claims are the
        plan's claims. Without marking them, generate found none for the
        month and created a second: $30,000 of forecast became $60,000,
        silently."""
        self.existing([("Design", "Sep-26", 1000000),
                       ("Design", "Oct-26", 2000000)])
        self.db.adopt_claims_into_plan(self.project_id, self.user["id"])
        result = self.db.generate_plan_claims(self.project_id, self.user["id"])
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["updated"], 2)
        self.assertEqual(self.total(), 3000000)

    def test_adopted_claims_are_marked_as_the_plans(self):
        self.existing([("Design", "Sep-26", 1000000)])
        self.db.adopt_claims_into_plan(self.project_id, self.user["id"])
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM claim_line WHERE from_plan = 1 "
            "AND project_id = ?", (self.project_id,)), 1)

    def test_moving_a_claim_moves_the_plan_with_it(self):
        """Otherwise the next Generate moves it back, silently undoing the
        decision just made."""
        self.existing([("Design", "Sep-26", 1000000)])
        self.db.adopt_claims_into_plan(self.project_id, self.user["id"])
        claim = self.db.scalar(
            "SELECT id FROM claim_line WHERE project_id = ?", (self.project_id,))
        self.db.update_claim_line(
            claim, {"period_id": self.period("Nov-26")}, self.user["id"], None)
        self.db.move_plan_allocations(claim, self.period("Sep-26"),
                                      self.period("Nov-26"), self.user["id"])
        self.assertEqual(self.db.scalar(
            """SELECT pe.label FROM claim_allocation a
               JOIN period pe ON pe.id = a.period_id"""), "Nov-26")

    def test_regenerating_after_a_move_changes_nothing(self):
        self.existing([("Design", "Sep-26", 1000000)])
        self.db.adopt_claims_into_plan(self.project_id, self.user["id"])
        claim = self.db.scalar(
            "SELECT id FROM claim_line WHERE project_id = ?", (self.project_id,))
        self.db.update_claim_line(
            claim, {"period_id": self.period("Nov-26")}, self.user["id"], None)
        self.db.move_plan_allocations(claim, self.period("Sep-26"),
                                      self.period("Nov-26"), self.user["id"])
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        self.assertEqual(self.total(), 1000000)
        self.assertEqual(self.db.scalar(
            """SELECT pe.label FROM claim_line cl
               JOIN period pe ON pe.id = cl.period_id
               WHERE cl.project_id = ?""", (self.project_id,)), "Nov-26")

    def test_a_locked_allocation_does_not_follow(self):
        """That month was invoiced, and its claim cannot be moved either."""
        self.existing([("Design", "Sep-26", 1000000)])
        self.db.adopt_claims_into_plan(self.project_id, self.user["id"])
        claim = self.db.scalar(
            "SELECT id FROM claim_line WHERE project_id = ?", (self.project_id,))
        self.db.lock_plan_for_claim(claim, self.user["id"])
        moved = self.db.move_plan_allocations(
            claim, self.period("Sep-26"), self.period("Nov-26"), self.user["id"])
        self.assertEqual(moved, 0)

    def test_it_may_move_onto_a_month_the_item_already_contributes_to(self):
        """The old `one share per item per month` rule was wrong: five
        Commissioning claims fall in `200 Victoria`'s Sep-26. It also broke
        moving a claim BACK -- the target month was `occupied`, so the plan
        silently stayed where it was while the claim returned."""
        self.existing([("Design", "Sep-26", 1000000),
                       ("Design", "Oct-26", 2000000)])
        self.db.adopt_claims_into_plan(self.project_id, self.user["id"])
        claim = self.db.scalar(
            """SELECT cl.id FROM claim_line cl JOIN period pe ON pe.id = cl.period_id
               WHERE cl.project_id = ? AND pe.label = 'Sep-26'""",
            (self.project_id,))
        self.assertEqual(self.db.move_plan_allocations(
            claim, self.period("Sep-26"), self.period("Oct-26"),
            self.user["id"]), 1)
        self.assertEqual(self.db.scalar(
            """SELECT COUNT(*) FROM claim_allocation a
               JOIN period pe ON pe.id = a.period_id WHERE pe.label = 'Oct-26'"""),
            2)

    def test_moving_out_and_back_returns_the_plan(self):
        """The sequence that exposed it: out to Oct-26, then back to Sep-26.
        The claim returned and the plan did not."""
        self.existing([("Design", "Sep-26", 1000000),
                       ("Design", "Sep-26", 500000)])
        self.db.adopt_claims_into_plan(self.project_id, self.user["id"])
        claim = self.db.scalar(
            "SELECT id FROM claim_line WHERE project_id = ? ORDER BY id LIMIT 1",
            (self.project_id,))
        self.db.move_plan_allocations(claim, self.period("Sep-26"),
                                      self.period("Oct-26"), self.user["id"])
        self.db.move_plan_allocations(claim, self.period("Oct-26"),
                                      self.period("Sep-26"), self.user["id"])
        labels = sorted(r["label"] for r in self.db.query(
            """SELECT pe.label FROM claim_allocation a
               JOIN period pe ON pe.id = a.period_id"""))
        self.assertEqual(labels, ["Sep-26", "Sep-26"])


class TestApiSurface(unittest.TestCase):
    """Over HTTP, because the module is where the role boundaries live.

    Standalone rather than a subclass of `Case`: inheriting its fixture
    built the project twice and the duplicate job code failed in setUp,
    which reports as an error in every test rather than one.
    """

    def setUp(self):
        import http.client
        import json
        import logging
        import threading
        from ops import auth
        from ops.config import Config
        from ops.main import boot
        from ops.secrets import LocalProvider
        self.json, self.http, self.auth = json, http.client, auth
        for n in ("ops.http", "ops.main", "ops.auth"):
            logging.getLogger(n).setLevel(logging.CRITICAL)
        self.dir = tempfile.mkdtemp()
        secrets = os.path.join(self.dir, "secrets", "store.json")
        LocalProvider(secrets).set("OIDC_CLIENT_SECRET", "x")
        cfg = Config(data_dir=self.dir, tls=False, port=0,
                     oidc_client_id="cid", oidc_redirect_uri="http://x/cb")
        self.db, self.server, self.sched = boot(
            cfg=cfg, env={"OPS_SECRETS_PATH": secrets}, serve=False)
        self.port = self.server.server_address[1]
        self.t = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
            daemon=True)
        self.t.start()
        self.key = auth.load_or_create_key(cfg.session_key_path)
        self.user = auth.sign_in(self.db, {"sub": "s1", "email": "r@x", "name": "R"})
        for role in ("viewer", "operations"):
            self.db.grant_role(self.user["id"], 1, role, self.user["id"])
        with self.db._tx() as c:
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,
                             created_ts, contract_value_cents)
                         VALUES (1,'720 Bourke','JN-5749','Active',0,?)""",
                      (CONTRACT,))
        self.project_id = self.db.scalar(
            "SELECT id FROM project WHERE job_code='JN-5749'")

    def tearDown(self):
        self.sched.stop()
        self.server.shutdown()
        self.server.server_close()
        self.t.join(timeout=5)
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def period(self, label):
        return self.db.scalar("SELECT id FROM period WHERE label = ?", (label,))

    def call(self, method, path, body=None):
        token = self.auth.mint_session(self.key, self.user["id"],
                                       self.user["token_version"])
        c = self.http.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path,
                  body=None if body is None else self.json.dumps(body).encode(),
                  headers={"Content-Type": "application/json",
                           "Sec-Fetch-Site": "same-origin",
                           "Cookie": f"{self.auth.COOKIE_NAME}={token}"})
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, (self.json.loads(raw) if raw else None)

    def build(self):
        items = {}
        for name, value in PHASES:
            items[name] = self.db.create_claim_item(
                {"entity_id": 1, "project_id": self.project_id,
                 "name": name, "value_cents": value}, self.user["id"])["id"]
        for name, month, pct, amount in PLAN:
            self.db.set_allocation(items[name], self.period(month), pct,
                                   amount, self.user["id"])
        return items

    def invoice(self, label):
        claim = self.db.query_one(
            """SELECT cl.id FROM claim_line cl JOIN period pe ON pe.id = cl.period_id
               WHERE cl.project_id = ? AND pe.label = ?""",
            (self.project_id, label))
        for to, extra in (("due", {}), ("approved", {"approved_date": "2026-03-20"}),
                          ("invoiced", {"invoice_number": "1405821",
                                        "invoiced_date": "2026-03-25"})):
            self.db.transition_claim(claim["id"], to, extra, None, self.user["id"])
        self.db.lock_plan_for_claim(claim["id"], self.user["id"])
        return claim["id"]

    def test_an_item_needs_a_name_and_a_value(self):
        for payload, key in (({"value_cents": 100}, "name"),
                             ({"name": "Equipment"}, "value_cents"),
                             ({"name": "Equipment", "value_cents": 0},
                              "value_cents")):
            status, body = self.call(
                "POST", f"/api/projects/{self.project_id}/plan/items", payload)
            self.assertEqual(status, 400, payload)
            self.assertIn(key, body["detail"])

    def test_the_plan_reports_gaps_rather_than_refusing_them(self):
        """A plan under construction is legitimately short of 100%, and
        demanding the whole thing in one sitting is how a tool stops being
        used."""
        status, _b = self.call(
            "POST", f"/api/projects/{self.project_id}/plan/items",
            {"name": "Equipment", "value_cents": 100000})
        self.assertEqual(status, 201)
        _s, plan = self.call("GET", f"/api/projects/{self.project_id}/plan")
        self.assertEqual(plan["unitemised_cents"], CONTRACT - 100000)

    def test_amending_needs_the_approver_role(self):
        """The figure has left the building and someone outside this system
        has it."""
        self.build()
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        claim = self.invoice("Mar-26")
        status, _b = self.call("POST", f"/api/claims/{claim}/amend",
                               {"amount_cents": 1200000, "reason": "x"})
        self.assertEqual(status, 403)
        self.db.grant_role(self.user["id"], 1, "approver", self.user["id"])
        status, _b = self.call("POST", f"/api/claims/{claim}/amend",
                               {"amount_cents": 1200000, "reason": "agreed"})
        self.assertEqual(status, 200)

    def test_generating_with_no_order_raised_yet(self):
        """On a job where orders arrive as the work does there may be none.
        A placeholder carries the claims until a real one appears, rather
        than the generate failing on a constraint."""
        self.build()
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM customer_po WHERE project_id = ?",
            (self.project_id,)), 0)
        result = self.db.generate_plan_claims(self.project_id, self.user["id"])
        self.assertEqual(result["created"], len(PLAN))
        self.assertEqual(self.db.scalar(
            """SELECT COUNT(*) FROM customer_po
               WHERE project_id = ? AND is_placeholder = 1""",
            (self.project_id,)), 1)
        # And it is not counted as ordered.
        self.assertEqual(self.db.scalar(
            "SELECT ordered_cents FROM v_project_orders_in_hand "
            "WHERE project_id = ?", (self.project_id,)), 0)

    def test_a_locked_month_is_409_not_500(self):
        items = self.build()
        self.db.generate_plan_claims(self.project_id, self.user["id"])
        self.invoice("Mar-26")
        status, body = self.call(
            "POST", f"/api/plan/items/{items['Verification of Design']}/allocate",
            {"period_id": self.period("Mar-26"), "percent_bp": 3000,
             "amount_cents": 1787490})
        self.assertEqual(status, 409)
        self.assertIn("amend the claim", body["error"])



if __name__ == "__main__":
    unittest.main(verbosity=2)
