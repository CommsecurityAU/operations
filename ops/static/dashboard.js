// dashboard.js — what the business is worth, what it costs, what is left.
//
//     revenue        claims, billed and forecast
//   - project cost   procurement, committed and estimated
//   - office cost    wages, overhead, the statutory charges
//   = gross profit
//   - corporate tax  ON THE YEAR
//   = net profit
//
// Office cost does not attach to a project: rent is not bought for a job,
// and spreading it across jobs would invent a margin nobody agreed to.
//
// Months that have ended are ACTUAL; the rest are projections. Marked,
// because a dashboard that mixes them silently is one nobody can act on.

import { api, fmt, h, mount, stateMessage } from "./app.js";
import { combo } from "./chart.js";

let pending = null;
let showing = null;
// Charts, tables, or both. TABLES on load: the figure somebody came for is
// in the table, and a chart is what they open when the figure raises a
// question. Kept across repaints -- a mode somebody chose is one they want
// to stay in.
let mode = "tables";

function figure(label, kind) {
  const value = document.createTextNode("");
  const note = document.createTextNode("");
  return {
    el: h("div", { class: kind ? `figure ${kind}` : "figure" },
      h("span", { class: "label" }, label),
      h("span", { class: "value" }, value),
      h("span", { class: "sub muted" }, note)),
    set: (v, n) => { value.nodeValue = v; note.nodeValue = n || ""; },
  };
}

export async function render(root) {
  mount(root, stateMessage("Loading dashboard", null, false));
  let data;
  try {
    data = await api("GET", "/api/dashboard");
  } catch (err) {
    mount(root, err.status === 403
      ? stateMessage("Finance only",
          "The dashboard shows what the business costs to run.", false)
      : stateMessage("Could not load the dashboard", err.message, true));
    return;
  }
  const notice = h("div", { class: "notice", hidden: true });
  if (pending) {
    notice.textContent = pending;
    notice.hidden = false;
    pending = null;
  }

  const years = data.years;
  if (!showing || !years.some((y) => y.fy_label === showing)) {
    // The year we are IN, not whichever sorts first: the question anyone
    // opens this to answer is almost always about now.
    const current = years.find((y) => y.fy === data.current_fy);
    showing = current ? current.fy_label
      : years.length ? years[years.length - 1].fy_label : null;
  }
  const year = years.find((y) => y.fy_label === showing) || {};
  const months = data.months.filter((m) => m.fy_label === showing);
  const actuals = months.filter((m) => m.is_actual);
  const sum = (rows, key) => rows.reduce((t, r) => t + r[key], 0);

  const cards = {
    revenue: figure("Invoiceable", "is-primary"),
    invoiced: figure("Invoiced to date"),
    // Under contract and owed to us: the only green on the screen.
    orders: figure("Orders in hand", "is-good"),
    // Costs and the deduction read quietly. They are present and they are
    // not what anybody opens this to see.
    project: figure("Project cost", "is-quiet"),
    office: figure("Office cost", "is-quiet"),
    total: figure("Total cost"),
    gross: figure("Gross profit", "is-primary"),
    tax: figure("Corporate tax", "is-quiet"),
    net: figure("Net profit"),
  };
  // What is under contract and not yet billed. A whole-of-portfolio figure
  // rather than a yearly one: an order placed in FY27 may well be billed in
  // FY28, and splitting it by year would answer a question nobody asked.
  const ordersInHand = data.projects.reduce(
    (t, p) => t + p.orders_in_hand_cents, 0);
  cards.revenue.set(fmt.money(year.revenue_cents || 0),
    year.further_sales_cents
      ? `+ ${fmt.money(year.further_sales_cents)} further sales`
      : "billed and forecast");
  cards.invoiced.set(fmt.money(year.invoiced_cents || 0),
    `${actuals.length} month${actuals.length === 1 ? "" : "s"} actual`);
  cards.orders.set(fmt.money(ordersInHand),
    "under contract, not yet billed");
  cards.project.set(fmt.money(year.project_cost_cents || 0),
    year.estimated_cost_cents
      ? `${fmt.money(year.estimated_cost_cents)} of it estimated`
      : "");
  cards.office.set(fmt.money(year.office_cost_cents || 0),
    "not charged to projects");
  cards.total.set(fmt.money(year.total_cost_cents || 0),
    "project and office together");
  cards.gross.set(fmt.money(year.gross_profit_cents || 0),
    year.revenue_cents
      ? `${fmt.share(year.gross_profit_cents, year.revenue_cents)}% of revenue`
      : "");
  cards.tax.set(fmt.money(year.corporate_tax_cents || 0),
    (year.gross_profit_cents || 0) > 0
      ? `${(year.tax_rate_bp || 0) / 10000}% of the year's profit`
      : "a year that loses money pays none");
  cards.net.set(fmt.money(year.net_profit_cents || 0), "");

  const picker = h("select", { "aria-label": "Financial year" },
    years.map((y) => h("option",
      { value: y.fy_label, selected: y.fy_label === showing }, y.fy_label)));
  picker.addEventListener("change", () => {
    showing = picker.value;
    render(root);
  });

  // The TOTAL sits beside the label, not eleven columns away. It is the
  // figure most often wanted and it was the one furthest from the eye.
  const row = (label, key, cls) => h("tr", { class: cls || null },
    h("th", null, label),
    h("th", { class: "num total-col" }, fmt.money(sum(months, key))),
    months.map((m) => h("td", { class: "num" }, fmt.money(m[key]))));

  const byCategory = data.expense_categories
    .filter((c) => c.fy_label === showing)
    .sort((a, b) => b.total_cents - a.total_cents);

  // Projects still to bill, largest first. A contract that has been fully
  // invoiced is finished business: it belongs in the history, not in a
  // list of what the year is carrying.
  //
  // As many rows as the cost breakdown beside it, so the two panels are
  // the same height and the page balances.
  const carrying = [...data.projects]
    .filter((p) => p.orders_in_hand_cents > 0)
    .sort((a, b) => b.orders_in_hand_cents - a.orders_in_hand_cents)
    .slice(0, Math.max(byCategory.length, 5));

  // Project by month: where the year's revenue actually comes from. Only
  // projects with something in the year -- sixty-five rows of which forty
  // are empty is a table nobody scans.
  const periodIds = months.map((m) => m.period_id);
  const cellFor = new Map();
  for (const row of data.project_months) {
    cellFor.set(`${row.project_id}:${row.period_id}`, row);
  }
  const inYear = [];
  for (const project of data.projects) {
    const cells = periodIds.map(
      (id) => cellFor.get(`${project.id}:${id}`) || null);
    const total = cells.reduce((t, c) => t + (c ? c.amount_cents : 0), 0);
    if (total) inYear.push({ project, cells, total });
  }
  inYear.sort((a, b) => b.total - a.total);
  const columnTotal = (i) => inYear.reduce(
    (t, r) => t + (r.cells[i] ? r.cells[i].amount_cents : 0), 0);

  // Six named projects and everything else. A stack of thirty is a stack
  // nobody can read, and the tail is individually too small to see.
  // Classes, not colours: the CSP blocks inline styles, so a colour named
  // here would never reach the page. `base.css` holds what each one is.
  const PROJECT_COLOURS = [
    "s-p0", "s-p1", "s-p2", "s-p3", "s-p4", "s-p5",
  ];
  const topProjects = inYear.slice(0, 6);
  const otherRows = inYear.slice(6);

  function invoicingTable() {
    return h("div", null,
      h("h2", null, `Invoicing by project \u00b7 ${showing}`),
      h("div", { class: "table-wrap fit-wrap" },
        h("table", { class: "expense-grid fit" },
          h("thead", null, h("tr", null,
            h("th", null, `${inYear.length} projects`),
            h("th", { class: "num total-col" }, "Total"),
            months.map((m) => h("th", { class: "num" }, m.label)))),
          h("tbody", null, inYear.map((row) => h("tr", null,
            h("td", { class: "text-wide" }, row.project.name),
            h("th", { class: "num total-col" }, fmt.money(row.total)),
            row.cells.map((cell) => h("td",
              { class: cell ? "num" : "num zero",
                // Billed and forecast look different: one is a fact.
                title: cell && cell.invoiced_cents
                  ? `${fmt.money(cell.invoiced_cents)} invoiced` : null },
              cell
                ? (cell.invoiced_cents
                    ? h("span", { class: "invoiced" },
                        fmt.money(cell.amount_cents))
                    : fmt.money(cell.amount_cents))
                : "\u2013"))))),
          h("tfoot", null, h("tr", null,
            h("th", null, "Total"),
            h("th", { class: "num total-col" },
              fmt.money(inYear.reduce((t, r) => t + r.total, 0))),
            months.map((_m, i) => h("th", { class: "num" },
              fmt.money(columnTotal(i)))))))));
  }

  mount(root, h("div", { class: "content" },
    h("div", { class: "page-head" },
      h("h1", null, "Dashboard"),
      h("span", { class: "eyebrow" }, "revenue, cost, and what is left"),
      h("span", { class: "spacer" }),
      h("span", { class: "row-actions" },
        // Charts, tables, or both. The default is both: the chart shows
        // the shape and the table has the figure somebody needs to quote.
        ...[["charts", "Charts"], ["both", "Both"], ["tables", "Tables"]]
          .map(([value, label]) => {
            const button = h("button", {
              type: "button",
              class: mode === value ? "primary" : null,
              "aria-pressed": String(mode === value),
            }, label);
            button.addEventListener("click", () => {
              mode = value;
              render(root);
            });
            return button;
          }),
        h("span", { class: "muted" },
          `${data.active_projects} active project`
          + `${data.active_projects === 1 ? "" : "s"} \u00b7 `
          + `${data.staff_count} employee`
          + `${data.staff_count === 1 ? "" : "s"}`),
        picker)),
    notice,
    h("div", { class: "figures" }, Object.values(cards).map((f) => f.el)),
    months.length && mode !== "tables"
      ? combo(
          months.map((m) => ({
            label: m.label.slice(0, 3),
            projected: !m.is_actual,
            project_cost_cents: m.project_cost_cents,
            office_cost_cents: m.office_cost_cents,
            revenue_cents: m.revenue_cents,
            gross_profit_cents: m.gross_profit_cents,
          })),
          [
            // Cost stacked, because the two together are what revenue has
            // to cover. Revenue and profit as lines over it: the question
            // is whether the line clears the column.
            { key: "project_cost_cents", label: "Project cost", kind: "bar",
              stack: "cost", cls: "s-project" },
            { key: "office_cost_cents", label: "Office cost", kind: "bar",
              stack: "cost", cls: "s-office" },
            { key: "revenue_cents", label: "Revenue", kind: "line",
              cls: "s-revenue" },
            { key: "gross_profit_cents", label: "Gross profit", kind: "line",
              cls: "s-profit" },
          ],
          { title: `Revenue against cost \u00b7 ${showing}` })
      : null,
    months.length && mode !== "charts"
      ? h("div", { class: "table-wrap fit-wrap" },
          h("table", { class: "expense-grid fit" },
            h("thead", null, h("tr", null,
              h("th", null, showing),
              h("th", { class: "num total-col" }, "Total"),
              months.map((m) => h("th", { class: "num" },
                m.label, m.is_actual ? null
                  : h("span", { class: "tag is-quiet" }, "proj"))))),
            h("tbody", null,
              row("Revenue", "revenue_cents"),
              row("Project cost", "project_cost_cents"),
              row("Office cost", "office_cost_cents"),
              row("Total cost", "total_cost_cents"),
              row("Gross profit", "gross_profit_cents", "group")),
            h("tfoot", null, h("tr", null,
              h("th", null, "Net profit"),
              h("th", { class: "num total-col" },
                fmt.money(year.net_profit_cents || 0)),
              h("td", { class: "num muted",
                        colspan: String(months.length) },
                "after corporate tax, assessed on the year")))))
      : stateMessage("Nothing for this year yet", null, false),
    // Two panels side by side: the cost breakdown is a narrow list of
    // categories and does not want the whole width, while the contract
    // table has four columns and does. Sized to their content rather than
    // splitting the page in half.
    // No chart for these two: a ranked bar directly above the table it is
    // drawn from says the same thing twice, and the table also carries the
    // figures. They stay as tables in every mode.
    h("div", { class: "split" },
      h("div", { class: "panel narrow" },
        h("h2", null, "Where the cost sits"),
        h("table", { class: "tight fit" },
          h("thead", null, h("tr", null,
            h("th", null, "Category"),
            h("th", { class: "num" }, showing),
            h("th", { class: "num" }, "Lines"))),
          h("tbody", null, byCategory.map((c) => h("tr", null,
            h("td", null, c.name),
            h("td", { class: "num" }, fmt.money(c.total_cents)),
            h("td", { class: "num muted" }, String(c.line_count))))),
          h("tfoot", null, h("tr", null,
            h("th", null, "Total"),
            h("th", { class: "num" },
              fmt.money(byCategory.reduce((t, c) => t + c.total_cents, 0))),
            h("th", null, ""))))),
      h("div", { class: "panel" },
        h("h2", null, "Largest contracts still to bill"),
        h("table", { class: "tight fit" },
          h("thead", null, h("tr", null,
            h("th", null, "Project"),
            h("th", { class: "num" }, "Contract"),
            h("th", { class: "num" }, "Left to bill"),
            h("th", { class: "num" }, "Cost"))),
          h("tbody", null, carrying.map((p) => h("tr", null,
            h("td", { class: "text-wide" }, p.name),
            h("td", { class: "num" }, fmt.money(p.contract_value_cents)),
            h("td", { class: "num" }, fmt.money(p.orders_in_hand_cents)),
            h("td", { class: "num muted" },
              fmt.money(p.committed_cents + p.estimated_cents))))),
          h("tfoot", null, h("tr", null,
            h("th", null, "Total"),
            h("th", { class: "num" },
              fmt.money(carrying.reduce(
                (t, p) => t + p.contract_value_cents, 0))),
            h("th", { class: "num" },
              fmt.money(carrying.reduce(
                (t, p) => t + p.orders_in_hand_cents, 0))),
            h("th", null, "")))))),
    // Who makes up each month. The monthly TOTALS are already the revenue
    // line above, so repeating them would be a second drawing of the same
    // fact; what this table knows and no chart yet shows is which projects
    // the money comes from.
    inYear.length && mode !== "tables"
      ? combo(
          months.map((m, i) => {
            const row = { label: m.label.slice(0, 3) };
            topProjects.forEach((p, n) => {
              row[`p${n}`] = p.cells[i] ? p.cells[i].amount_cents : 0;
            });
            row.other = otherRows.reduce(
              (t, r) => t + (r.cells[i] ? r.cells[i].amount_cents : 0), 0);
            return row;
          }),
          topProjects.map((p, n) => ({
            key: `p${n}`, label: p.project.name, kind: "bar", stack: "inv",
            cls: PROJECT_COLOURS[n % PROJECT_COLOURS.length],
          })).concat(otherRows.length
            ? [{ key: "other", label: `${otherRows.length} others`,
                 kind: "bar", stack: "inv", cls: "s-office" }]
            : []),
          { title: `Invoicing by project \u00b7 ${showing}` })
      : null,
    inYear.length && mode !== "charts" ? invoicingTable() : null,
    h("p", { class: "muted note" },
      "Office cost is not charged to projects \u2014 "
      + "rent and payroll tax are not bought for a job, and spreading them "
      + "across jobs would invent a margin nobody agreed to. Corporate tax "
      + "is assessed on the YEAR: a loss in one month offsets a profit in "
      + "another, and a year that loses money pays none.")));
}
