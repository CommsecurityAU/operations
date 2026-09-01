// expenseforms.js — the dialogs behind the expense grid.
//
// Loaded on demand: the grid is what most visits want.

import { api, fmt, h, moneyInput, toCents } from "./app.js";
import { field, sheet } from "./sheet.js";

// A month of a line. Typing a figure into a calculated cell OVERRIDES the
// calculation from then on, so it says so rather than quietly winning.
export function amountDialog(line, period, existing, data, onDone) {
  const derived = existing && existing.source !== "entered";
  const controls = {
    amount_cents: field("Amount",
      moneyInput(existing ? existing.amount_cents : 0,
                 { "aria-label": "Amount" }),
      derived
        ? "This month is calculated. Typing a figure here replaces the "
          + "calculation for this month only, and keeps it."
        : "Leave it at nothing to clear the month"),
    reason: field("Why", h("input", { type: "text", "aria-label": "Why" }),
      "Kept with the change"),
  };
  sheet(`${line.line_name} \u00b7 ${period.label}`,
    `${line.category_name}`
    + (derived ? " \u00b7 currently calculated" : ""),
    { fields: Object.values(controls).map((c) => c.wrap),
      controls: Object.values(controls), byKey: controls },
    "Save",
    async () => {
      const cents = toCents(controls.amount_cents.control.value);
      if (cents === null) {
        controls.amount_cents.setError("not an amount");
        throw new Error("");
      }
      await api("POST", "/api/expenses/amounts", {
        line_id: line.line_id, period_id: period.id, amount_cents: cents,
        reason: controls.reason.control.value.trim(),
      });
      onDone(null, cents
        ? `${line.line_name}: ${period.label} set to ${fmt.money(cents)}`
        : `${line.line_name}: ${period.label} cleared`);
    });
}

// An annual salary from a month. A rise is a NEW revision, not an edit:
// what somebody earned last year is a fact about last year.
export function salaryDialog(line, data, onDone) {
  const from = h("select", { "aria-label": "From" },
    data.periods.map((p) => h("option", { value: String(p.id) },
      `${p.label}  ${p.fy_label}`)));
  const controls = {
    from_period_id: field("From", from,
      "Every month from here on is recalculated"),
    annual_cents: field("Annual salary",
      moneyInput(line.annual_cents || 0, { "aria-label": "Annual salary" }),
      "The monthly figure is a twelfth of this"),
    note: field("Note", h("input", { type: "text", "aria-label": "Note" })),
  };
  const history = (data.salaries || [])
    .filter((r) => r.expense_line_id === line.line_id)
    .map((r) => h("li", null,
      `${r.from_label}: ${fmt.money(r.annual_cents)}`));

  sheet(`${line.line_name} \u00b7 salary`,
    line.annual_cents ? `${fmt.money(line.annual_cents)} a year` : "",
    { fields: [
        ...Object.values(controls).map((c) => c.wrap),
        history.length
          ? h("div", { class: "field" },
              h("span", { class: "label" }, "So far"),
              h("ul", { class: "muted note" }, history))
          : null,
      ].filter(Boolean),
      controls: Object.values(controls), byKey: controls },
    "Set salary",
    async () => {
      const cents = toCents(controls.annual_cents.control.value);
      if (cents === null) {
        controls.annual_cents.setError("not an amount");
        throw new Error("");
      }
      const result = await api("POST", "/api/expenses/salaries", {
        line_id: line.line_id,
        from_period_id: Number(from.value),
        annual_cents: cents,
        note: controls.note.control.value.trim(),
      });
      onDone(null, `${line.line_name}: ${fmt.money(cents)} a year, `
                   + `${result.months_updated} month(s) recalculated`
                   + (result.recomputed
                      ? `, ${result.recomputed} figure(s) followed` : ""));
    });
}

export function lineDialog(line, data, onDone) {
  const isNew = !line;
  const row = line || {};
  const category = h("select", { "aria-label": "Category" },
    data.categories.map((c) => h("option",
      { value: String(c.id), selected: c.id === row.category_id }, c.name)));
  const state = h("select", { "aria-label": "State" },
    h("option", { value: "" }, "(not a person, or unstated)"),
    data.states.map((s) => h("option",
      { value: s, selected: s === row.state }, s)));

  const controls = {
    category_id: field("Category", category),
    name: field("Name",
      h("input", { type: "text", value: row.line_name || "",
                   "aria-label": "Name" })),
    state: field("State", state,
      "Work Cover and payroll tax are state schemes at different rates"),
    is_forecast: field("Forecast",
      (() => {
        const box = h("input", { type: "checkbox",
                                 "aria-label": "Forecast" });
        box.checked = Boolean(row.is_forecast);
        return box;
      })(),
      "Real for planning, not yet real for paying"),
    note: field("Note",
      h("input", { type: "text", value: row.note || "",
                   "aria-label": "Note" })),
  };
  // A rate only means something on a line that has one.
  if (!isNew && row.rate_bp) {
    controls.rate_percent = field("Rate",
      h("input", { type: "number", step: "0.001", min: "0", max: "100",
                   value: String(row.rate_bp / 10000),
                   "aria-label": "Rate" }),
      "A percentage. Changing it recalculates every month.");
    if (row.threshold_annual_cents) {
      controls.threshold_annual_cents = field("Annual reduction",
        moneyInput(row.threshold_annual_cents,
                   { "aria-label": "Annual reduction" }),
        "Taken off the year before the rate applies");
    }
  }

  sheet(isNew ? "New expense line" : row.line_name,
    isNew ? "" : row.category_name,
    { fields: Object.values(controls).map((c) => c.wrap),
      controls: Object.values(controls), byKey: controls },
    isNew ? "Add line" : "Save",
    async () => {
      const payload = {
        category_id: Number(category.value),
        name: controls.name.control.value.trim(),
        state: state.value || null,
        is_forecast: controls.is_forecast.control.checked,
        note: controls.note.control.value.trim(),
      };
      if (controls.rate_percent) {
        payload.rate_percent = Number(controls.rate_percent.control.value);
      }
      if (controls.threshold_annual_cents) {
        payload.threshold_annual_cents =
          toCents(controls.threshold_annual_cents.control.value);
      }
      if (isNew) {
        await api("POST", "/api/expenses/lines", payload);
        onDone(null, `Added ${payload.name}`);
      } else {
        const result = await api("PATCH",
                                 `/api/expenses/lines/${row.line_id}`, payload);
        onDone(null, `${payload.name} updated`
                     + (result.recomputed
                        ? `, ${result.recomputed} figure(s) recalculated` : ""));
      }
    });
}

export function categoryDialog(data, onDone) {
  const kind = h("select", { "aria-label": "Kind" },
    [["expense", "An ordinary cost"],
     ["wages", "Wages \u2014 drives super and the statutory charges"],
     ["super", "Superannuation"],
     ["statutory", "Work Cover or payroll tax"]].map(
       ([value, label]) => h("option", { value }, label)));
  const controls = {
    name: field("Name", h("input", { type: "text", "aria-label": "Name" })),
    kind: field("Kind", kind,
      "What it means, not what it is called \u2014 a category someone "
      + "renames should not change what it does"),
  };
  sheet("New category", "",
    { fields: Object.values(controls).map((c) => c.wrap),
      controls: Object.values(controls), byKey: controls },
    "Add category",
    async () => {
      const made = await api("POST", "/api/expenses/categories", {
        name: controls.name.control.value.trim(),
        kind: kind.value,
      });
      onDone(made, `Added ${made.name}`);
    });
}

// Not a dialog that shows anything: a dialog that explains why it cannot,
// and offers the one thing that would.
export function revealDialog(line, onDone) {
  sheet(`${line.line_name} \u00b7 salary`,
    "not sent to this page",
    { fields: [
        h("p", { class: "muted note" },
          "Salaries are withheld from this screen rather than hidden on it, "
          + "so this figure is not in the page and not in the network tab "
          + "either."),
        h("p", { class: "muted note" },
          "Signing in again reveals them for fifteen minutes. Google will "
          + "ask for the password even though the session is live \u2014 "
          + "that is the point."),
        h("p", { class: "muted note" },
          "Looking is recorded against your name."),
      ],
      controls: [], byKey: {} },
    "Sign in again",
    async () => {
      window.location.href = "/auth/elevate";
      onDone(null, null);
    });
}
