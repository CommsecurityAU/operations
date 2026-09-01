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

let pending = null;
let showing = null;

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

// A bar per month, drawn as SVG rather than pulled from a charting library:
// twelve rectangles and a zero line is not worth 90 KB, and the tokens are
// already here.
function barChart(months, pick, title) {
  const values = months.map(pick);
  const top = Math.max(1, ...values.map(Math.abs));
  // Lower case because they are locals. A CAPS name inside a function
  // reads as a module constant to anyone scanning, including the guardrail
  // that checks every module constant is declared.
  const barWidth = 22, gap = 6, height = 72;
  const width = months.length * (barWidth + gap);
  const zero = height / 2;
  return h("figure", { class: "chart" },
    h("figcaption", null, title),
    h("svg", {
      viewBox: `0 0 ${width} ${height + 16}`, class: "bars",
      role: "img", "aria-label": title,
    },
      h("line", { x1: "0", y1: String(zero), x2: String(width),
                  y2: String(zero), class: "axis" }),
      months.map((month, i) => {
        const v = values[i];
        const bar = Math.abs(v) / top * (height / 2 - 2);
        return h("g", null,
          h("rect", {
            x: String(i * (barWidth + gap)),
            y: String(v >= 0 ? zero - bar : zero),
            width: String(barWidth), height: String(Math.max(1, bar)),
            class: (v >= 0 ? "up" : "down")
                   + (month.is_actual ? " actual" : " projected"),
          }, h("title", null,
               `${month.label}  ${fmt.money(v)}`
               + (month.is_actual ? "" : "  (projected)"))),
          h("text", { x: String(i * (barWidth + gap) + barWidth / 2),
                      y: String(height + 12), class: "tick" },
            month.label.slice(0, 3)));
      })));
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
    orders: figure("Orders in hand"),
    project: figure("Project cost"),
    office: figure("Office cost"),
    total: figure("Total cost"),
    gross: figure("Gross profit", "is-primary"),
    tax: figure("Corporate tax"),
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

  const row = (label, key, cls) => h("tr", { class: cls || null },
    h("th", null, label),
    months.map((m) => h("td", { class: "num" }, fmt.money(m[key]))),
    h("th", { class: "num" }, fmt.money(sum(months, key))));

  // Which projects are actually carrying the year. Not every project: the
  // ten that matter, because a list of sixty-five is a list nobody reads.
  const carrying = [...data.projects]
    .filter((p) => p.contract_value_cents)
    .sort((a, b) => b.contract_value_cents - a.contract_value_cents)
    .slice(0, 10);

  const byCategory = data.expense_categories
    .filter((c) => c.fy_label === showing)
    .sort((a, b) => b.total_cents - a.total_cents);

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

  function invoicingTable() {
    return h("div", null,
      h("h2", null, `Invoicing by project \u00b7 ${showing}`),
      h("div", { class: "table-wrap" },
        h("table", { class: "expense-grid" },
          h("thead", null, h("tr", null,
            h("th", null, `${inYear.length} projects`),
            months.map((m) => h("th", { class: "num" }, m.label)),
            h("th", { class: "num" }, "Total"))),
          h("tbody", null, inYear.map((row) => h("tr", null,
            h("td", { class: "text-wide" }, row.project.name),
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
                : "\u2013")),
            h("th", { class: "num" }, fmt.money(row.total))))),
          h("tfoot", null, h("tr", null,
            h("th", null, "Total"),
            months.map((_m, i) => h("th", { class: "num" },
              fmt.money(columnTotal(i)))),
            h("th", { class: "num" },
              fmt.money(inYear.reduce((t, r) => t + r.total, 0))))))));
  }

  mount(root, h("div", { class: "content" },
    h("div", { class: "page-head" },
      h("h1", null, "Dashboard"),
      h("span", { class: "eyebrow" }, "revenue, cost, and what is left"),
      h("span", { class: "spacer" }),
      h("span", { class: "row-actions" },
        h("span", { class: "muted" },
          `${data.active_projects} active project`
          + `${data.active_projects === 1 ? "" : "s"} \u00b7 `
          + `${data.staff_count} employee`
          + `${data.staff_count === 1 ? "" : "s"}`),
        picker)),
    notice,
    h("div", { class: "figures" }, Object.values(cards).map((f) => f.el)),
    months.length
      ? h("div", { class: "charts" },
          barChart(months, (m) => m.revenue_cents, "Revenue by month"),
          barChart(months, (m) => m.total_cost_cents, "Cost by month"),
          barChart(months, (m) => m.gross_profit_cents,
                   "Gross profit by month"))
      : null,
    months.length
      ? h("div", { class: "table-wrap" },
          h("table", { class: "expense-grid" },
            h("thead", null, h("tr", null,
              h("th", null, showing),
              months.map((m) => h("th", { class: "num" },
                m.label, m.is_actual ? null
                  : h("span", { class: "tag is-quiet" }, "proj"))),
              h("th", { class: "num" }, "Total"))),
            h("tbody", null,
              row("Revenue", "revenue_cents"),
              row("Project cost", "project_cost_cents"),
              row("Office cost", "office_cost_cents"),
              row("Total cost", "total_cost_cents"),
              row("Gross profit", "gross_profit_cents", "group")),
            h("tfoot", null, h("tr", null,
              h("th", null, "Corporate tax and net profit"),
              h("td", { class: "num muted",
                        colspan: String(months.length) },
                "assessed on the year, not the month"),
              h("th", { class: "num" },
                fmt.money(year.net_profit_cents || 0))))))
      : stateMessage("Nothing for this year yet", null, false),
    h("div", { class: "split" },
      h("div", null,
        h("h2", null, "Where the cost sits"),
        h("table", null,
          h("thead", null, h("tr", null,
            h("th", null, "Category"), h("th", { class: "num" }, "Lines"),
            h("th", { class: "num" }, showing))),
          h("tbody", null, byCategory.map((c) => h("tr", null,
            h("td", null, c.name),
            h("td", { class: "num muted" }, String(c.line_count)),
            h("td", { class: "num" }, fmt.money(c.total_cents))))))),
      h("div", null,
        h("h2", null, "Largest contracts"),
        h("table", null,
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
              fmt.money(p.committed_cents + p.estimated_cents)))))))),
    inYear.length ? invoicingTable() : null,
    h("p", { class: "muted note" },
      "Office cost is not charged to projects \u2014 "
      + "rent and payroll tax are not bought for a job, and spreading them "
      + "across jobs would invent a margin nobody agreed to. Corporate tax "
      + "is assessed on the YEAR: a loss in one month offsets a profit in "
      + "another, and a year that loses money pays none.")));
}
