// procurementforms.js — the dialogs behind the register.
//
// Loaded ON DEMAND, not with the screen: five forms is 15 KB that most
// visits never open, and the grid was within two kilobytes of the page
// budget with them bundled in.
//
// Each reports what it did rather than setting shared state, because a
// module that reaches back into the screen that opened it is a module that
// can only ever be opened from there.

import { api, fmt, h, moneyInput, mount, toCents } from "./app.js";
import { field, sheet } from "./sheet.js";

const DATE_LABEL = {
  requested_date: "Requested", ordered_date: "Ordered",
  invoiced_date: "Invoiced", delivered_date: "Delivered",
  paid_date: "Paid", cancelled_date: "Cancelled",
};

// Setting a date is the whole workflow, so it is one dialog with all of
// them rather than six buttons. Cancelling asks why: a line that vanishes
// from the cost without a reason is a figure nobody can explain at month
// end.
export function datesDialog(line, onDone) {
  const controls = {};
  for (const key of ["ordered_date", "invoiced_date", "delivered_date",
                     "paid_date"]) {
    controls[key] = field(DATE_LABEL[key],
      h("input", { type: "date", value: line[key] || "",
                   "aria-label": DATE_LABEL[key] }));
  }
  controls.cancelled_date = field("Cancelled",
    h("input", { type: "date", value: line.cancelled_date || "",
                 "aria-label": "Cancelled" }),
    "A cancelled line stops counting as a cost");
  controls.cancel_reason = field("Why cancelled",
    h("textarea", { rows: 2, "aria-label": "Cancel reason" },
      line.cancel_reason || ""));

  sheet(`${line.item || line.description || "Line"} \u00b7 ${line.project_name}`,
    `${line.supplier_name || "no supplier"} \u00b7 `
    + fmt.money(line.total_cents)
    + (line.state_undated
       ? ` \u00b7 the register said "${line.state}"` : ""),
    { fields: Object.values(controls).map((c) => c.wrap),
      controls: Object.values(controls), byKey: controls },
    "Save",
    async () => {
      const payload = {};
      for (const [key, control] of Object.entries(controls)) {
        payload[key] = control.control.value;
      }
      await api("PATCH", `/api/procurement/${line.id}`, payload);
      onDone(null, `${line.project_name}: dates updated`);
    });
}

// Everything a line IS, in one form. The dates dialog is separate because
// it is the workflow rather than the data -- what a thing is and where it
// has got to are different questions, and mixing them makes a long form
// that is mostly irrelevant whichever you came for.
export function editDialog(line, data, onDone) {
  const isNew = !line;
  const row = line || { quantity: 1, unit_cost_cents: 0, currency: "AUD" };

  const project = h("select", { "aria-label": "Project" },
    data.projects.map((p) => h("option",
      { value: String(p.id), selected: p.id === row.project_id },
      `${p.name} \u00b7 ${p.job_code || "no job code"}`)));
  const supplier = h("select", { "aria-label": "Supplier" },
    h("option", { value: "" }, "(none yet)"),
    data.suppliers.map((s) => h("option",
      { value: String(s.id), selected: s.id === row.supplier_id }, s.name)));
  const currency = h("select", { "aria-label": "Currency" },
    ["AUD", "USD"].map((c) => h("option",
      { value: c, selected: c === row.currency }, c)));
  const period = h("select", { "aria-label": "EOM" },
    h("option", { value: "" }, "(not scheduled)"),
    data.periods.map((p) => h("option",
      { value: String(p.id), selected: p.id === row.period_id },
      `${p.label}  ${p.fy_label}`)));
  const quote = h("select", { "aria-label": "Quote" });
  const po = h("select", { "aria-label": "Purchase order" });

  // Quotes and orders belong to a supplier, and an order to a project too.
  // Offering all of them would let a line be costed at another supplier's
  // rate.
  const refresh = () => {
    const supplierId = Number(supplier.value) || null;
    const projectId = Number(project.value) || null;
    mount(quote, h("option", { value: "" }, "(none)"));
    for (const q of data.quotes.filter((q) => q.supplier_id === supplierId)) {
      quote.appendChild(h("option",
        { value: String(q.id), selected: q.id === row.supplier_quote_id },
        `${q.quote_ref || "(no ref)"} \u00b7 ${q.currency}`
        + (q.fx_rate_bp ? ` @ ${fmt.fromBp(q.fx_rate_bp / 1000)}` : "")));
    }
    mount(po, h("option", { value: "" }, "(none)"));
    for (const p of data.pos.filter(
        (p) => p.supplier_id === supplierId && p.project_id === projectId)) {
      po.appendChild(h("option",
        { value: String(p.id), selected: p.id === row.supplier_po_id },
        p.po_number || "(no number)"));
    }
  };
  supplier.addEventListener("change", refresh);
  project.addEventListener("change", refresh);
  refresh();

  const controls = {
    project_id: field("Project", project, "Which job this is bought for"),
    supplier_id: field("Supplier", supplier),
    item: field("Item",
      h("input", { type: "text", value: row.item || "",
                   "aria-label": "Item" })),
    description: field("Description",
      h("textarea", { rows: 2, "aria-label": "Description" },
        row.description || "")),
    quantity: field("Quantity",
      h("input", { type: "number", min: "1", step: "1",
                   value: String(row.quantity || 1),
                   "aria-label": "Quantity" })),
    currency: field("Currency", currency),
    unit_cost_cents: field("Unit cost",
      moneyInput(row.unit_cost_cents || 0, { "aria-label": "Unit cost" }),
      "Ex-GST, in the currency above"),
    // A quote or an order usually does not exist until someone is already
    // entering the line it belongs to. Sending them elsewhere to make one
    // means losing what they have typed.
    supplier_quote_id: field("Quote",
      h("span", { class: "with-add" }, quote,
        h("button", { type: "button", class: "link-button",
                      onclick: () => quoteDialog(data, (made) => {
                        if (!made) return;
                        data.quotes.push(made);
                        row.supplier_quote_id = made.id;
                        refresh();
                      }, { supplier_id: Number(supplier.value) || null }) },
          "New")),
      "A USD line takes its rate from the quote"),
    supplier_po_id: field("Purchase order",
      h("span", { class: "with-add" }, po,
        h("button", { type: "button", class: "link-button",
                      onclick: () => poDialog(data, (made) => {
                        if (!made) return;
                        data.pos.push(made);
                        row.supplier_po_id = made.id;
                        refresh();
                      }, { supplier_id: Number(supplier.value) || null,
                           project_id: Number(project.value) || null }) },
          "New"))),
    period_id: field("EOM", period, "The month payment is expected"),
    note: field("Note",
      h("textarea", { rows: 2, "aria-label": "Note" }, row.note || "")),
  };

  sheet(isNew ? "New procurement line"
              : `${row.item || "Line"} \u00b7 ${row.project_name}`,
    isNew ? "" : fmt.money(row.total_cents),
    { fields: Object.values(controls).map((c) => c.wrap),
      controls: Object.values(controls), byKey: controls },
    isNew ? "Add line" : "Save",
    async () => {
      const cents = toCents(controls.unit_cost_cents.control.value);
      if (cents === null) {
        controls.unit_cost_cents.setError("not an amount");
        throw new Error("");
      }
      const payload = {
        project_id: Number(project.value),
        supplier_id: Number(supplier.value) || null,
        item: controls.item.control.value.trim(),
        description: controls.description.control.value.trim(),
        quantity: Number(controls.quantity.control.value) || 1,
        currency: currency.value,
        unit_cost_cents: cents,
        supplier_quote_id: Number(quote.value) || null,
        supplier_po_id: Number(po.value) || null,
        period_id: Number(period.value) || null,
        note: controls.note.control.value.trim(),
      };
      if (isNew) {
        await api("POST", "/api/procurement", payload);
        onDone(null, "Line added");
      } else {
        await api("PATCH", `/api/procurement/${row.id}`, payload);
        onDone(null, `${row.project_name}: line updated`);
      }
    });
}

// A quote exists to carry the FX rate, so a USD purchase cannot be recorded
// without one.
// Takes a preset supplier and reports what it created, so the same dialog
// serves the header button, the edit form and the grid. A quote usually
// does not exist until someone is already entering the line it belongs to.
export function quoteDialog(data, onDone, preset) {
  preset = preset || {};
  const supplier = h("select", { "aria-label": "Supplier" },
    data.suppliers.map((s) => h("option",
      { value: String(s.id), selected: s.id === preset.supplier_id }, s.name)));
  const chosen = data.suppliers.find((s) => s.id === preset.supplier_id);
  const currency = h("select", { "aria-label": "Currency" },
    ["AUD", "USD"].map((c) => h("option",
      { value: c, selected: chosen && c === chosen.default_currency }, c)));
  const controls = {
    supplier_id: field("Supplier", supplier),
    quote_ref: field("Reference",
      h("input", { type: "text", "aria-label": "Reference" })),
    quote_date: field("Date",
      h("input", { type: "date", "aria-label": "Date" })),
    currency: field("Currency", currency),
    fx_rate: field("Rate",
      h("input", { type: "number", step: "0.000001", min: "0",
                   "aria-label": "Rate" }),
      "AUD per unit of the foreign currency, as agreed with the supplier "
      + "\u2014 e.g. 1.388561"),
    email_subject: field("Email subject",
      h("input", { type: "text", "aria-label": "Email subject" }),
      "So the correspondence can be found months later"),
  };
  const syncRate = () => {
    controls.fx_rate.wrap.hidden = currency.value === "AUD";
  };
  currency.addEventListener("change", syncRate);
  syncRate();

  sheet("New quote", "",
    { fields: Object.values(controls).map((c) => c.wrap),
      controls: Object.values(controls), byKey: controls },
    "Add quote",
    async () => {
      const made = await api("POST", "/api/procurement/quotes", {
        supplier_id: Number(supplier.value),
        quote_ref: controls.quote_ref.control.value.trim(),
        quote_date: controls.quote_date.control.value || null,
        currency: currency.value,
        fx_rate: Number(controls.fx_rate.control.value) || null,
        email_subject: controls.email_subject.control.value.trim(),
      });
      onDone(made, `Quote ${made.quote_ref || "(no ref)"} added`);
    });
}

export function poDialog(data, onDone, preset) {
  preset = preset || {};
  const project = h("select", { "aria-label": "Project" },
    data.projects.map((p) => h("option",
      { value: String(p.id), selected: p.id === preset.project_id },
      `${p.name} \u00b7 ${p.job_code || "no job code"}`)));
  const supplier = h("select", { "aria-label": "Supplier" },
    data.suppliers.map((s) => h("option",
      { value: String(s.id), selected: s.id === preset.supplier_id }, s.name)));
  const controls = {
    project_id: field("Project", project),
    supplier_id: field("Supplier", supplier),
    po_number: field("PO number",
      h("input", { type: "text", "aria-label": "PO number" })),
    po_date: field("Date", h("input", { type: "date", "aria-label": "Date" })),
    approved_by: field("Approved by",
      h("input", { type: "text", "aria-label": "Approved by" })),
    approved_date: field("Approved",
      h("input", { type: "date", "aria-label": "Approved date" })),
  };
  sheet("New purchase order", "generally one per project",
    { fields: Object.values(controls).map((c) => c.wrap),
      controls: Object.values(controls), byKey: controls },
    "Add order",
    async () => {
      const made = await api("POST", "/api/procurement/pos", {
        project_id: Number(project.value),
        supplier_id: Number(supplier.value),
        po_number: controls.po_number.control.value.trim(),
        po_date: controls.po_date.control.value || null,
        approved_by: controls.approved_by.control.value.trim(),
        approved_date: controls.approved_date.control.value || null,
      });
      onDone(made, `Order ${made.po_number} added`);
    });
}

export function invoiceDialog(line, onDone) {
  const controls = {
    invoice_ref: field("Invoice reference",
      h("input", { type: "text", value: line.invoice_ref || "",
                   "aria-label": "Invoice reference" }),
      "One invoice often covers several orders \u2014 the same reference "
      + "attaches them all"),
    invoice_date: field("Invoice date",
      h("input", { type: "date", "aria-label": "Invoice date" })),
    due_date: field("Due", h("input", { type: "date", "aria-label": "Due" })),
  };
  sheet(`Invoice \u00b7 ${line.project_name}`,
    line.supplier_name || "no supplier",
    { fields: Object.values(controls).map((c) => c.wrap),
      controls: Object.values(controls), byKey: controls },
    "Attach",
    async () => {
      const result = await api("POST", `/api/procurement/${line.id}/invoice`, {
        invoice_ref: controls.invoice_ref.control.value.trim(),
        invoice_date: controls.invoice_date.control.value || null,
        due_date: controls.due_date.control.value || null,
      });
      onDone(null, result.created
        ? `Invoice ${result.invoice.invoice_ref} created and attached`
        : `Attached to existing invoice ${result.invoice.invoice_ref}`);
    });
}

// The EOM and the state ARE the controls, the way the month cell is on the
// invoicing grid. Changing either is the commonest thing anyone does here,
// and a dialog would put two clicks in front of one decision.

// Deleting is for a row that should NEVER have existed -- a duplicate, a
// mistyped entry. Cancelling is for one that was real and is not any more,
// and that leaves a trace on purpose. The dialog says which this is,
// because they are one click apart and only one of them is reversible by
// reading the audit log.
export function deleteDialog(line, onDone) {
  const controls = {
    reason: field("Why is this being deleted",
      h("textarea", { rows: 2, "aria-label": "Reason" }),
      "Kept in the audit log with the whole row, so it can be read back"),
  };
  sheet(`Delete \u00b7 ${line.item || line.description || "line"}`,
    `${line.project_name} \u00b7 ${fmt.money(line.total_cents)}`
    + (line.is_estimate ? " \u00b7 an estimate" : ""),
    { fields: [
        h("p", { class: "muted note" },
          "Delete a row that should not exist \u2014 entered twice, or "
          + "superseded by a real purchase. If it was a real order that is "
          + "no longer going ahead, CANCEL it instead: that keeps it "
          + "visible and out of the totals."),
        ...Object.values(controls).map((c) => c.wrap),
      ],
      controls: Object.values(controls), byKey: controls },
    "Delete",
    async () => {
      const reason = controls.reason.control.value.trim();
      if (!reason) {
        controls.reason.setError("required");
        throw new Error("");
      }
      const gone = await api("DELETE", `/api/procurement/${line.id}`,
                             { reason });
      onDone(null, `Deleted ${gone.item || "line"} on ${gone.project}`);
    });
}
