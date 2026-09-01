// expenses.js — what the business costs to run.
//
// A matrix: categories down, months across. That is the shape of the sheet
// it replaces, and the shape people already read.
//
// Two levels, because both are asked of it. THE YEAR is what a budget
// conversation needs; THE MONTH is what a cash conversation needs. Same
// data, one toggle.
//
// Wages are DERIVED from an annual salary, super from wages, and the
// statutory charges from wages plus super. So most of this grid is not
// typed, and the cells that are typed look different from the cells that
// are not — a figure whose provenance is invisible is a figure nobody can
// question.

import { api, fmt, h, mount, stateMessage } from "./app.js";

// The dialogs load when one is opened: the grid is what most visits want.
const forms = () => import("./expenseforms.js");

let pending = null;
let view = "month";      // or `fy`
let showing = null;      // which FY, when showing months

// Categories start CLOSED. Signing in to finance should show what the
// business costs, not eleven people's monthly pay — the summary is the
// answer to the common question and the detail is the answer to a rarer
// one. Kept across repaints, so opening a category survives an edit.
const open_ = new Set();

const SOURCE_TITLE = {
  entered: "typed",
  salary: "from the annual salary",
  rate: "calculated",
};

function figure(label, kind) {
  const value = document.createTextNode("");
  return {
    el: h("div", { class: kind ? `figure ${kind}` : "figure" },
      h("span", { class: "label" }, label),
      h("span", { class: "value" }, value)),
    set: (t) => { value.nodeValue = t; },
  };
}

export async function render(root) {
  mount(root, stateMessage("Loading expenses", null, false));
  let data, me;
  try {
    [data, me] = await Promise.all([
      api("GET", "/api/expenses"),
      api("GET", "/api/me"),
    ]);
  } catch (err) {
    mount(root, err.status === 403
      ? stateMessage("Finance only",
          "These figures are wages. The finance role is granted separately "
          + "from every other, and is implied by none of them.", false)
      : stateMessage("Could not load expenses", err.message, true));
    return;
  }
  const canWrite = new Set(me.roles.map((r) => r.role)).has("finance");

  const notice = h("div", { class: "notice", hidden: true });
  if (pending) {
    notice.textContent = pending;
    notice.hidden = false;
    pending = null;
  }
  const report = (message) => { pending = message; render(root); };

  async function open(name, ...args) {
    const module = await forms();
    module[name](...args, (_result, message) => report(message));
  }

  // Which columns the grid shows. A financial year at a time, because
  // eighteen months across is a grid nobody can read.
  const years = [];
  for (const period of data.periods) {
    if (!years.some((y) => y.label === period.fy_label)) {
      years.push({ label: period.fy_label, fy: period.fy });
    }
  }
  const used = new Set(data.amounts.map((a) => a.period_id));
  const live = years.filter((y) => data.periods.some(
    (p) => p.fy_label === y.label && used.has(p.id)));
  if (!showing || !live.some((y) => y.label === showing)) {
    showing = live.length ? live[0].label : null;
  }
  const columns = view === "fy"
    ? live.map((y) => ({ key: y.label, label: y.label,
                         periods: data.periods.filter(
                           (p) => p.fy_label === y.label).map((p) => p.id) }))
    : data.periods.filter((p) => p.fy_label === showing)
        .map((p) => ({ key: String(p.id), label: p.label, periods: [p.id],
                       period: p }));

  const byCell = new Map();
  for (const a of data.amounts) {
    byCell.set(`${a.expense_line_id}:${a.period_id}`, a);
  }
  const cellsFor = (lineId, column) => column.periods
    .map((id) => byCell.get(`${lineId}:${id}`)).filter(Boolean);
  const sum = (rows) => rows.reduce((t, r) => t + r.amount_cents, 0);

  const figures = {
    year: figure(showing || "", "is-primary"),
    wages: figure("Wages and super"),
    statutory: figure("Work Cover and payroll tax"),
    other: figure("Everything else"),
  };
  const inYear = data.amounts.filter((a) => {
    const period = data.periods.find((p) => p.id === a.period_id);
    return period && period.fy_label === showing;
  });
  const kindOf = new Map(data.lines.map((l) => [l.line_id, l.category_kind]));
  figures.year.set(fmt.money(sum(inYear)));
  figures.wages.set(fmt.money(sum(inYear.filter(
    (a) => ["wages", "super"].includes(kindOf.get(a.expense_line_id))))));
  figures.statutory.set(fmt.money(sum(inYear.filter(
    (a) => kindOf.get(a.expense_line_id) === "statutory"))));
  figures.other.set(fmt.money(sum(inYear.filter(
    (a) => kindOf.get(a.expense_line_id) === "expense"))));

  // A salary is never in the payload unless this person has just
  // re-authenticated, so the cell offers to fetch one rather than hiding a
  // figure it was given. Hiding it in the interface would hide it from
  // nobody with the developer tools open.
  const salaried = new Set(data.salaried || []);
  function salaryCell(line) {
    if (line.rate_bp && !salaried.has(line.line_id)) {
      // A statutory rate is policy, not pay. It stays visible so the
      // charge beneath it can be checked.
      return canWrite
        ? h("button", { type: "button", class: "link-button",
                        title: "change the rate",
                        onclick: () => open("lineDialog", line, data) },
            `${line.rate_bp / 10000}%`)
        : `${line.rate_bp / 10000}%`;
    }
    if (!salaried.has(line.line_id)) return "\u2013";
    if (data.elevated && line.annual_cents) {
      return h("button", { type: "button", class: "link-button",
                           title: "annual salary",
                           onclick: () => open("salaryDialog", line, data) },
        fmt.money(line.annual_cents));
    }
    if (!data.may_see_salaries) {
      // Not a locked door with a key beside it: this person will not be
      // let through however many times they sign in.
      return h("span", { class: "muted",
                         title: "the payroll role is granted separately" },
        "\u2022\u2022\u2022\u2022\u2022\u2022");
    }
    return h("button", { type: "button", class: "link-button muted",
                         title: "sign in again to see this",
                         onclick: () => open("revealDialog", line) },
      "\u2022\u2022\u2022\u2022\u2022\u2022");
  }

  function cell(line, column) {
    const found = cellsFor(line.line_id, column);
    const total = sum(found);
    const single = column.period && found.length === 1 ? found[0] : null;
    const source = single ? single.source : null;
    // A typed figure and a calculated one look different, because knowing
    // which is which is the difference between checking a number and
    // trusting it.
    const cls = "num" + (total ? "" : " zero")
      + (source && source !== "entered" ? " derived" : "");
    if (!canWrite || !column.period) {
      return h("td", { class: cls, title: SOURCE_TITLE[source] || null },
        total ? fmt.money(total) : "\u2013");
    }
    const button = h("button", { type: "button", class: "cell-button",
                                 title: SOURCE_TITLE[source] || null },
      total ? fmt.money(total) : "\u2013");
    button.addEventListener("click", () =>
      open("amountDialog", line, column.period, single, data));
    return h("td", { class: cls }, button);
  }

  const rows = [];
  let lastCategory = null;
  for (const line of data.lines) {
    if (line.category_name !== lastCategory) {
      lastCategory = line.category_name;
      const name = lastCategory;
      const inCategory = data.lines.filter(
        (l) => l.category_name === name).map((l) => l.line_id);
      const isOpen = open_.has(name);
      const toggleRow = h("button", { type: "button", class: "group-toggle",
                                      "aria-expanded": String(isOpen) },
        h("span", { class: "chevron" }, isOpen ? "\u25be" : "\u25b8"),
        name,
        h("span", { class: "muted" },
          ` ${inCategory.length} line${inCategory.length === 1 ? "" : "s"}`));
      toggleRow.addEventListener("click", () => {
        if (isOpen) open_.delete(name); else open_.add(name);
        render(root);
      });
      rows.push(h("tr", { class: "group" },
        h("th", { colspan: "2" }, toggleRow),
        columns.map((column) => h("th", { class: "num" },
          fmt.money(inCategory.reduce(
            (t, id) => t + sum(cellsFor(id, column)), 0))))));
    }
    if (!open_.has(lastCategory)) continue;
    rows.push(h("tr", null,
      h("td", { class: "text-wide" },
        canWrite
          ? h("button", { type: "button", class: "link-button",
                          onclick: () => open("lineDialog", line, data) },
              line.line_name)
          : line.line_name,
        line.is_forecast ? h("span", { class: "tag" }, "forecast") : null,
        line.state ? h("span", { class: "tag is-quiet" }, line.state) : null),
      h("td", { class: "num muted" }, salaryCell(line)),
      columns.map((column) => cell(line, column))));
  }

  const toggle = (name, label) => {
    const button = h("button", { type: "button",
                                 class: view === name ? "primary" : null }, label);
    button.addEventListener("click", () => { view = name; render(root); });
    return button;
  };
  const yearPicker = h("select", { "aria-label": "Financial year" },
    live.map((y) => h("option",
      { value: y.label, selected: y.label === showing }, y.label)));
  yearPicker.addEventListener("change", () => {
    showing = yearPicker.value;
    render(root);
  });

  mount(root, h("div", { class: "content" },
    h("div", { class: "page-head" },
      h("h1", null, "Office expenses"),
      h("span", { class: "eyebrow" }, "what the business costs to run"),
      h("span", { class: "spacer" }),
      h("span", { class: "row-actions" },
        h("button", { type: "button",
                      onclick: () => {
                        // All or nothing: opening eleven categories one at
                        // a time to find a figure is worse than the wall of
                        // rows this exists to avoid.
                        const names = [...new Set(
                          data.lines.map((l) => l.category_name))];
                        if (open_.size) open_.clear();
                        else names.forEach((n) => open_.add(n));
                        render(root);
                      } },
          open_.size ? "Collapse all" : "Expand all"),
        toggle("month", "By month"), toggle("fy", "By year"),
        view === "month" ? yearPicker : null,
        canWrite
          ? h("button", { type: "button",
                          onclick: () => open("categoryDialog", data) },
              "New category")
          : null,
        h("a", { class: "button", href: "/api/expenses/export",
                 title: data.elevated
                   ? "Includes salaries, because you have signed in again"
                   : "Salaries are left out" }, "Export CSV"),
        canWrite
          ? h("button", { type: "button", class: "primary",
                          onclick: () => open("lineDialog", null, data) },
              "New line")
          : null)),
    notice,
    h("div", { class: "figures" }, Object.values(figures).map((f) => f.el)),
    data.lines.length
      ? h("div", { class: "table-wrap" },
          h("table", { class: "expense-grid" },
            h("thead", null, h("tr", null,
              h("th", null, "Line"),
              h("th", { class: "num" }, "Salary / rate"),
              columns.map((c) => h("th", { class: "num" }, c.label)))),
            h("tbody", null, rows),
            h("tfoot", null, h("tr", null,
              h("th", { colspan: "2" }, "Total"),
              columns.map((column) => h("th", { class: "num" },
                fmt.money(data.lines.reduce(
                  (t, l) => t + sum(cellsFor(l.line_id, column)), 0))))))))
      : stateMessage("Nothing here yet",
          "Import the office expenses sheet, or add a category and a line.",
          false),
    h("p", { class: "muted note" },
      (data.elevated
        ? "You have signed in again, so salaries are shown. That lapses "
          + "after fifteen minutes. "
        : data.may_see_salaries
          ? "Salaries are not sent to this page at all \u2014 not hidden, "
            + "withheld. Click one to sign in again and see it. "
          : "Salaries need the payroll role, which is granted separately "
            + "from finance. ")
      + "Figures in a lighter weight are calculated \u2014 a wage from its "
      + "annual salary, superannuation from the wage, Work Cover and "
      + "payroll tax from wages plus super for that state. Change a salary "
      + "or a rate and every month follows.")));
}
