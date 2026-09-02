"""Operations dashboard (STP-5).

What the business is worth, what it costs, and what is left. The screen the
workbook chain was doing badly, and the reason for all of this.

    revenue        claims, billed and forecast
  - project cost   procurement, committed and estimated
  - office cost    wages, overhead, the statutory charges
  = gross profit
  - corporate tax  ON THE YEAR, not the month
  = net profit

OFFICE COST DOES NOT ATTACH TO A PROJECT. Rent and payroll tax are not
bought for a job, and spreading them across jobs would invent a margin
nobody agreed to. They come off the bottom line.

TAX IS ANNUAL. The sheet taxed each profitable month and gave no credit for
loss months: $267,227 against $104,647 of gross profit, and a headline net
profit of -$162,580. Assessed on the year it is $26,162 and +$78,485. A
quarter of a million apart, and the second is how company tax works.

SALARIES ARE NOT HERE. The office figure is a total; nothing on this screen
identifies a person, so `finance` is enough and `payroll` is not needed.
"""

import time
from typing import Any

from ops.db import Db
from ops.http_util import HttpError, Router

ROLE_READ = "finance"
ROLE_WRITE = "admin"


def entity_ids(user: dict[str, Any]) -> list[int]:
    return sorted({r["entity_id"] for r in user["roles"]})


def register(router: Router, db: Db) -> None:

    def scoped(user):
        ids = entity_ids(user)
        if not ids:
            raise HttpError(403, "no entity access")
        return ids

    @router.route("/api/dashboard", role=ROLE_READ)
    def dashboard(handler, user):
        ids = scoped(user)
        marks = ",".join("?" * len(ids))
        today = time.strftime("%Y-%m-%d")

        months = {}
        for row in db.query(
                """SELECT period_id, period_label, fy, fy_label, month_start,
                          invoiced_cents, forecast_cents, total_cents
                   FROM v_month_revenue ORDER BY month_start"""):
            months[row["period_id"]] = {
                "period_id": row["period_id"], "label": row["period_label"],
                "fy": row["fy"], "fy_label": row["fy_label"],
                "month_start": row["month_start"],
                "invoiced_cents": row["invoiced_cents"],
                "forecast_cents": row["forecast_cents"],
                "revenue_cents": row["total_cents"],
                "project_cost_cents": 0, "estimated_cost_cents": 0,
                "office_cost_cents": 0,
            }
        for row in db.query("SELECT * FROM v_month_project_cost"):
            if row["period_id"] in months:
                months[row["period_id"]]["project_cost_cents"] = \
                    row["total_cents"]
                months[row["period_id"]]["estimated_cost_cents"] = \
                    row["estimated_cents"]
        for row in db.query("SELECT * FROM v_month_office_cost"):
            if row["period_id"] in months:
                months[row["period_id"]]["office_cost_cents"] = \
                    row["total_cents"]

        # A month that has ENDED is actual; one still running or ahead is a
        # projection. Said explicitly, because a dashboard that mixes them
        # silently is a dashboard nobody can act on.
        for month in months.values():
            month["total_cost_cents"] = (month["project_cost_cents"]
                                         + month["office_cost_cents"])
            month["gross_profit_cents"] = (month["revenue_cents"]
                                           - month["total_cost_cents"])
            month["is_actual"] = month["month_start"] <= today
        ordered = sorted(months.values(), key=lambda m: m["month_start"])
        live = [m for m in ordered
                if m["revenue_cents"] or m["total_cost_cents"]]

        settings = {row["fy"]: dict(row) for row in db.query(
            f"SELECT * FROM fy_settings WHERE entity_id IN ({marks})",
            tuple(ids))}

        years = {}
        for month in live:
            year = years.setdefault(month["fy"], {
                "fy": month["fy"], "fy_label": month["fy_label"],
                "invoiced_cents": 0, "forecast_cents": 0, "revenue_cents": 0,
                "project_cost_cents": 0, "estimated_cost_cents": 0,
                "office_cost_cents": 0, "total_cost_cents": 0,
            })
            for key in ("invoiced_cents", "forecast_cents", "revenue_cents",
                        "project_cost_cents", "estimated_cost_cents",
                        "office_cost_cents", "total_cost_cents"):
                year[key] += month[key]
        for year in years.values():
            setting = settings.get(year["fy"], {})
            rate = setting.get("tax_rate_bp", 250000)
            further = setting.get("further_sales_cents", 0)
            year["tax_rate_bp"] = rate
            year["further_sales_cents"] = further
            year["gross_profit_cents"] = (year["revenue_cents"]
                                          - year["total_cost_cents"])
            # ON THE YEAR, and a year that loses money pays nothing. No
            # carry-forward: a forecast that assumes relief it has not
            # claimed is a forecast that flatters itself.
            taxable = max(0, year["gross_profit_cents"])
            year["corporate_tax_cents"] = Db.rate_amount(taxable, rate)
            year["net_profit_cents"] = (year["gross_profit_cents"]
                                        - year["corporate_tax_cents"])
            year["planned_revenue_cents"] = year["revenue_cents"] + further

        return 200, {
            "months": live,
            "years": sorted(years.values(), key=lambda y: y["fy"]),
            "today": today,
            "projects": db.query(
                f"""SELECT p.id, p.name, p.job_code, p.status,
                           t.code AS type_code,
                           v.contract_value_cents, v.invoiced_prior_cents,
                           v.orders_in_hand_cents,
                           COALESCE(c.committed_cents, 0) AS committed_cents,
                           COALESCE(c.estimated_cents, 0) AS estimated_cents
                    FROM project p
                    JOIN v_project_orders_in_hand v ON v.project_id = p.id
                    LEFT JOIN project_type t ON t.id = p.type_id
                    LEFT JOIN v_project_procurement c ON c.project_id = p.id
                    WHERE p.entity_id IN ({marks})
                    ORDER BY p.name""", tuple(ids)),
            # Project by month, which is the table people actually read
            # when they ask where the year's revenue comes from. Only
            # projects with something in them: sixty-five rows of which
            # forty are empty is a table nobody scans.
            "project_months": db.query(
                f"""SELECT cl.project_id, cl.period_id,
                           SUM(cl.amount_cents) AS amount_cents,
                           SUM(CASE WHEN cl.status IN ('invoiced','paid')
                                    THEN cl.amount_cents ELSE 0 END)
                               AS invoiced_cents
                    FROM claim_line cl
                    JOIN project p ON p.id = cl.project_id
                    WHERE p.entity_id IN ({marks})
                      AND cl.is_opening_balance = 0
                      AND cl.period_id IS NOT NULL
                    GROUP BY cl.project_id, cl.period_id""", tuple(ids)),
            "expense_categories": db.query(
                f"""SELECT c.name, c.kind, pe.fy_label,
                           SUM(a.amount_cents) AS total_cents,
                           COUNT(DISTINCT l.id) AS line_count
                    FROM expense_amount a
                    JOIN expense_line l ON l.id = a.expense_line_id
                    JOIN expense_category c ON c.id = l.category_id
                    JOIN period pe ON pe.id = a.period_id
                    WHERE l.entity_id IN ({marks})
                    GROUP BY c.id, pe.fy_label
                    ORDER BY c.sequence""", tuple(ids)),
            # The year we are IN, so the screen lands on it rather than on
            # whichever happens to sort first. The Australian financial
            # year runs July to June, so anything from July belongs to the
            # year named for the following calendar year.
            "current_fy": (int(today[:4]) + 1 if int(today[5:7]) >= 7
                           else int(today[:4])),
            # `is_open`, not the literal `Active`: DLP and SLA are work
            # still going on, and `Lost` and `Complete` are both finished
            # in very different ways. The lookup decides, so adding a
            # status does not mean finding every count that hard-coded one.
            "active_projects": db.scalar(
                f"""SELECT COUNT(*) FROM project p
                    JOIN project_status s ON s.code = p.status
                    WHERE p.entity_id IN ({marks}) AND s.is_open = 1""",
                tuple(ids)) or 0,
            "staff_count": db.scalar(
                f"""SELECT COUNT(*) FROM expense_line l
                    JOIN expense_category c ON c.id = l.category_id
                    WHERE c.kind = 'wages' AND l.is_active = 1
                      AND l.is_forecast = 0 AND l.entity_id IN ({marks})""",
                tuple(ids)) or 0,
            "settings": sorted(settings.values(), key=lambda s: s["fy"]),
        }

    @router.route("/api/dashboard/settings", role=ROLE_WRITE, method="POST")
    def set_settings(handler, user):
        """The tax rate and the further-sales overlay, per year.

        Admin, not finance: these change what every figure on the screen
        means, and a rate somebody can quietly move is a rate nobody can
        rely on. Every change is audited.
        """
        ids = scoped(user)
        payload = handler.read_json()
        errors = {}
        try:
            fy = int(payload.get("fy"))
        except (TypeError, ValueError):
            errors["fy"] = "required"
            fy = 0
        rate = None
        if "tax_rate_percent" in payload:
            try:
                percent = float(payload["tax_rate_percent"])
            except (TypeError, ValueError):
                errors["tax_rate_percent"] = "a percentage, e.g. 25"
            else:
                if percent < 0 or percent > 100:
                    errors["tax_rate_percent"] = "between 0 and 100"
                else:
                    rate = Db.rate(percent)
        further = None
        if "further_sales_cents" in payload:
            try:
                further = int(payload["further_sales_cents"])
            except (TypeError, ValueError):
                errors["further_sales_cents"] = "whole cents"
            else:
                if further < 0:
                    errors["further_sales_cents"] = "cannot be negative"
        if errors:
            raise HttpError(400, "validation failed", errors)
        return 200, db.set_fy_settings(
            ids[0], fy, rate, further, user["id"],
            (payload.get("note") or "").strip() or None)
