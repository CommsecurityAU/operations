// procurement.js — the register, and the two things the sheet cannot do.
//
// Recording WHEN something was delivered or paid, rather than what state
// somebody last typed; and showing committed cost against the project it
// belongs to.
//
// Payment and delivery are INDEPENDENT. `Paid, pending delivery` and
// `Delivered, unpaid` both exist and neither is a stage in a sequence, so
// the row offers both dates rather than a next-step button.

import { api, fmt, h, mount, stateMessage } from "./app.js";
import { datatable } from "./datatable.js";

// The dialogs live in their own module and load when one is opened: five
// forms is 15 KB that most visits never need.
const forms = () => import("./procurementforms.js");

let pending = null;

function figure(label, kind) {
  const value = document.createTextNode("");
  const el = h("div", { class: kind ? `figure ${kind}` : "figure" },
    h("span", { class: "label" }, label),
    h("span", { class: "value" }, value));
  return { el, set: (t) => { value.nodeValue = t; } };
}

function inlineSelect(current, options, ariaLabel, onChange) {
  const select = h("select", { class: "cell-select", "aria-label": ariaLabel },
    options.map((o) => h("option",
      { value: String(o.value), selected: o.value === current }, o.label)));
  select.addEventListener("change", () => onChange(select.value));
  return select;
}

export async function render(root) {
  mount(root, stateMessage("Loading procurement", null, false));
  let data, me;
  try {
    [data, me] = await Promise.all([
      api("GET", "/api/procurement"),
      api("GET", "/api/me"),
    ]);
  } catch (err) {
    mount(root, stateMessage("Could not load procurement", err.message, true));
    return;
  }
  const canWrite = new Set(me.roles.map((r) => r.role)).has("operations");

  const notice = h("div", { class: "notice", hidden: true });
  if (pending) {
    notice.textContent = pending;
    notice.hidden = false;
    pending = null;
  }

  const figures = {
    committed: figure("Committed", "is-primary"),
    estimated: figure("Estimated"),
    paid: figure("Paid"),
    undelivered: figure("Not yet delivered", "is-attention"),
    lines: figure("Lines"),
  };

  function summarise(visible) {
    const live = visible.filter((r) => !r.cancelled_date);
    const sum = (rows) => rows.reduce((t, r) => t + r.total_cents, 0);
    // An estimate is not a commitment: $1.57m of them beside $160k of
    // real orders would make committed cost wrong by a factor of ten.
    const real = live.filter((r) => !r.is_estimate);
    figures.committed.set(fmt.money(sum(real)));
    figures.estimated.set(fmt.money(sum(live.filter((r) => r.is_estimate))));
    // From the view, not from the dates: a line whose state says paid IS
    // paid, however it came to say so. Counting only dates made the screen
    // report $0.00 beside twenty rows reading `complete`.
    figures.paid.set(fmt.money(sum(real.filter((r) => r.is_paid))));
    figures.undelivered.set(
      fmt.money(sum(real.filter((r) => !r.is_delivered))));
    figures.lines.set(String(visible.length));
  }

  // One way in for every dialog: load the module, call the form, repaint
  // with whatever it says it did.
  async function open(name, ...args) {
    const module = await forms();
    module[name](...args, (_result, message) => {
      pending = message || _result || null;
      render(root);
    });
  }

  async function patch(line, payload, said) {
    try {
      await api("PATCH", `/api/procurement/${line.id}`, payload);
      pending = said;
    } catch (err) {
      const detail = err.detail;
      pending = detail && typeof detail === "object"
        ? Object.entries(detail).map(([k, v]) => `${k}: ${v}`).join("; ")
        : err.message;
    }
    render(root);
  }

  function eomCell(line) {
    if (!canWrite || line.cancelled_date) {
      return h("span", { class: "mono" }, line.period_label || "\u2013");
    }
    return inlineSelect(line.period_id,
      [{ value: "", label: "\u2013" },
       ...data.periods.map((p) => ({ value: p.id, label: p.label }))],
      `EOM for ${line.project_name}`,
      (value) => {
        const chosen = data.periods.find((p) => p.id === Number(value));
        patch(line, { period_id: Number(value) || null },
              `${line.project_name}: EOM ${chosen ? chosen.label : "cleared"}`);
      });
  }

  function stateCell(line) {
    // A line with real dates shows what they say and is not overridable
    // here: `when` beats `what someone said`, and a dropdown that silently
    // discarded a recorded date would be worse than no dropdown.
    const dated = line.delivered_date || line.paid_date || line.invoiced_date
                  || line.cancelled_date;
    if (!canWrite || dated) {
      return h("span", { class: "muted" }, line.state);
    }
    return inlineSelect(line.stated_state,
      [{ value: "", label: "\u2013" },
       ...data.states.map((v) => ({ value: v, label: v }))],
      `State of ${line.project_name}`,
      (value) => patch(line, { stated_state: value || null },
                       `${line.project_name}: ${value || "state cleared"}`));
  }

  // Naming what is missing rather than showing a dash: the dash is the
  // commonest state on this grid, and it is also the thing you most often
  // came to fix.
  function refCell(line, kind) {
    const has = kind === "po" ? line.po_number : line.quote_ref;
    if (has) return h("span", { class: "mono" }, has);
    if (!canWrite) return h("span", { class: "muted" }, "\u2013");
    const button = h("button", { type: "button", class: "link-button" },
      kind === "po" ? "Add PO" : "Add quote");
    button.addEventListener("click", async (e) => {
      e.stopPropagation();
      // Created and attached in one gesture: the reason it did not exist
      // is that nobody had reached this line yet.
      const attach = (made) => {
        if (!made) return;
        patch(line,
              kind === "po" ? { supplier_po_id: made.id }
                            : { supplier_quote_id: made.id },
              `${line.project_name}: `
              + (kind === "po" ? `order ${made.po_number}`
                               : `quote ${made.quote_ref || "(no ref)"}`)
              + " attached");
      };
      const module = await forms();
      if (kind === "po") {
        module.poDialog(data, attach, { supplier_id: line.supplier_id,
                                        project_id: line.project_id });
      } else {
        module.quoteDialog(data, attach, { supplier_id: line.supplier_id });
      }
    });
    return button;
  }

  const COLUMNS = [
    { key: "period_label", label: "EOM", sortKey: "month_start",
      fmt: (_v, r) => eomCell(r) },
    { key: "fy_label", label: "FY", cls: () => "mono" },
    { key: "project_name", label: "Project", cls: () => "text-wide" },
    { key: "job_code", label: "Job code", cls: () => "mono" },
    { key: "supplier_name", label: "Supplier", cls: () => "text",
      fmt: (v) => v || "\u2013" },
    { key: "item", label: "Item", cls: () => "text",
      fmt: (v, r) => (r.is_estimate
        ? h("span", null, v || "\u2013",
            h("span", { class: "tag", title: "not yet quoted or ordered" },
              "estimate"))
        : (v || "\u2013")) },
    { key: "quantity", label: "Qty", align: "right" },
    { key: "currency", label: "Cur", cls: () => "mono" },
    { key: "total_cents", label: "Cost", align: "right", fmt: fmt.moneyDash },
    { key: "quote_ref", label: "Quote", cls: () => "text",
      fmt: (_v, r) => refCell(r, "quote") },
    { key: "po_number", label: "PO", cls: () => "text",
      fmt: (_v, r) => refCell(r, "po") },
    { key: "invoice_ref", label: "Invoice", cls: () => "mono text",
      fmt: (v) => v || "\u2013" },
    // Amber where the state came from the imported sheet rather than a
    // date: it is the work still to do, and invisible provenance is how a
    // guess becomes a fact.
    { key: "state", label: "State", fmt: (_v, r) => stateCell(r),
      cls: (_v, r) => (r.state_undated ? "flagged-cell" : null) },
  ];

  if (canWrite) {
    // FIRST, not last. The actions are what you came to use, and putting
    // them past ten columns meant scrolling right to reach them on every
    // row.
    COLUMNS.unshift({
      key: "id", label: "", fmt: (_v, row) => h("span", { class: "row-actions" },
        h("button", { type: "button",
                      onclick: (e) => {
                        e.stopPropagation();
                        open("editDialog", row, data);
                      } }, "Edit"),
        h("button", { type: "button",
                      onclick: (e) => {
                        e.stopPropagation();
                        open("datesDialog", row);
                      } }, "Dates"),
        h("button", { type: "button",
                      onclick: (e) => {
                        e.stopPropagation();
                        open("invoiceDialog", row);
                      } }, "Invoice")),
    });
  }

  // A filter needs a value to filter on, and `is_estimate` is a number.
  for (const line of data.lines) {
    line.kind = line.is_estimate ? "estimate" : "ordered";
  }

  mount(root, h("div", { class: "content" },
    h("div", { class: "page-head" },
      h("h1", null, "Procurement"),
      h("span", { class: "eyebrow" }, "what a project costs to buy"),
      h("span", { class: "spacer" }),
      canWrite
        ? h("span", { class: "row-actions" },
            h("button", { type: "button",
                          onclick: () => open("quoteDialog", data) },
              "New quote"),
            h("button", { type: "button",
                          onclick: () => open("poDialog", data) },
              "New order"),
            h("button", { type: "button", class: "primary",
                          onclick: () => open("editDialog", null, data) },
              "New line"))
        : null),
    notice,
    h("div", { class: "figures" }, Object.values(figures).map((f) => f.el)),
    data.lines.length
      ? datatable({
          columns: COLUMNS,
          rows: data.lines,
          filters: ["fy_label", "period_label", "project_name",
                    "supplier_name", "state"],
          searchKeys: ["project_name", "job_code", "supplier_name", "item",
                       "description", "po_number", "invoice_ref"],
          onVisible: summarise,
          pageSize: 200,
          exportName: "procurement",
          stateKey: "procurement",
        })
      : stateMessage("Nothing ordered yet",
          "Import the procurement register, or add a line.", false)));
}
