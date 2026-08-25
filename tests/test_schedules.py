"""Recurring claim schedules and renewals (migration 005).

Maintenance is one agreement spread over a year, not twelve claims someone
typed. `36 Wellington` is $22,689 as twelve payments of $1,890.75 — entering
those by hand is the monthly copy-forward ritual in another guise.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from ops.db import Db  # noqa: E402

MIGRATIONS = os.path.join(ROOT, "ops", "migrations")

MONTHLY = 189075        # $1,890.75
ANNUAL = 2268900        # $22,689.00 — the PO


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Db(os.path.join(self.dir, "ops.db"), MIGRATIONS)
        self.db.migrate()
        self.user = self.db.upsert_user("s1", "r@x", "R")
        with self.db._tx() as c:
            c.execute("INSERT INTO client (entity_id,name) VALUES (1,'Hines')")
            c.execute("""INSERT INTO project (entity_id,name,job_code,status,created_ts)
                         VALUES (1,'36 Wellington','JN-3579','SLA',0)""")
        self.project_id = self.db.scalar(
            "SELECT id FROM project WHERE job_code='JN-3579'")
        with self.db._tx() as c:
            c.execute("""INSERT INTO customer_po (entity_id,project_id,
                             amount_cents,created_ts) VALUES (1,?,?,0)""",
                      (self.project_id, ANNUAL))
        self.po_id = self.db.scalar(
            "SELECT id FROM customer_po WHERE project_id=?", (self.project_id,))
        self.jul26 = self.period("2026-07-01")
        self.aug26 = self.period("2026-08-01")
        self.jun27 = self.period("2027-06-01")

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def period(self, month_start):
        return self.db.scalar(
            "SELECT id FROM period WHERE month_start = ?", (month_start,))

    def schedule(self, **over):
        fields = {"entity_id": 1, "project_id": self.project_id,
                  "customer_po_id": self.po_id, "description": "Maintenance",
                  "amount_cents": MONTHLY, "frequency": "monthly",
                  "start_period_id": self.jul26, "end_period_id": self.jun27}
        fields.update(over)
        return self.db.create_schedule(fields, self.user["id"])


class TestGeneration(Case):
    def test_a_year_of_monthly_maintenance(self):
        s = self.schedule()
        result = self.db.generate_schedule_claims(s["id"], self.user["id"])
        self.assertEqual(result["created"], 12)
        self.assertEqual(
            self.db.scalar("SELECT SUM(amount_cents) FROM claim_line "
                           "WHERE schedule_id = ?", (s["id"],)), ANNUAL)

    def test_it_reconciles_to_the_po(self):
        """$1,890.75 x 12 is the contract, exactly. If generation and the PO
        disagree, one of them is wrong before anyone has invoiced anything."""
        s = self.schedule()
        self.db.generate_schedule_claims(s["id"], self.user["id"])
        cover = self.db.query_one(
            "SELECT * FROM v_schedule_coverage WHERE schedule_id = ?", (s["id"],))
        self.assertEqual(cover["generated_cents"], ANNUAL)
        self.assertEqual(cover["generated_cents"],
                         self.db.scalar("SELECT amount_cents FROM customer_po "
                                        "WHERE id = ?", (self.po_id,)))

    def test_generation_is_idempotent(self):
        """Safe to run on a timer, on demand, or by accident."""
        s = self.schedule()
        first = self.db.generate_schedule_claims(s["id"], self.user["id"])
        second = self.db.generate_schedule_claims(s["id"], self.user["id"])
        self.assertEqual(first["created"], 12)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["existing"], 12)
        self.assertEqual(self.db.scalar(
            "SELECT COUNT(*) FROM claim_line WHERE schedule_id = ?", (s["id"],)), 12)

    def test_the_database_refuses_a_duplicate_even_by_hand(self):
        """The index is the guarantee; the method's check is a courtesy."""
        s = self.schedule()
        self.db.generate_schedule_claims(s["id"], self.user["id"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db._write.execute(
                """INSERT INTO claim_line (entity_id,project_id,customer_po_id,
                       period_id,status,amount_cents,schedule_id,created_ts)
                   VALUES (1,?,?,?, 'forecast',?,?,0)""",
                (self.project_id, self.po_id, self.jul26, MONTHLY, s["id"]))

    def test_quarterly_steps_from_the_start_not_calendar_quarters(self):
        """An agreement beginning in August bills Aug, Nov, Feb, May.
        Snapping to Jan/Apr/Jul/Oct would invent a billing date nobody
        agreed to."""
        s = self.schedule(frequency="quarterly", start_period_id=self.aug26,
                          amount_cents=760833)
        result = self.db.generate_schedule_claims(s["id"], self.user["id"])
        self.assertEqual(result["periods"],
                         ["Aug-26", "Nov-26", "Feb-27", "May-27"])

    def test_annual_generates_one(self):
        s = self.schedule(frequency="annual", amount_cents=ANNUAL)
        self.assertEqual(
            self.db.generate_schedule_claims(s["id"], self.user["id"])["created"], 1)

    def test_generated_claims_are_ordinary_forecasts(self):
        """Editable, able to slip, invoiced like anything else. Only their
        origin is recorded."""
        s = self.schedule()
        self.db.generate_schedule_claims(s["id"], self.user["id"])
        row = self.db.query_one(
            "SELECT * FROM claim_line WHERE schedule_id = ?", (s["id"],))
        self.assertEqual(row["status"], "forecast")
        self.assertIsNotNone(row["period_id"])
        self.assertIsNotNone(row["customer_po_id"])

    def test_an_inactive_schedule_generates_nothing(self):
        s = self.schedule()
        with self.db._tx() as c:
            c.execute("UPDATE claim_schedule SET is_active = 0 WHERE id = ?",
                      (s["id"],))
        self.assertEqual(
            self.db.generate_schedule_claims(s["id"], self.user["id"])["created"], 0)

    def test_generation_is_audited(self):
        s = self.schedule()
        self.db.generate_schedule_claims(s["id"], self.user["id"])
        row = self.db.query_one(
            "SELECT detail FROM audit_log WHERE action='schedule_generate'")
        self.assertIn("12 claims", row["detail"])
        self.assertIn("Jul-26", row["detail"])


class TestRenewals(Case):
    def test_a_future_renewal_is_reported_as_future(self):
        self.schedule(renewal_date="2099-01-01")
        row = self.db.upcoming_renewals(entity_ids=[1])[0]
        self.assertEqual(row["renewal_state"], "future")
        self.assertGreater(row["days_until"], 0)

    def test_a_renewal_inside_the_notice_period_is_due(self):
        import datetime
        soon = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        self.schedule(renewal_date=soon, renewal_notice_days=60)
        self.assertEqual(
            self.db.upcoming_renewals(entity_ids=[1])[0]["renewal_state"], "due")

    def test_a_passed_renewal_stays_in_the_list_as_overdue(self):
        """A maintenance agreement that lapsed last month is more urgent than
        one due next month. Dropping it off the end is how revenue quietly
        stops."""
        self.schedule(renewal_date="2020-01-01")
        rows = self.db.upcoming_renewals(entity_ids=[1])
        self.assertEqual(rows[0]["renewal_state"], "overdue")
        self.assertLess(rows[0]["days_until"], 0)

    def test_overdue_sorts_before_due(self):
        import datetime
        soon = (datetime.date.today() + datetime.timedelta(days=10)).isoformat()
        self.schedule(renewal_date=soon, description="soon")
        self.schedule(renewal_date="2020-01-01", description="lapsed")
        states = [r["renewal_state"] for r in self.db.upcoming_renewals([1])]
        self.assertEqual(states[0], "overdue")

    def test_a_schedule_with_no_renewal_date_is_not_reported(self):
        """Silence is better than a row saying 'unknown' every time someone
        opens the list."""
        self.schedule(renewal_date=None)
        self.assertEqual(self.db.upcoming_renewals(entity_ids=[1]), [])

    def test_an_inactive_schedule_is_not_chased(self):
        s = self.schedule(renewal_date="2099-01-01")
        with self.db._tx() as c:
            c.execute("UPDATE claim_schedule SET is_active = 0 WHERE id = ?",
                      (s["id"],))
        self.assertEqual(self.db.upcoming_renewals(entity_ids=[1]), [])

    def test_within_days_narrows_the_list(self):
        import datetime
        soon = (datetime.date.today() + datetime.timedelta(days=20)).isoformat()
        self.schedule(renewal_date=soon, description="soon")
        self.schedule(renewal_date="2099-01-01", description="far off")
        rows = self.db.upcoming_renewals(entity_ids=[1], within_days=90)
        self.assertEqual([r["description"] for r in rows], ["soon"])

    def test_the_renewal_note_survives(self):
        self.schedule(renewal_date="2099-01-01",
                      renewal_note="confirm licence count first")
        self.assertEqual(self.db.scalar(
            "SELECT renewal_note FROM claim_schedule"),
            "confirm licence count first")


class TestMigrationIsExpandOnly(unittest.TestCase):
    def test_005_only_adds(self):
        with open(os.path.join(MIGRATIONS, "005_schedules.sql"),
                  encoding="utf-8") as f:
            body = f.read().upper()
        self.assertNotIn("DROP TABLE", body)
        self.assertNotIn("DROP COLUMN", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
