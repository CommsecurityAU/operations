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

import { api, fmt, h, mount, stateMessage } from "./app.js";
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
      forecast: figure("Forecast"),
      due: figure("Due"),
      approved: figure("Approved"),
      invoiced: figure("Invoiced", "is-primary"),
      retention: figure("Retention held", "is-attention"),
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
    }

    const COLUMNS = [
      { key: "fy_label", label: "FY", cls: () => "mono" },
      { key: "period_label", label: "Month", sortKey: "month_start",
        fmt: (_v, r) => monthCell(r) },
      { key: "project_name", label: "Project", cls: () => "text-wide" },
      { key: "job_code", label: "Job code", cls: () => "mono" },
      { key: "client", label: "Client", cls: () => "muted text" },
      { key: "type", label: "Type", cls: () => "mono" },
      { key: "detail", label: "Detail", cls: () => "muted text" },
      { key: "invoice_number", label: "Invoice", cls: () => "mono",
        fmt: (v) => v || "\u2013" },
      { key: "amount_cents", label: "Amount", align: "right", fmt: fmt.moneyDash },
      { key: "retention_state", label: "Retention", cls: () => "muted" },
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
                      "type", "client", "status", "retention_state"],
            // Land on the current financial year. Showing FY26 through
            // FY28 at once would make the totals describe nothing anyone
            // asked about.
            filterDefaults: { fy_label: [current.fy_label] },
            searchKeys: ["project_name", "job_code", "detail", "invoice_number"],
            onVisible: summarise,
            pageSize: 200,
            exportName: "invoicing",
          })
        : stateMessage("No claims yet",
                       "Import the forecast, or add one.", false)));
  }

  mount(root, h("div", { class: "content" },
    h("div", { class: "page-head" },
      h("h1", null, "Invoicing"),
      h("span", { class: "eyebrow" }, "by end of month")),
    notice,
    body));

  await load();
}
