"""Compare the Project List tab against the platform (ADR-27).

    python3 tools/drift_check.py --csv register.csv --db /data/ops.db

The plan's original control was to make each workbook tab read-only once its
phase shipped. That was refused for the Project List while the platform does
not yet do everything the workbook does -- a reasonable call, but the control
existed for a real reason, so it is replaced rather than dropped.

**The risk was never editing the workbook. It was the two diverging in
silence.** This makes divergence visible within a day instead of at STP-5,
when a dashboard figure fails to match and nobody can say when it stopped.

Three things this deliberately does NOT do:

* It does not judge which side is right. It reports that they differ and
  leaves the decision to a human, because either side can legitimately be
  the newer one.
* It does not treat a platform-issued job number as drift. Once the worklist
  turns `TBA` into `JN-6889`, the workbook is simply stale on a field the
  platform now owns (STP-1). Reporting that would bury the real findings
  under expected ones, which is how a check gets ignored.
* It does not write anything. A checker that repairs what it finds is a
  second, unreviewed import path.

Exit code 1 when there is actionable drift, so it can be scheduled.
"""

import argparse
import csv
import os
import sqlite3
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..")))
from ops import money  # noqa: E402

# Fields the platform now owns outright. A difference here is the workbook
# being stale, not drift.
PLATFORM_OWNED = ("job_code",)

PLACEHOLDERS = {"TBA", "NA", "N/A", "VARIOUS", "TBC", "TBD", "-", ""}

COMPARE = [
    ("client", "Client", "text"),
    ("type", "Type", "text"),
    ("status", "Status", "text"),
    ("project_lead", "Project Lead", "text"),
    ("purchase_order_cents", "Purchase Order", "money"),
    ("invoiced_prior_cents", "Invoiced Prior", "money"),
]


def read_workbook(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("Project") or "").strip()]
    if not rows:
        raise SystemExit(f"no project rows in {path}")
    return {r["Project"].strip(): r for r in rows}


def read_platform(db_path, entity_id=1):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT p.name, p.job_code, p.status, p.project_lead,
                      p.purchase_order_cents, p.invoiced_prior_cents,
                      COALESCE(pt.code, '') AS type,
                      COALESCE(c.name, '')  AS client
               FROM project p
               LEFT JOIN project_type pt ON pt.id = p.type_id
               LEFT JOIN client c ON c.id = p.client_id
               WHERE p.entity_id = ?""", (entity_id,)).fetchall()
    finally:
        conn.close()
    return {r["name"]: dict(r) for r in rows}


def compare(workbook, platform):
    """Match on PROJECT NAME, not job code.

    The job code is the obvious key and the wrong one: the platform reissues
    it, so every resolved worklist row would report as a mismatch against a
    workbook that has not caught up.
    """
    findings = {"missing_in_platform": [], "missing_in_workbook": [],
                "field_drift": [], "workbook_stale": []}

    for name in sorted(set(workbook) - set(platform)):
        findings["missing_in_platform"].append(name)
    for name in sorted(set(platform) - set(workbook)):
        findings["missing_in_workbook"].append(name)

    for name in sorted(set(workbook) & set(platform)):
        wb, pf = workbook[name], platform[name]

        wb_code = (wb.get("Job Code") or "").strip()
        if wb_code != pf["job_code"]:
            if wb_code.upper() in PLACEHOLDERS:
                findings["workbook_stale"].append(
                    (name, "job_code", wb_code, pf["job_code"]))
            else:
                findings["field_drift"].append(
                    (name, "job_code", wb_code, pf["job_code"]))

        for key, column, kind in COMPARE:
            raw = (wb.get(column) or "").strip()
            if kind == "money":
                try:
                    left = money.parse(raw)
                except money.MoneyError:
                    findings["field_drift"].append(
                        (name, column, raw, "unparseable"))
                    continue
                right = pf[key] or 0
                if left != right:
                    findings["field_drift"].append(
                        (name, column, money.format(left), money.format(right)))
            else:
                right = (pf[key] or "").strip()
                # Case and spacing differences are not drift worth a human's
                # attention; a different value is.
                if raw.casefold().strip() != right.casefold().strip():
                    findings["field_drift"].append((name, column, raw, right))
    return findings


def report(findings, workbook, platform):
    out = ["", f"  workbook rows  {len(workbook)}",
           f"  platform rows  {len(platform)}", ""]
    actionable = 0

    if findings["missing_in_platform"]:
        actionable += len(findings["missing_in_platform"])
        out.append("  IN THE WORKBOOK, NOT IN THE PLATFORM")
        out += [f"    {n}" for n in findings["missing_in_platform"]]
        out.append("")
    if findings["missing_in_workbook"]:
        actionable += len(findings["missing_in_workbook"])
        out.append("  IN THE PLATFORM, NOT IN THE WORKBOOK")
        out += [f"    {n}" for n in findings["missing_in_workbook"]]
        out.append("")
    if findings["field_drift"]:
        actionable += len(findings["field_drift"])
        out.append("  DIFFERENT VALUES  (workbook | platform)")
        for name, field, left, right in findings["field_drift"]:
            out.append(f"    {name[:38]:38s} {field:16s} {left!s:>16} | {right!s:>16}")
        out.append("")
    if findings["workbook_stale"]:
        out.append("  workbook stale on a field the platform owns "
                   "(expected, no action)")
        for name, field, left, right in findings["workbook_stale"]:
            out.append(f"    {name[:38]:38s} {field:16s} {left!s:>16} -> {right!s:>16}")
        out.append("")

    out.append("  no drift" if actionable == 0
               else f"  {actionable} difference(s) need a decision")
    out.append("")
    return "\n".join(out), actionable


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True,
                    help="Project List tab exported as CSV")
    ap.add_argument("--db", required=True)
    ap.add_argument("--entity", type=int, default=1)
    args = ap.parse_args(argv)

    workbook = read_workbook(args.csv)
    platform = read_platform(args.db, args.entity)
    findings = compare(workbook, platform)
    text, actionable = report(findings, workbook, platform)
    print(text)
    return 1 if actionable else 0


if __name__ == "__main__":
    sys.exit(main())
