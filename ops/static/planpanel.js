// planpanel.js — turning a contract into a forecast.
//
//   A contract splits into ITEMS with values.
//   Each item is spread across months by PERCENTAGE.
//   A month's claim is the SUM of that month's contributions.
//
// The grid is the thing: items down, months across, an amount in each cell.
// That is the shape of the progress-claim workbooks this replaces, and
// showing it any other way would mean learning a new one.
//
// Nothing here refuses to save an incomplete plan. A plan under
// construction is legitimately short of 100%, and demanding the whole thing
// in one sitting is how a tool stops being used. The gaps are REPORTED, at
// the top, while you work.

import { api, fmt, h, moneyInput, mount, toCents } from "./app.js";
import { field, sheet } from "./sheet.js";

// Which months the grid shows: every month already used, plus a year ahead
// of the last one, so there is always somewhere to put the next allocation.
function monthsFor(plan, periods) {
  const used = new Set(plan.allocations.map((a) => a.period_id));
  for (const m of plan.months) used.add(m.period_id);
  const chosen = periods.filter((p) => used.has(p.id));
  const last = chosen.length ? chosen[chosen.length - 1].month_start : null;
  const ahead = periods
    .filter((p) => !used.has(p.id) && (!last || p.month_start > last))
    .slice(0, 12);
  return [...chosen, ...ahead].sort(
    (a, b) => (a.month_start < b.month_start ? -1 : 1));
}

function itemDialog(project, item, onDone) {
  const controls = {
    name: field("Name",
      h("input", { type: "text", value: item ? item.name : "",
                   "aria-label": "Name" }),
      "Labels every claim this contributes to"),
    value_cents: field("Value",
      moneyInput(item ? item.value_cents : 0, { "aria-label": "Value" }),
      "Ex-GST"),
    is_variation: field("Kind",
      (() => {
        const s = h("select", { "aria-label": "Kind" },
          h("option", { value: "0", selected: !item || !item.is_variation },
            "Part of the contract"),
          h("option", { value: "1", selected: item && item.is_variation },
            "Variation \u2014 outside the contract total"));
        return s;
      })()),
    note: field("Note",
      h("textarea", { rows: 2, "aria-label": "Note" }, item ? item.note || "" : "")),
  };
  sheet(item ? `Edit ${item.name}` : `New item \u00b7 ${project.name}`,
    item ? fmt.money(item.value_cents) : project.job_code,
    { fields: Object.values(controls).map((c) => c.wrap),
      controls: Object.values(controls), byKey: controls },
    item ? "Save" : "Add item",
    async () => {
      const cents = toCents(controls.value_cents.control.value);
      if (cents === null) {
        controls.value_cents.setError("not an amount");
        throw new Error("");
      }
      const payload = {
        name: controls.name.control.value.trim(),
        value_cents: cents,
        is_variation: controls.is_variation.control.value === "1",
        note: controls.note.control.value.trim(),
      };
      if (item) await api("PATCH", `/api/plan/items/${item.claim_item_id}`, payload);
      else await api("POST", `/api/projects/${project.id}/plan/items`, payload);
      onDone(item ? `${payload.name} updated`
                  : `Added ${payload.name}, ${fmt.money(cents)}`);
    });
}

// A cell: type a percentage OR an amount. The AMOUNT is what is stored --
// 33.33% of $79,444 is $26,478.69 while the agreed figure is $26,481.33 --
// so typing a percentage fills the amount in and the amount stays editable.
function allocationDialog(item, period, existing, onDone) {
  const percent = h("input", { type: "number", min: "0", max: "100",
                               step: "0.01", "aria-label": "Percent",
                               value: existing
                                 ? fmt.fromBp(existing.percent_bp) : "" });
  const amount = moneyInput(existing ? existing.amount_cents : 0,
                            { "aria-label": "Amount" });
  const controls = {
    percent_bp: field("Percent of the item", percent,
      `${fmt.money(item.value_cents)} in total`),
    amount_cents: field("Amount", amount,
      "What will be claimed. The percentage is how it was expressed; this "
      + "is the figure."),
    note: field("Note",
      h("textarea", { rows: 2, "aria-label": "Note" },
        existing ? existing.note || "" : "")),
  };
  // Typing a percentage proposes an amount; the amount stays yours.
  percent.addEventListener("input", () => {
    const bp = fmt.toBp(percent.value);
    if (!bp) return;
    // Proposed, not imposed: the amount stays editable because the agreed
    // figure and the rounded percentage do not always agree.
    amount.value = fmt.plain(Math.round(item.value_cents * bp / 10000));
  });
  sheet(`${item.name} \u00b7 ${period.label}`,
    existing ? fmt.money(existing.amount_cents) : "not yet allocated",
    { fields: Object.values(controls).map((c) => c.wrap),
      controls: Object.values(controls), byKey: controls },
    "Set",
    async () => {
      const cents = toCents(amount.value);
      if (cents === null) {
        controls.amount_cents.setError("not an amount");
        throw new Error("");
      }
      await api("POST", `/api/plan/items/${item.claim_item_id}/allocate`, {
        period_id: period.id,
        percent_bp: fmt.toBp(percent.value || 0),
        amount_cents: cents,
        note: controls.note.control.value.trim(),
      });
      onDone(cents
        ? `${item.name}: ${period.label} set to ${fmt.money(cents)}`
        : `${item.name}: ${period.label} cleared`);
    });
}

// A gap under a dollar is rounding in the source, not a finding. Amber on
// half the register for a cent is how a check teaches people to ignore it.
const NOISE = 100;

function healthLine(plan) {
  const bits = [];
  if (!plan.items.length && plan.unplanned_claims && plan.unplanned_claims.n) {
    return [h("span", { class: "gap-note" },
      `${fmt.money(plan.unplanned_claims.cents)} already forecast, `
      + "not yet described as a plan")];
  }
  // Measured against what a plan CAN describe: the contract less whatever
  // was billed before this platform's window opened. `720 Bourke` claimed
  // $112,545.67 in FY26, and calling that unplanned was wrong -- it is
  // billed, and no plan here could ever cover it.
  if (plan.opening_balance_cents) {
    bits.push(h("span", { class: "muted" },
      `\u00b7 ${fmt.money(plan.plannable_cents)} to plan `
      + `(${fmt.money(plan.opening_balance_cents)} claimed before FY27)`));
  }
  if (plan.unitemised_cents > NOISE) {
    bits.push(h("span", { class: "gap-note" },
      `${fmt.money(plan.unitemised_cents)} not broken into items yet`));
  } else if (plan.unitemised_cents < -NOISE) {
    bits.push(h("span", { class: "gap-note" },
      `items exceed what is left to claim by `
      + fmt.money(-plan.unitemised_cents)));
  }
  const short = plan.items.filter(
    (i) => Math.abs(i.unallocated_cents) > NOISE);
  if (short.length) {
    bits.push(h("span", { class: "gap-note" },
      `${short.length} item${short.length === 1 ? "" : "s"} not fully spread `
      + `across months`));
  }
  if (!bits.length) {
    bits.push(h("span", { class: "muted" },
      `\u00b7 ${fmt.money(plan.allocated_cents)} planned, every item at 100%`));
  }
  return bits;
}

export async function planPanel(project, canWrite, onChange, periods) {
  const plan = await api("GET", `/api/projects/${project.id}/plan`);
  const months = monthsFor(plan, periods);
  const byCell = new Map();
  for (const a of plan.allocations) {
    byCell.set(`${a.claim_item_id}:${a.period_id}`, a);
  }
  const claimByPeriod = new Map(plan.claims.map((c) => [c.period_id, c]));

  const cell = (item, period) => {
    const found = byCell.get(`${item.claim_item_id}:${period.id}`);
    if (!canWrite || (found && found.is_locked)) {
      return h("td", { class: found ? "num" : "num zero",
                       title: found && found.is_locked
                         ? "invoiced \u2014 amend the claim to change it" : null },
        found ? fmt.money(found.amount_cents) : "\u2013");
    }
    const button = h("button", { type: "button", class: "cell-button" },
      found ? fmt.money(found.amount_cents) : "\u2013");
    button.addEventListener("click", () =>
      allocationDialog(item, period, found, onChange));
    return h("td", { class: found ? "num" : "num zero" }, button);
  };

  const itemRow = (item) => h("tr", null,
    h("td", { class: "text-wide" },
      canWrite
        ? h("button", { type: "button", class: "link-button",
                        onclick: () => itemDialog(project, item, onChange) },
            item.name)
        : item.name,
      item.is_variation ? h("span", { class: "tag" }, "variation") : null),
    h("td", { class: "num" }, fmt.money(item.value_cents)),
    // Short of its value is the only reason to keep working on a row.
    h("td", { class: Math.abs(item.unallocated_cents) > NOISE
                       ? "num flagged-cell" : "num zero" },
      Math.abs(item.unallocated_cents) > NOISE
        ? fmt.money(item.unallocated_cents) : "\u2013"),
    months.map((p) => cell(item, p)));

  const monthTotal = (period) => plan.items.reduce((sum, i) => {
    const found = byCell.get(`${i.claim_item_id}:${period.id}`);
    return sum + (found ? found.amount_cents : 0);
  }, 0);

  return h("div", { class: "plan-panel" },
    h("div", { class: "po-panel-head" },
      h("h3", null, `Claim plan \u00b7 ${fmt.money(plan.contract_value_cents)}`),
      healthLine(plan),
      h("span", { class: "spacer" }),
      canWrite
        ? h("span", { class: "row-actions" },
            h("button", { type: "button",
                          onclick: () => itemDialog(project, null, onChange) },
              "Add item"),
            // Only while there is something to adopt and nothing to lose.
            // Rebuild, once a plan exists: the plan is derived from the
            // claims, so it can always be rebuilt from them -- and a plan
            // built before the model changed will not follow its claims.
            plan.items.length
              ? h("button", { type: "button",
                              title: "Discard the plan and rebuild it from "
                                     + "the claims as they stand now",
                              onclick: async () => {
                                if (!window.confirm(
                                    "Rebuild this plan from the claims? "
                                    + "Anything entered by hand is lost.")) {
                                  return;
                                }
                                try {
                                  const r = await api(
                                    "POST",
                                    `/api/projects/${project.id}/plan/adopt`,
                                    { rebuild: true });
                                  onChange(`Rebuilt: ${r.items} item(s), `
                                           + `${r.allocations} month(s)`);
                                } catch (err) {
                                  onChange(err.message);
                                }
                              } }, "Rebuild from claims")
              : null,
            !plan.items.length && plan.unplanned_claims
                              && plan.unplanned_claims.n
              ? h("button", { type: "button",
                              title: "Build the plan from the claims that "
                                     + "already exist here",
                              onclick: async () => {
                                try {
                                  const r = await api(
                                    "POST",
                                    `/api/projects/${project.id}/plan/adopt`);
                                  onChange(r.items
                                    ? `Built ${r.items} item(s) from `
                                      + `${r.allocations} month(s) of existing `
                                      + "claims"
                                    : r.reason || "nothing to adopt");
                                } catch (err) {
                                  onChange(err.message);
                                }
                              } }, "Adopt claims")
              : null,
            h("button", { type: "button", class: "primary",
                          title: "Create or update the forecast claims. "
                                 + "Invoiced months are never touched.",
                          onclick: async () => {
                            try {
                              const r = await api(
                                "POST",
                                `/api/projects/${project.id}/plan/generate`);
                              onChange(
                                `${r.created} claim(s) created, ${r.updated} `
                                + `updated`
                                + (r.locked
                                   ? `, ${r.locked} left alone \u2014 invoiced`
                                   : ""));
                            } catch (err) {
                              onChange(err.message);
                            }
                          } }, "Generate claims"))
        : null),
    plan.items.length
      ? h("div", { class: "table-wrap" },
          h("table", { class: "plan-grid" },
            h("thead", null, h("tr", null,
              h("th", null, "Item"),
              h("th", { class: "num" }, "Value"),
              h("th", { class: "num" }, "Unspread"),
              months.map((p) => h("th", { class: "num" }, p.label)))),
            h("tbody", null, plan.items.map(itemRow)),
            h("tfoot", null, h("tr", null,
              h("th", null, "Claim"),
              h("th", { class: "num" }, fmt.money(plan.allocated_cents)),
              h("th", null, ""),
              months.map((p) => {
                const claim = claimByPeriod.get(p.id);
                const total = monthTotal(p);
                return h("th", { class: "num",
                                 title: claim ? `claim is ${claim.status}` : null },
                  total ? fmt.money(total) : "\u2013",
                  claim && (claim.status === "invoiced" || claim.status === "paid")
                    ? h("span", { class: "locked-mark", title: "invoiced" }, " \u25cf")
                    : null);
              })))))
      // Never report an empty plan as an empty project: these claims exist
      // and saying nothing about them is how a screen loses trust.
      : plan.unplanned_claims && plan.unplanned_claims.n
        ? h("div", { class: "muted" },
            `${plan.unplanned_claims.n} claim`
            + `${plan.unplanned_claims.n === 1 ? "" : "s"} already forecast `
            + `here, ${fmt.money(plan.unplanned_claims.cents)}, with no plan `
            + "behind them. Adopt builds one from the phases they came from, "
            + "rather than asking for the same forecast twice.")
        : h("div", { class: "muted" },
            "No plan yet. Break the contract into items \u2014 Equipment, "
            + "Project Management, Design \u2014 then spread each across the "
            + "months you expect to claim it."));
}
