"""Office expenses (STP-4).

What the business costs to run, on the same monthly axis as everything
else — so overhead can eventually meet revenue in one place.

ACCESS. Every route requires `finance`, which implies nothing and is
implied by nothing. These figures are wages: Justin's salary, Richard's
salary, everyone's superannuation. An administrator can grant the role,
which is what administering is, but granting is not having.
"""

from typing import Any

from ops import money
from ops.db import Db
from ops.http_util import HttpError, Router

#: Opens the screen: what the business costs to run.
ROLE = "finance"
#: Shows what people earn. A separate grant, because seeing the costs is
#: not seeing the salaries -- and reporting will need the first without the
#: second.
SALARY_ROLE = "payroll"
MAX_MONEY_CENTS = 100_000_000_00
STATES = ("VIC", "NSW", "QLD", "SA", "WA", "TAS", "NT", "ACT")
KINDS = ("wages", "super", "statutory", "expense")


def entity_ids(user: dict[str, Any]) -> list[int]:
    return sorted({r["entity_id"] for r in user["roles"]})


def money_field(raw, errors, key):
    try:
        amount = int(raw)
    except (TypeError, ValueError):
        errors[key] = "must be a whole number of cents"
        return 0
    if abs(amount) > MAX_MONEY_CENTS:
        errors[key] = f"looks like a typo (over ${MAX_MONEY_CENTS // 100:,})"
    return amount


def register(router: Router, db: Db) -> None:

    def elevated(handler, user):
        """Has this person re-authenticated in the last fifteen minutes?

        Checked on the SERVER, and salaries are withheld from the payload
        rather than hidden by the screen. A figure the browser is sent is a
        figure anyone with the developer tools already has -- hiding it in
        the interface hides it from nobody who matters.
        """
        from ops import auth
        token = auth.read_named_cookie(handler.headers.get("Cookie"),
                                       auth.ELEVATION_COOKIE)
        return auth.verify_elevation(router.session_key, token, user["id"])

    def may_see_salaries(user):
        return any(r["role"] == SALARY_ROLE for r in user.get("roles", []))

    def require_salary_access(handler, user):
        """Two gates, and they answer different questions. The ROLE is who
        may ever see a salary; the ELEVATION is whether they have just
        proved they are still at the keyboard."""
        if not may_see_salaries(user):
            raise HttpError(
                403, "the payroll role is required",
                {"why": "Seeing what the business costs is not seeing what "
                        "people earn. An administrator grants payroll "
                        "separately."})
        if not elevated(handler, user):
            raise HttpError(
                403, "re-authentication required",
                {"elevate": "/auth/elevate",
                 "why": "Salaries are shown only after signing in again, "
                        "and only for fifteen minutes."})


    def scoped(user):
        ids = entity_ids(user)
        if not ids:
            raise HttpError(403, "no entity access")
        return ids

    def owned_line(user, line_id):
        row = db.query_one("SELECT * FROM v_expense_line WHERE line_id = ?",
                           (line_id,))
        if row is None or row["entity_id"] not in entity_ids(user):
            raise HttpError(404, "not found")
        return row

    @router.route("/api/expenses", role=ROLE)
    def overview(handler, user):
        ids = scoped(user)
        marks = ",".join("?" * len(ids))
        # Both gates, because either alone leaves salaries in the payload
        # for somebody who should not have them.
        is_elevated = may_see_salaries(user) and elevated(handler, user)
        return 200, {
            "categories": db.query(
                f"""SELECT * FROM expense_category
                    WHERE entity_id IN ({marks})
                    ORDER BY sequence, name""", tuple(ids)),
            # `annual_cents` is REMOVED unless this person has just
            # re-authenticated. Not blanked on the screen: withheld from
            # the response, so it is not in the network tab either.
            "lines": [
                {k: v for k, v in dict(row).items()
                 if k != "annual_cents" or is_elevated}
                for row in db.query(
                    f"""SELECT * FROM v_expense_line
                        WHERE entity_id IN ({marks})
                        ORDER BY category_sequence, line_sequence, line_name""",
                    tuple(ids))],
            # Which lines HAVE a salary, so the screen can offer to reveal
            # one without knowing what it is.
            "salaried": [r["line_id"] for r in db.query(
                f"""SELECT DISTINCT l.line_id FROM v_expense_line l
                    WHERE l.entity_id IN ({marks})
                      AND l.annual_cents IS NOT NULL""", tuple(ids))],
            "elevated": is_elevated,
            "may_see_salaries": may_see_salaries(user),
            "amounts": db.query(
                f"""SELECT a.id, a.expense_line_id, a.period_id,
                           a.amount_cents, a.source
                    FROM expense_amount a
                    JOIN expense_line l ON l.id = a.expense_line_id
                    WHERE l.entity_id IN ({marks})""", tuple(ids)),
            # The revision history is the salary, month by month. Same
            # rule.
            "salaries": db.query(
                f"""SELECT r.id, r.expense_line_id, r.from_period_id,
                           r.annual_cents, r.note, pe.label AS from_label
                    FROM salary_revision r
                    JOIN expense_line l ON l.id = r.expense_line_id
                    JOIN period pe ON pe.id = r.from_period_id
                    WHERE l.entity_id IN ({marks})
                    ORDER BY pe.month_start""", tuple(ids))
                if is_elevated else [],
            "periods": db.query(
                """SELECT id, label, fy, fy_label, month_start FROM period
                   ORDER BY month_start"""),
            "wage_base": db.query(
                f"""SELECT * FROM v_wage_base WHERE entity_id IN ({marks})""",
                tuple(ids)),
            "states": list(STATES),
            "kinds": list(KINDS),
        }

    @router.route("/api/expenses/salary/{line_id}", role=ROLE)
    def one_salary(handler, user, line_id):
        """One salary, for one line, after a re-authentication.

        Separate from the list so that seeing a colleague's pay is a
        request someone made, which the audit log then holds.
        """
        line = owned_line(user, line_id)
        require_salary_access(handler, user)
        db.record_salary_view(line["line_id"], user["id"])
        return 200, {
            "line_id": line["line_id"], "line_name": line["line_name"],
            "annual_cents": line["annual_cents"],
            "revisions": db.query(
                """SELECT r.annual_cents, r.note, pe.label AS from_label
                   FROM salary_revision r
                   JOIN period pe ON pe.id = r.from_period_id
                   WHERE r.expense_line_id = ? ORDER BY pe.month_start""",
                (line["line_id"],)),
        }

    @router.route("/api/expenses/export", role=ROLE)
    def export(handler, user):
        """The matrix as CSV. Without salaries, unless elevated -- an
        export is the easiest way for a figure to leave the building."""
        ids = scoped(user)
        marks = ",".join("?" * len(ids))
        is_elevated = may_see_salaries(user) and elevated(handler, user)
        periods = db.query(
            """SELECT DISTINCT pe.id, pe.label, pe.month_start
               FROM expense_amount a JOIN period pe ON pe.id = a.period_id
               ORDER BY pe.month_start""")
        lines = db.query(
            f"""SELECT * FROM v_expense_line WHERE entity_id IN ({marks})
                ORDER BY category_sequence, line_sequence, line_name""",
            tuple(ids))
        amounts = {}
        for row in db.query(
                f"""SELECT a.expense_line_id, a.period_id, a.amount_cents
                    FROM expense_amount a
                    JOIN expense_line l ON l.id = a.expense_line_id
                    WHERE l.entity_id IN ({marks})""", tuple(ids)):
            amounts[(row["expense_line_id"], row["period_id"])] = \
                row["amount_cents"]
        import csv
        import io
        out = io.StringIO()
        writer = csv.writer(out)
        header = ["Category", "Line", "State", "Forecast", "Rate %"]
        if is_elevated:
            header.append("Annual salary")
        writer.writerow(header + [p["label"] for p in periods])
        for line in lines:
            row = [line["category_name"], line["line_name"],
                   line["state"] or "", "yes" if line["is_forecast"] else "",
                   f"{line['rate_bp'] / 10000:g}" if line["rate_bp"] else ""]
            if is_elevated:
                row.append(money.format(line["annual_cents"])
                           if line["annual_cents"] else "")
            for period in periods:
                cents = amounts.get((line["line_id"], period["id"]))
                row.append(money.format(cents) if cents else "")
            writer.writerow(row)
        writer.writerow([])
        writer.writerow(["Total", "", "", "", ""]
                        + ([""] if is_elevated else [])
                        + [money.format(sum(
                            amounts.get((l["line_id"], p["id"]), 0)
                            for l in lines)) for p in periods])
        body = out.getvalue().encode("utf-8-sig")
        handler._send(200, body, content_type="text/csv; charset=utf-8",
                      extra_headers={
                          "Content-Disposition":
                              'attachment; filename="office-expenses.csv"'})
        return None

    @router.route("/api/expenses/categories", role=ROLE, method="POST")
    def add_category(handler, user):
        ids = scoped(user)
        payload = handler.read_json()
        errors = {}
        name = (payload.get("name") or "").strip()
        if not name:
            errors["name"] = "required"
        kind = (payload.get("kind") or "expense").strip()
        if kind not in KINDS:
            errors["kind"] = f"one of {', '.join(KINDS)}"
        if errors:
            raise HttpError(400, "validation failed", errors)
        try:
            return 201, db.create_expense_category(
                ids[0], name, kind, user["id"])
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HttpError(409, f"there is already a category called "
                                     f"{name!r}")
            raise

    @router.route("/api/expenses/lines", role=ROLE, method="POST")
    def add_line(handler, user):
        ids = scoped(user)
        payload = handler.read_json()
        errors = {}
        category = db.query_one(
            "SELECT id, entity_id, kind FROM expense_category WHERE id = ?",
            (payload.get("category_id"),))
        if category is None or category["entity_id"] not in ids:
            errors["category_id"] = "required"
        name = (payload.get("name") or "").strip()
        if not name:
            errors["name"] = "required"
        state = (payload.get("state") or "").strip() or None
        if state and state not in STATES:
            errors["state"] = f"one of {', '.join(STATES)}"
        if errors or category is None:
            raise HttpError(400, "validation failed",
                            errors or {"category_id": "required"})
        try:
            return 201, db.create_expense_line({
                "entity_id": category["entity_id"],
                "category_id": category["id"], "name": name, "state": state,
                "is_forecast": 1 if payload.get("is_forecast") else 0,
                "rate_bp": payload.get("rate_bp"),
                "note": (payload.get("note") or "").strip() or None,
            }, user["id"])
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HttpError(409, f"that category already has a line "
                                     f"called {name!r}")
            raise

    @router.route("/api/expenses/lines/{line_id}", role=ROLE, method="PATCH")
    def update_line(handler, user, line_id):
        line = owned_line(user, line_id)
        payload = handler.read_json()
        fields, errors = {}, {}
        if "name" in payload:
            name = (payload["name"] or "").strip()
            if not name:
                errors["name"] = "required"
            else:
                fields["name"] = name
        if "state" in payload:
            state = (payload["state"] or "").strip() or None
            if state and state not in STATES:
                errors["state"] = f"one of {', '.join(STATES)}"
            else:
                fields["state"] = state
        for key in ("is_forecast", "is_active"):
            if key in payload:
                fields[key] = 1 if payload[key] else 0
        if "note" in payload:
            fields["note"] = (payload["note"] or "").strip() or None
        # Rates change: Work Cover is reassessed yearly and payroll tax
        # moves with the state budget. Entered as a PERCENTAGE, because
        # that is what the letter from the insurer says, and stored in
        # hundredths of a basis point so 1.785% and 0.39% both survive.
        if "rate_percent" in payload:
            try:
                percent = float(payload["rate_percent"])
            except (TypeError, ValueError):
                errors["rate_percent"] = "a percentage, e.g. 1.785"
            else:
                if percent < 0 or percent > 100:
                    errors["rate_percent"] = "between 0 and 100"
                else:
                    fields["rate_bp"] = Db.rate(percent)
        if "threshold_annual_cents" in payload:
            fields["threshold_annual_cents"] = money_field(
                payload["threshold_annual_cents"] or 0, errors,
                "threshold_annual_cents")
        if "category_id" in payload:
            target = db.query_one(
                "SELECT id, entity_id FROM expense_category WHERE id = ?",
                (payload["category_id"],))
            if target is None or target["entity_id"] != line["entity_id"]:
                errors["category_id"] = "not a category on this entity"
            else:
                fields["category_id"] = target["id"]
        if errors:
            raise HttpError(400, "validation failed", errors)
        result = db.update_expense_line(line_id, fields, user["id"])
        if result is None:
            raise HttpError(404, "not found")
        # A rate that changes and does not reach the months is a rate
        # nobody has changed. The sheet's payroll tax was stale for
        # twenty-one months for exactly this reason.
        touched = 0
        if {"rate_bp", "threshold_annual_cents", "state"} & set(result["changed"]):
            touched = db.recompute_derived(line["entity_id"], user["id"])
        return 200, {**result, "recomputed": touched}

    @router.route("/api/expenses/amounts", role=ROLE, method="POST")
    def set_amount(handler, user):
        """One month of one line. Setting it to nothing removes it: a month
        a line does not run in should be absent, not zero."""
        payload = handler.read_json()
        line = owned_line(user, payload.get("line_id"))
        errors = {}
        period_id = db.scalar("SELECT id FROM period WHERE id = ?",
                              (payload.get("period_id"),))
        if period_id is None:
            errors["period_id"] = "required"
        amount = money_field(payload.get("amount_cents", 0), errors,
                             "amount_cents")
        if errors:
            raise HttpError(400, "validation failed", errors)
        result = db.set_expense_amount(
            line["line_id"], period_id, amount, user["id"],
            (payload.get("reason") or "").strip() or None)
        # A wage typed directly still moves super and the statutory lines.
        touched = 0
        if line["category_kind"] in ("wages", "super"):
            touched = db.recompute_derived(line["entity_id"], user["id"],
                                           period_id)
        return 200, {**result, "recomputed": touched}

    @router.route("/api/expenses/salaries", role=ROLE, method="POST")
    def set_salary(handler, user):
        """An annual salary from a month. A rise is a NEW revision, not an
        edit: what somebody earned last year is a fact about last year.

        Every month from that one to the end of the known periods is
        recomputed, because a salary is the fact and the months are its
        consequence.
        """
        require_salary_access(handler, user)
        payload = handler.read_json()
        line = owned_line(user, payload.get("line_id"))
        if line["category_kind"] != "wages":
            raise HttpError(409, "a salary belongs to a wages line")
        errors = {}
        period_id = db.scalar("SELECT id FROM period WHERE id = ?",
                              (payload.get("from_period_id"),))
        if period_id is None:
            errors["from_period_id"] = "required"
        annual = money_field(payload.get("annual_cents", 0), errors,
                             "annual_cents")
        if annual < 0:
            errors["annual_cents"] = "cannot be negative"
        if errors:
            raise HttpError(400, "validation failed", errors)
        result = db.set_salary(
            line["line_id"], period_id, annual, user["id"],
            (payload.get("note") or "").strip() or None)
        # Super follows the wage and the statutory charges follow both, so a
        # raise moves five other lines. Doing it here is what stops anyone
        # having to remember.
        touched = db.recompute_derived(line["entity_id"], user["id"],
                                       period_id)
        return 200, {**result, "recomputed": touched}
