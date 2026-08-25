// claims.js — the EOM invoicing grid.
//
// We invoice monthly, so the end-of-month is the axis: pick a month, see
// everything assigned to it, move claims along as the month closes. Invoices
// are raised in Xero from about the 18th, so this is the screen worked in
// that window.
//
// NO OPTIMISTIC UI. Every action sends, waits, and repaints from what the
// server returned. A grid that shows what you asked for rather than what
// happened is how a month gets closed on figures nobody checked.

import { api, fmt, h, mount, stateMessage } from "./app.js";

const STATUS_LABEL = {
  forecast: "Forecast", due: "Due", approved: "Approved",
  invoiced: "Invoiced", paid: "Paid", cancelled: "Cancelled",
};

// Fields the server insists on before a status may be entered. Kept in step
// with REQUIRED_ON_ENTRY in the module; the server refuses regardless, this
// only saves a round trip.
const REQUIRED = {
  approved: [["approved_date", "Approved date", "date"]],
  invoiced: [["invoice_number", "Invoice number", "text"],
             ["invoiced_date", "Invoice date", "date"]],
  paid: [["paid_date", "Payment received", "date"]],
};

const BACKWARD = { forecast: 0, due: 1, approved: 2, invoiced: 3, paid: 4 };

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
              "Recorded against the claim — this is what makes slippage and "
              + "reversals reportable"))
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

  const roles = new Set(me.roles.map((r) => r.role));
  const canWrite = roles.has("operations");

  // Default to the current month, which is the one being worked.
  const today = new Date().toISOString().slice(0, 10);
  const current = periods.periods.find((p) => p.month_end >= today)
    || periods.periods[0];
  let periodId = current.id;

  const figures = {
    forecast: figure("Forecast"),
    due: figure("Due"),
    approved: figure("Approved"),
    invoiced: figure("Invoiced", "is-primary"),
    paid: figure("Paid"),
    retention: figure("Retention held", "is-attention"),
  };

  const picker = h("select", { "aria-label": "End of month" },
    periods.periods.map((p) => h("option",
      { value: String(p.id), selected: p.id === periodId },
      `${p.label}  ${p.fy_label}`)));
  picker.addEventListener("change", () => {
    periodId = Number(picker.value);
    load();
  });

  const tbody = h("tbody");
  const count = h("span", { class: "count" });
  const notice = h("div", { class: "notice", hidden: true });

  function say(message) {
    notice.textContent = message;
    notice.hidden = !message;
  }

  async function move(claim, to) {
    const backward = BACKWARD[to] < BACKWARD[claim.status];
    const needsReason = backward || to === "cancelled";
    const send = async (payload) => {
      const result = await api("POST", `/api/claims/${claim.id}/status`, payload);
      // Repaint from the server's answer, never from the request.
      say(`${claim.project_name}: ${STATUS_LABEL[result.from]} \u2192 `
          + `${STATUS_LABEL[result.to]}`
          + (result.retention_cents
             ? `, retention ${fmt.money(result.retention_cents)} withheld` : ""));
      await load();
    };
    if ((REQUIRED[to] || []).length || needsReason) {
      transitionDialog(claim, to, needsReason, send);
    } else {
      try { await send({ status: to }); }
      catch (err) { say(err.message); }
    }
  }

  function statusControl(claim, transitions) {
    const allowed = transitions[claim.status] || [];
    if (!canWrite || !allowed.length || claim.is_opening_balance) {
      return h("span", { class: "tag" }, STATUS_LABEL[claim.status]);
    }
    const select = h("select", { "aria-label": `Status of ${claim.project_name}` },
      h("option", { value: "" }, STATUS_LABEL[claim.status]),
      allowed.map((s) => h("option", { value: s },
        `\u2192 ${STATUS_LABEL[s]}`)));
    select.addEventListener("change", () => {
      const to = select.value;
      select.value = "";
      if (to) move(claim, to);
    });
    return select;
  }

  async function load() {
    let payload;
    try {
      payload = await api("GET", `/api/claims?period=${periodId}`);
    } catch (err) {
      mount(root, stateMessage("Could not load claims", err.message, true));
      return;
    }
    const rows = payload.claims;
    for (const [key, fig] of Object.entries(figures)) {
      if (key === "retention") continue;
      fig.set(fmt.money(payload.totals[key] || 0));
    }
    figures.retention.set(fmt.money(payload.retention_cents || 0));
    figures.retention.el.hidden = !payload.retention_cents;

    while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
    for (const c of rows) {
      tbody.appendChild(h("tr", { class: c.is_opening_balance ? "flagged" : null },
        h("td", null, c.project_name),
        h("td", { class: "mono" }, c.job_code),
        h("td", { class: "muted" }, c.client),
        h("td", { class: "muted" }, c.detail || ""),
        h("td", { class: "mono" }, c.invoice_number || "\u2013"),
        h("td", { class: "num" }, fmt.moneyDash(c.amount_cents)),
        h("td", { class: "num zero" }, fmt.moneyDash(c.retention_cents)),
        h("td", null, statusControl(c, payload.transitions))));
    }
    mount(count, document.createTextNode(
      rows.length === 1 ? "1 claim" : `${rows.length} claims`));
    if (!rows.length) {
      tbody.appendChild(h("tr", null,
        h("td", { colspan: "8", class: "muted" },
          "Nothing assigned to this month yet.")));
    }
  }

  mount(root, h("div", { class: "content" },
    h("div", { class: "page-head" },
      h("h1", null, "Invoicing"),
      h("span", { class: "eyebrow" }, "by end of month")),
    notice,
    h("div", { class: "figures" }, Object.values(figures).map((f) => f.el)),
    h("div", { class: "controls" },
      picker, h("span", { class: "spacer" }), count),
    h("div", { class: "table-wrap" },
      h("table", null,
        h("thead", null, h("tr", null,
          h("th", null, "Project"),
          h("th", null, "Job code"),
          h("th", null, "Client"),
          h("th", null, "Detail"),
          h("th", null, "Invoice"),
          h("th", { class: "num" }, "Amount"),
          h("th", { class: "num" }, "Retention"),
          h("th", null, "Status"))),
        tbody))));

  await load();
}
