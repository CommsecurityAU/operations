// projectform.js — create / edit a project.
//
// A native <dialog>: Escape closes it, focus is trapped, and the backdrop
// comes free. Rebuilding that by hand is where accessibility quietly goes.

import { api, h, moneyInput, mount, toCents } from "./app.js";

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
    // Shown as dollars, the same way the register shows them. A field that
    // presents money differently from the screen it came from is where a
    // typo hides.
    purchase_order_cents: field("Contract value",
      moneyInput(p.purchase_order_cents ?? 0, { "aria-label": "Contract value" }),
      "Ex-GST"),
    invoiced_prior_cents: field("Invoiced prior",
      moneyInput(p.invoiced_prior_cents ?? 0, { "aria-label": "Invoiced prior" }),
      "Before FY27, ex-GST"),
    notes: field("Notes", h("textarea", { rows: 2 }, p.notes || "")),
  };

  // Job code, on create only. The platform does NOT allocate: numbers still
  // come from iTrade, so one issued here could collide with one issued there
  // tomorrow, and the collision would surface in Xero (ADR-28). Either we
  // were given a code, or we say plainly that we do not have one yet.
  const codeMode = h("select", { "aria-label": "Job number" },
    h("option", { value: "defer" }, "Not assigned yet (goes to the worklist)"),
    h("option", { value: "existing" }, "It already has a code"));
  const codeInput = h("input", { type: "text", placeholder: "e.g. JN-6948",
                                 "aria-label": "Existing job code",
                                 hidden: true });
  const codeField = editing ? null : field("Job number",
    h("div", { class: "field-row" }, codeMode, codeInput));
  codeMode.addEventListener("change", () => {
    codeInput.hidden = codeMode.value !== "existing";
    if (!codeInput.hidden) codeInput.focus();
  });

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
  // Deliberately not part of the ordinary save: job_code stays immutable
  // through PATCH, and this is the priced exception.
  const fixCode = canDelete && editing
    ? h("button", { type: "button" }, "Correct job code")
    : null;

  const dialog = h("dialog", { class: "sheet", "aria-label": editing ? "Edit project" : "New project" },
    h("div", { class: "sheet-head" },
      h("h2", null, editing ? p.name : "New project"),
      editing ? h("span", { class: "mono muted" }, p.job_code) : null),
    leads, clients,
    h("div", { class: "form-grid" },
      Object.values(fields).map((f) => f.wrap),
      codeField ? codeField.wrap : null),
    summary,
    h("div", { class: "sheet-foot" },
      remove, fixCode, h("span", { class: "spacer" }), cancel, save));

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
        else if (codeField && (key === "job_code" || key === "job_code_mode")) {
          codeField.setError(message);
        } else unmatched.push(`${key}: ${message}`);
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
    if (codeField) {
      payload.job_code_mode = codeMode.value;
      if (codeMode.value === "existing") payload.job_code = codeInput.value.trim();
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

  if (fixCode) {
    fixCode.addEventListener("click", async () => {
      const code = window.prompt(
        `Correct the job code for ${p.name}.\nCurrently ${p.job_code}.`,
        p.job_code);
      if (!code || code.trim() === p.job_code) return;
      const reason = window.prompt("Why is the current code wrong?");
      if (!reason || !reason.trim()) return;
      clearErrors();
      try {
        await api("POST", `/api/projects/${p.id}/job-code`,
                  { job_code: code.trim(), reason: reason.trim() });
        dialog.close();
        onSaved(p, true, `Job code corrected to ${code.trim()}`);
      } catch (err) {
        showErrors(err);
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
