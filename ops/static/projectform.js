// projectform.js — create / edit a project.
//
// A native <dialog>: Escape closes it, focus is trapped, and the backdrop
// comes free. Rebuilding that by hand is where accessibility quietly goes.

import { api, fmt, h, mount } from "./app.js";

const MONEY = /^-?\d*\.?\d{0,2}$/;

// Money is entered in dollars and stored in cents. The conversion happens
// HERE and nowhere else, and it goes through a string so 19.99 * 100 never
// becomes 1998.9999999999998.
function toCents(text) {
  const clean = String(text ?? "").replace(/[$,\s]/g, "");
  if (clean === "") return 0;
  if (!MONEY.test(clean)) return null;
  const neg = clean.startsWith("-");
  const [whole, frac = ""] = clean.replace("-", "").split(".");
  const cents = Number(whole || 0) * 100 + Number((frac + "00").slice(0, 2));
  return neg ? -cents : cents;
}

function toDollars(cents) {
  if (cents === null || cents === undefined) return "";
  return (cents / 100).toFixed(2);
}

function field(label, control, hint) {
  const error = h("span", { class: "field-error" });
  const wrap = h("label", { class: "field" },
    h("span", { class: "field-label" }, label),
    control,
    hint ? h("span", { class: "field-hint" }, hint) : null,
    error);
  return { wrap, control, setError: (m) => { error.textContent = m || ""; wrap.classList.toggle("has-error", !!m); } };
}

function select(options, value, placeholder) {
  return h("select", null,
    placeholder ? h("option", { value: "" }, placeholder) : null,
    options.map((o) => h("option",
      { value: String(o.value), selected: String(o.value) === String(value) },
      o.label)));
}

export function projectForm({ project, reference, onSaved, onDeleted, canDelete }) {
  const editing = !!project;
  const p = project || {};

  const fields = {
    name: field("Project name",
      h("input", { type: "text", value: p.name || "", maxlength: 200, required: true })),
    // A combobox, not a select: new clients appear, and forcing a user to
    // leave the form to add one is how projects get filed against the
    // nearest wrong client instead.
    client_name: field("Client",
      h("input", { type: "text", list: "clients", required: true,
                   value: p.client || "",
                   placeholder: "Pick one or type a new name" }),
      "Existing clients are offered as you type"),
    type_id: field("Type",
      select(reference.types.map((t) => ({ value: t.id, label: t.code })),
        p.type_id, "Select a type")),
    status: field("Status",
      select(reference.statuses.map((s) => ({ value: s, label: s })),
        p.status || "Active")),
    project_lead: field("Project lead",
      h("input", { type: "text", value: p.project_lead || "",
                   list: "leads", required: true }),
      "Every project has an owner"),
    project_no: field("Project no.",
      h("input", { type: "text", value: p.project_no || "" })),
    purchase_order_cents: field("Contract value",
      h("input", { type: "text", inputmode: "decimal",
                   value: toDollars(p.purchase_order_cents) }),
      "Dollars"),
    invoiced_prior_cents: field("Invoiced prior",
      h("input", { type: "text", inputmode: "decimal",
                   value: toDollars(p.invoiced_prior_cents) }),
      "Before FY27"),
    notes: field("Notes", h("textarea", { rows: 2 }, p.notes || "")),
  };

  const leads = h("datalist", { id: "leads" },
    reference.leads.map((l) => h("option", { value: l })));
  const clients = h("datalist", { id: "clients" },
    reference.clients.map((c) => h("option", { value: c.name })));

  const summary = h("div", { class: "form-error", hidden: true });
  const save = h("button", { type: "button", class: "primary" },
    editing ? "Save changes" : "Create project");
  const cancel = h("button", { type: "button" }, "Cancel");
  const remove = canDelete && editing
    ? h("button", { type: "button", class: "danger" }, "Delete")
    : null;

  const dialog = h("dialog", { class: "sheet", "aria-label": editing ? "Edit project" : "New project" },
    h("div", { class: "sheet-head" },
      h("h2", null, editing ? p.name : "New project"),
      editing ? h("span", { class: "mono muted" }, p.job_code) : null),
    leads, clients,
    h("div", { class: "form-grid" }, Object.values(fields).map((f) => f.wrap)),
    summary,
    h("div", { class: "sheet-foot" },
      remove, h("span", { class: "spacer" }), cancel, save));

  function clearErrors() {
    for (const f of Object.values(fields)) f.setError("");
    summary.hidden = true;
    summary.textContent = "";
  }

  function showErrors(err) {
    const detail = err.detail;
    if (detail && typeof detail === "object") {
      let unmatched = [];
      for (const [key, message] of Object.entries(detail)) {
        if (fields[key]) fields[key].setError(message);
        else unmatched.push(`${key}: ${message}`);
      }
      // A field error with nowhere to render is worse than a general one:
      // the form looks fine and the save silently fails.
      if (unmatched.length) {
        summary.textContent = unmatched.join("; ");
        summary.hidden = false;
      }
    } else {
      summary.textContent = err.message;
      summary.hidden = false;
    }
  }

  function collect() {
    const typedClient = fields.client_name.control.value.trim();
    const existing = reference.clients.find(
      (c) => c.name.toLowerCase() === typedClient.toLowerCase());
    const payload = {
      name: fields.name.control.value.trim(),
      // Send the id when the typed text matches a known client exactly, and
      // the name otherwise. The server does the near-miss matching -- doing
      // it here as well would put the rule in two places.
      client_id: existing ? existing.id : null,
      client_name: existing ? null : typedClient,
      type_id: Number(fields.type_id.control.value) || null,
      status: fields.status.control.value,
      project_lead: fields.project_lead.control.value.trim(),
      project_no: fields.project_no.control.value.trim(),
      notes: fields.notes.control.value.trim(),
    };
    for (const key of ["purchase_order_cents", "invoiced_prior_cents"]) {
      const cents = toCents(fields[key].control.value);
      if (cents === null) {
        fields[key].setError("not an amount");
        return null;
      }
      payload[key] = cents;
    }
    return payload;
  }

  save.addEventListener("click", async () => {
    clearErrors();
    const payload = collect();
    if (!payload) return;
    save.disabled = true;
    try {
      const response = editing
        ? await api("PATCH", `/api/projects/${p.id}`, payload)
        : await api("POST", "/api/projects", payload);
      const saved = editing ? response.project : response;
      const resolved = response.client_resolved;
      dialog.close();
      // Tell the user when their spelling was folded into an existing
      // client. Silently correcting it means they discover the difference
      // from a report months later.
      onSaved(saved, editing, resolved && resolved.reused_existing_spelling
        ? `Client "${resolved.typed}" matched the existing "${resolved.name}"`
        : null);
    } catch (err) {
      showErrors(err);
    } finally {
      save.disabled = false;
    }
  });

  if (remove) {
    remove.addEventListener("click", async () => {
      // Name the project in the prompt. "Are you sure?" is a question
      // nobody reads; the name is what makes someone stop.
      if (!window.confirm(`Delete ${p.job_code} ${p.name}? This cannot be undone.`)) return;
      clearErrors();
      remove.disabled = true;
      try {
        await api("DELETE", `/api/projects/${p.id}`);
        dialog.close();
        onDeleted(p);
      } catch (err) {
        showErrors(err);
      } finally {
        remove.disabled = false;
      }
    });
  }

  cancel.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove());
  document.body.appendChild(dialog);
  dialog.showModal();
  fields.name.control.focus();
  return dialog;
}
