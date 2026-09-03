// claims.js — the invoicing grid.
//
// We invoice monthly, so the end-of-month is the axis. But FORECASTING
// means looking across months and moving work between them, which one month
// at a time cannot show -- so the grid loads a financial year and filters
// client-side. Month becomes just another filter, and a claim can be moved
// to a different one from the row it sits in.
//
// NO OPTIMISTIC UI. Every action sends, waits, and repaints from what the
// server returned. A grid that shows what you asked for rather than what
// happened is how a month gets closed on figures nobody checked.

import { api, fmt, h, mount, stateMessage, typeCell } from "./app.js";
import { datatable } from "./datatable.js";

const STATUS_LABEL = {
  forecast: "Forecast", due: "Due", approved: "Approved",
  invoiced: "Invoiced", paid: "Paid", cancelled: "Cancelled",
};

// Fields the server insists on before a status may be entered. It refuses
// regardless; this only saves a round trip.
const REQUIRED = {
  approved: [["approved_date", "Approved date", "date"]],
  invoiced: [["invoice_number", "Invoice number", "text"],
             ["invoiced_date", "Invoice date", "date"]],
  paid: [["paid_date", "Payment received", "date"]],
};

const ORDER = { forecast: 0, due: 1, approved: 2, invoiced: 3, paid: 4 };

function figure(label, kind) {
  const value = document.createTextNode("");
  const el = h("div", { class: kind ? `figure ${kind}` : "figure" },
    h("span", { class: "label" }, label),
    h("span", { class: "value" }, value));
  return { el, set: (t) => { value.nodeValue = t; } };
}

// A small dialog for the fields a transition needs. Native <dialog>: Escape
// closes it and focus is trapped without us rebuilding either.
function transitionDialog(claim, to, needsReason, onConfirm) {
  const inputs = {};
  const rows = (REQUIRED[to] || []).map(([key, label, type]) => {
    const input = h("input", { type, "aria-label": label });
    inputs[key] = input;
    return h("label", { class: "field" },
      h("span", { class: "field-label" }, label), input);
  });
  const reason = h("textarea", { rows: 2, "aria-label": "Reason" });
  const error = h("div", { class: "form-error", hidden: true });
  const go = h("button", { type: "button", class: "primary" },
    `Mark ${STATUS_LABEL[to].toLowerCase()}`);
  const cancel = h("button", { type: "button" }, "Cancel");

  const dialog = h("dialog", { class: "sheet", "aria-label": "Change status" },
    h("div", { class: "sheet-head" },
      h("h2", null, `${STATUS_LABEL[claim.status]} \u2192 ${STATUS_LABEL[to]}`),
      h("span", { class: "mono muted" }, claim.job_code)),
    h("div", { class: "form-grid" },
      rows,
      needsReason
        ? h("label", { class: "field" },
            h("span", { class: "field-label" }, "Reason"),
            reason,
            h("span", { class: "field-hint" },
              "Recorded against the claim \u2014 this is what makes slippage "
              + "and reversals reportable"))
        : null),
    error,
    h("div", { class: "sheet-foot" },
      h("span", { class: "spacer" }), cancel, go));

  go.addEventListener("click", async () => {
    const payload = { status: to };
    for (const [key, input] of Object.entries(inputs)) payload[key] = input.value;
    if (needsReason) payload.reason = reason.value.trim();
    go.disabled = true;
    try {
      await onConfirm(payload);
      dialog.close();
    } catch (err) {
      const detail = err.detail;
      error.textContent = detail && typeof detail === "object"
        ? Object.entries(detail).map(([k, v]) => `${k}: ${v}`).join("; ")
        : err.message;
      error.hidden = false;
    } finally {
      go.disabled = false;
    }
  });
  cancel.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove());
  document.body.appendChild(dialog);
  dialog.showModal();
  const first = rows.length ? Object.values(inputs)[0] : reason;
  if (first) first.focus();
}

// Ask for a reason. Only reached when the server would demand one, so it
// never interrupts ordinary re-forecasting.
function reasonDialog(title, onConfirm) {
  const reason = h("textarea", { rows: 2, "aria-label": "Reason" });
  const error = h("div", { class: "form-error", hidden: true });
  const go = h("button", { type: "button", class: "primary" }, "Move it");
  const cancel = h("button", { type: "button" }, "Cancel");
  const dialog = h("dialog", { class: "sheet", "aria-label": "Reason" },
    h("div", { class: "sheet-head" }, h("h2", null, title)),
    h("div", { class: "form-grid" },
      h("label", { class: "field" },
        h("span", { class: "field-label" }, "Why is it moving?"),
        reason,
        h("span", { class: "field-hint" },
          "This claim was committed to a month, so the move is slippage"))),
    error,
    h("div", { class: "sheet-foot" },
      h("span", { class: "spacer" }), cancel, go));
  go.addEventListener("click", async () => {
    go.disabled = true;
    try {
      await onConfirm(reason.value.trim());
      dialog.close();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
    } finally {
      go.disabled = false;
    }
  });
  cancel.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove());
  document.body.appendChild(dialog);
  dialog.showModal();
  reason.focus();
}

export async function render(root) {
  mount(root, stateMessage("Loading claims", null, false));
  let periods, me;
  try {
    [periods, me] = await Promise.all([
      api("GET", "/api/periods"),
      api("GET", "/api/me"),
    ]);
  } catch (err) {
    mount(root, stateMessage("Could not load claims", err.message, true));
    return;
  }
  const canWrite = new Set(me.roles.map((r) => r.role)).has("operations");

  // The financial year is a filter like any other, not a separate control
  // above the grid. Two kinds of filtering on one screen is two things to
  // learn, and the FY behaves no differently from the client or the type.
  const today = new Date().toISOString().slice(0, 10);
  const current = periods.periods.find((p) => p.month_end >= today)
    || periods.periods[0];

  const notice = h("div", { class: "notice", hidden: true });
  const body = h("div");
  const say = (m) => { notice.textContent = m; notice.hidden = !m; };

  async function move(claim, periodId, label) {
    const send = async (reason) => {
      const payload = { period_id: periodId };
      if (reason) payload.reason = reason;
      await api("PATCH", `/api/claims/${claim.id}`, payload);
      say(`${claim.project_name}: moved to ${label}`);
      await load();
    };
    try {
      await send(null);
    } catch (err) {
      // The server decides whether a reason is needed: a forecast moving is
      // planning, a committed claim moving is slippage.
      if (err.detail && err.detail.reason) {
        reasonDialog(`${claim.project_name} \u2192 ${label}`, send);
      } else {
        say(err.message);
      }
    }
  }

  function monthCell(claim) {
    if (!canWrite || claim.is_opening_balance
        || claim.status === "invoiced" || claim.status === "paid") {
      return h("span", { class: "mono" }, claim.period_label || "\u2013");
    }
    // The month IS the control. Changing it moves the claim -- which is the
    // whole activity when forecasting, so it should not need a dialog.
    const select = h("select", { class: "month-move",
                                 "aria-label": `Month for ${claim.project_name}` },
      periods.periods.map((p) => h("option",
        { value: String(p.id), selected: p.id === claim.period_id }, p.label)));
    select.addEventListener("change", () => {
      const chosen = periods.periods.find((p) => p.id === Number(select.value));
      move(claim, chosen.id, chosen.label);
    });
    return select;
  }

  // Some jobs raise a PO per invoice, so this happens constantly. Doing it
  // from the row means not leaving the month you are working on.
  function poCell(claim) {
    if (claim.is_opening_balance) return h("span", { class: "muted" }, "\u2013");
    if (claim.po_number) return h("span", { class: "mono" }, claim.po_number);
    if (!canWrite) return h("span", { class: "muted" }, "no order");
    const button = h("button", { type: "button", class: "link-button" },
      "Add PO");
    button.addEventListener("click", async () => {
      // The same dialog the register uses. Loaded on demand, because most
      // visits to this screen never raise an order.
      const { poDialog } = await import("./popanel.js");
      poDialog({
        title: `New PO \u00b7 ${claim.project_name}`,
        subtitle: claim.job_code,
        presetCents: claim.amount_cents,
        hint: "Ex-GST. One order often covers several claims, so this need "
              + "not match the claim it is raised from.",
        submit: async (payload) => {
          const result = await api("POST", `/api/claims/${claim.id}/po`, payload);
          say(`${claim.project_name}: order `
              + `${result.po.po_number || "(no number)"} raised for `
              + fmt.money(result.po.amount_cents));
          await load();
        },
      });
    });
    return button;
  }

  function statusCell(claim, transitions) {
    const allowed = transitions[claim.status] || [];
    if (!canWrite || !allowed.length || claim.is_opening_balance) {
      return h("span", { class: "tag" }, STATUS_LABEL[claim.status]);
    }
    const select = h("select", { "aria-label": `Status of ${claim.project_name}` },
      h("option", { value: "" }, STATUS_LABEL[claim.status]),
      allowed.map((s) => h("option", { value: s }, `\u2192 ${STATUS_LABEL[s]}`)));
    select.addEventListener("change", () => {
      const to = select.value;
      select.value = "";
      if (!to) return;
      const backward = ORDER[to] < ORDER[claim.status];
      const send = async (payload) => {
        const result = await api("POST", `/api/claims/${claim.id}/status`, payload);
        say(`${claim.project_name}: ${STATUS_LABEL[result.from]} \u2192 `
            + `${STATUS_LABEL[result.to]}`
            + (result.retention_cents
               ? `, retention ${fmt.money(result.retention_cents)} withheld` : ""));
        await load();
      };
      if ((REQUIRED[to] || []).length || backward || to === "cancelled") {
        transitionDialog(claim, to, backward || to === "cancelled", send);
      } else {
        send({ status: to }).catch((err) => say(err.message));
      }
    });
    return select;
  }

  async function load() {
    let payload;
    try {
      // Everything, then filter in the browser: 200 claims is nothing to
      // ship, and it means changing a filter is instant rather than a
      // round trip.
      payload = await api("GET", "/api/claims");
    } catch (err) {
      mount(root, stateMessage("Could not load claims", err.message, true));
      return;
    }
    const rows = payload.claims;
    const figures = {
      // In the order the money moves: what the job is worth, what has been
      // billed, what is held back out of that, what remains to bill, how
      // much of it is scheduled, how much is not, and then the two waiting
      // states. Filtering to one project reads as a position rather than
      // as a set of unrelated totals.
      //
      // The first two are PROJECT figures, summed over the projects on
      // screen. They do not change when the month filter does -- a
      // contract is not a monthly quantity.
      contract: figure("Contract"),
      invoiced: figure("Invoiced", "is-primary"),
      retention: figure("Retention held", "is-attention"),
      oih: figure("Orders in hand", "is-good"),
      forecast: figure("Forecast"),
      gap: figure("Not forecast", "is-attention"),
      due: figure("Due"),
      approved: figure("Approved"),
    };
    const held = payload.retention_by_project || {};

    function summarise(visible) {
      const total = (s) => visible.filter((r) => r.status === s)
        .reduce((sum, r) => sum + r.amount_cents, 0);
      for (const key of ["forecast", "due", "approved", "invoiced"]) {
        figures[key].set(fmt.money(total(key)));
      }
      // Held is a POSITION: summed over the distinct projects in view, not
      // over the rows, or a project with six claims would count six times.
      const seen = new Set(visible.map((r) => r.project_id));
      const sum = [...seen].reduce((t, id) => t + (held[id] || 0), 0);
      figures.retention.set(fmt.money(sum));
      figures.retention.el.hidden = sum === 0;

      // Coverage follows the PROJECTS on screen, not the claims: a project
      // with six claims must count once. Netting an under-forecast job
      // against an over-forecast one would report that everything is fine.
      // Project figures, over the DISTINCT projects in view. A project
      // with six claims must count once, and a month filter must not
      // change a contract value.
      const inView = (payload.coverage || []).filter(
        (c) => seen.has(c.project_id));
      figures.contract.set(fmt.money(inView.reduce(
        (t, c) => t + c.contract_value_cents, 0)));
      figures.oih.set(fmt.money(inView.reduce(
        (t, c) => t + c.orders_in_hand_cents, 0)));
      const projects = inView.length;
      figures.contract.el.title = figures.oih.el.title =
        `across ${projects} project${projects === 1 ? "" : "s"}; not `
        + "affected by the month filter";

      const short = inView.filter((c) => c.state === "project under");
      const gap = short.reduce((t, c) => t + c.gap_cents, 0);
      const count = short.length;
      figures.gap.set(fmt.money(gap));
      figures.gap.el.hidden = gap === 0;
      figures.gap.el.title = count
        ? `${count} project${count === 1 ? "" : "s"} with work left to bill `
          + "and no month to bill it in"
        : "";
    }

    const COLUMNS = [
      { key: "fy_label", label: "FY", cls: () => "mono" },
      { key: "period_label", label: "Month", sortKey: "month_start",
        fmt: (_v, r) => monthCell(r) },
      { key: "project_name", label: "Project", cls: () => "text-wide" },
      { key: "job_code", label: "Job code", cls: () => "mono" },
      { key: "client", label: "Client", cls: () => "muted text" },
      { key: "type", label: "Type", fmt: (v) => typeCell(v) },
      { key: "detail", label: "Detail", cls: () => "muted text" },
      { key: "po_number", label: "Order", fmt: (_v, r) => poCell(r) },
      { key: "invoice_number", label: "Invoice", cls: () => "mono",
        fmt: (v) => v || "\u2013" },
      { key: "amount_cents", label: "Amount", align: "right", fmt: fmt.moneyDash },
      { key: "retention_state", label: "Retention", cls: () => "muted" },
      // The project's forecast coverage, on every one of its claims, so
      // the grid can be filtered down to the jobs that need scheduling.
      // The PROJECT's coverage, shown on each of its claims. Named so it
      // reads correctly beside a forecast row: the claim is forecast, the
      // project is short.
      { key: "coverage", label: "Project forecast",
        title: "Whether the whole project's remaining contract sits in "
               + "months. It describes the PROJECT, not this claim.",
        fmt: (v, r) => (v === "complete"
          ? h("span", { class: "muted" }, "complete")
          : h("span", { title: `${fmt.money(Math.abs(r.coverage_gap_cents))} `
                               + (v === "project under"
                                  ? "of this project is left to bill and is "
                                    + "in no month"
                                  : "more is forecast than the contract "
                                    + "allows") },
              v === "project under" ? "under" : "over",
              " ", fmt.money(Math.abs(r.coverage_gap_cents)))),
        cls: (v) => (v === "complete" ? "muted" : "flagged-cell") },
      { key: "retention_cents", label: "Withheld", align: "right",
        fmt: fmt.moneyDash, cls: (v) => (v ? "secondary" : "secondary zero") },
      { key: "status", label: "Status", fmt: (_v, r) => statusCell(r, payload.transitions) },
    ];

    mount(body, h("div", null,
      h("div", { class: "figures" }, Object.values(figures).map((f) => f.el)),
      rows.length
        ? datatable({
            columns: COLUMNS,
            rows,
            filters: ["fy_label", "period_label", "project_name",
                      "type", "client", "status", "retention_state",
                      "coverage"],
            // Land on the current financial year. Showing FY26 through
            // FY28 at once would make the totals describe nothing anyone
            // asked about.
            filterDefaults: { fy_label: [current.fy_label] },
            searchKeys: ["project_name", "job_code", "detail", "invoice_number"],
            onVisible: summarise,
            pageSize: 200,
            exportName: "invoicing",
            // Acting on a row reloads the screen. Without this the project
            // filter clears on every move, and re-selecting it each time
            // makes the grid unusable for the work it exists for.
            stateKey: "invoicing",
          })
        : stateMessage("No claims yet",
                       "Import the forecast, or add one.", false),
      // The lifecycle, written down where the work happens. It was in the
      // architecture document and nowhere a person doing the invoicing
      // would look, so the rules about reasons and approvals had to be
      // discovered by being refused.
      h("div", { class: "howto" },
        h("h2", null, "How a claim moves"),
        h("ol", null,
          h("li", null,
            h("b", null, "Forecast"), " \u2014 the work is scheduled into a "
            + "month. Change the month from the grid; no reason needed "
            + "while it is only a plan."),
          h("li", null,
            h("b", null, "Due"), " \u2014 the work is done and it can be "
            + "claimed. From here on, moving it to another month is "
            + "SLIPPAGE and asks why."),
          h("li", null,
            h("b", null, "Approved"), " \u2014 the customer has agreed the "
            + "claim. Ready to invoice."),
          h("li", null,
            h("b", null, "Invoiced"), " \u2014 the invoice has been issued. "
            + "Record the invoice number; retention is withheld here if the "
            + "project has terms."),
          h("li", null,
            h("b", null, "Paid"), " \u2014 the money has arrived.")),
        h("p", { class: "muted note" },
          "Each step can be stepped back. Going back out of ",
          h("b", null, "invoiced"), " or ", h("b", null, "paid"),
          " needs an approver and a reason, because it undoes something "
          + "that exists in Xero. A claim that is not going ahead is "
          + "CANCELLED rather than deleted: it keeps its history and drops "
          + "out of the totals."),
        h("p", { class: "muted note" },
          "The ", h("b", null, "Project forecast"), " column describes the "
          + "whole project, not the row it sits on. ",
          h("b", null, "under"),
          " means the project has work left to bill that is in no month "
          + "\u2014 add a forecast claim for the difference. ",
          h("b", null, "over"),
          " means its months add up to more than the contract allows "
          + "\u2014 usually an unrecorded variation."))));
  }

  mount(root, h("div", { class: "content" },
    h("div", { class: "page-head" },
      h("h1", null, "Invoicing"),
      h("span", { class: "eyebrow" }, "by end of month")),
    notice,
    body));

  await load();
}
