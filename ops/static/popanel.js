// poPanel.js — the customer orders behind a project.
//
// A project's contract value is the SUM OF ITS POs, not a number typed on
// the project. Some jobs force a new PO per invoice, so a dozen is normal;
// others run one order for the life of the work.
//
// Three operations, and conflating any two loses something:
//
//   NEW PO      separate scope, its own number, its own retention terms
//   VARIATION   the contract became bigger, ON A DATE. Figures before that
//               date were right.
//   CORRECTION  the recorded value was wrong. Figures before it were wrong
//               too, so correcting changes what they should have said.
//
// In the data a variation and a correction look identical -- `X -> Y`. They
// differ only when someone asks what orders in hand WAS at 30 June, and
// reproducing a past position is the thing this platform exists to do.

import { api, fmt, h, moneyInput, mount, toCents } from "./app.js";
import { field, sheet } from "./sheet.js";

// ONE dialog for raising an order, wherever it is raised from.
//
// The invoicing grid first did this with two `window.prompt` calls, which
// silently dropped the issue date and the note -- a worse version of a
// thing that already existed. Where an order is created from should change
// where it POSTs, not what can be recorded about it.
export function poDialog({ title, subtitle, presetCents, hint, submit }) {
  const controls = {
    po_number: field("PO number",
      h("input", { type: "text", "aria-label": "PO number" }),
      "Leave blank if it has not been issued yet"),
    amount_cents: field("Value",
      moneyInput(presetCents ?? 0, { "aria-label": "Value" }),
      hint || "Ex-GST"),
    issued_date: field("Issued",
      h("input", { type: "date", "aria-label": "Issued date" })),
    note: field("Note", h("textarea", { rows: 2, "aria-label": "Note" })),
  };
  sheet(title, subtitle,
    { fields: Object.values(controls).map((c) => c.wrap),
      controls: Object.values(controls), byKey: controls },
    "Add PO",
    async () => {
      const cents = toCents(controls.amount_cents.control.value);
      if (cents === null) {
        controls.amount_cents.setError("not an amount");
        throw new Error("");
      }
      await submit({
        po_number: controls.po_number.control.value.trim(),
        amount_cents: cents,
        issued_date: controls.issued_date.control.value || null,
        note: controls.note.control.value.trim(),
      });
    });
}

function addPoDialog(project, onDone, presetCents) {
  poDialog({
    title: `New PO \u00b7 ${project.name}`,
    subtitle: project.job_code,
    presetCents,
    submit: async (payload) => {
      await api("POST", `/api/projects/${project.id}/pos`, payload);
      onDone(`Added a PO of ${fmt.money(payload.amount_cents)}`);
    },
  });
}

function editPoDialog(po, project, onDone) {
  const applies = h("select", { "aria-label": "Retention" },
    h("option", { value: "0", selected: !po.retention_applies }, "No retention"),
    h("option", { value: "1", selected: !!po.retention_applies },
      "Retention applies"));
  const policy = h("select", { "aria-label": "Release" },
    h("option", { value: "dlp", selected: po.release_policy !== "split" },
      "All at end of DLP"),
    h("option", { value: "split", selected: po.release_policy === "split" },
      "Split: practical completion and DLP"));

  const controls = {
    po_number: field("PO number",
      h("input", { type: "text", value: po.po_number || "",
                   "aria-label": "PO number" })),
    issued_date: field("Issued",
      h("input", { type: "date", value: po.issued_date || "",
                   "aria-label": "Issued date" })),
    note: field("Note",
      h("textarea", { rows: 2, "aria-label": "Note" }, po.note || "")),
    retention_applies: field("Retention", applies,
      // Per PO, not per project: a variation raising this order raises its
      // cap, and scope run as a separate order carries its own terms or
      // none at all.
      "Terms belong to this order"),
    retention_cap_bp: field("Cap",
      h("input", { type: "number", min: "0", max: "10000", step: "1",
                   value: String(po.retention_cap_bp ?? 500),
                   "aria-label": "Cap basis points" }),
      "Basis points of this order: 500 = 5%"),
    retention_rate_bp: field("Withheld per claim",
      h("input", { type: "number", min: "0", max: "10000", step: "1",
                   value: String(po.retention_rate_bp ?? 1000),
                   "aria-label": "Rate basis points" }),
      "1000 = 10%, until the cap is reached"),
    release_policy: field("Release", policy),
    release_split_bp: field("Share at PC",
      h("input", { type: "number", min: "0", max: "10000", step: "1",
                   value: String(po.release_split_bp ?? 5000),
                   "aria-label": "Split basis points" }),
      "5000 = half"),
  };

  // Retention detail is noise on an order that has none.
  const syncRetention = () => {
    const on = applies.value === "1";
    for (const key of ["retention_cap_bp", "retention_rate_bp",
                       "release_policy", "release_split_bp"]) {
      controls[key].wrap.hidden = !on;
    }
    controls.release_split_bp.wrap.hidden = !on || policy.value !== "split";
  };
  applies.addEventListener("change", syncRetention);
  policy.addEventListener("change", syncRetention);
  syncRetention();

  sheet(`Edit ${po.po_number || "PO"} \u00b7 ${project.name}`,
    // The value is not here on purpose: changing it says whether the
    // contract grew or the figure was wrong, and that needs `Revise`.
    `${fmt.money(po.amount_cents)} \u2014 use Revise to change the value`,
    { fields: Object.values(controls).map((c) => c.wrap),
      controls: Object.values(controls), byKey: controls },
    "Save",
    async () => {
      const on = applies.value === "1";
      const number = (value) => {
        const n = Number(value);
        return Number.isFinite(n) ? Math.round(n) : null;
      };
      await api("PATCH", `/api/pos/${po.customer_po_id}`, {
        po_number: controls.po_number.control.value.trim() || null,
        issued_date: controls.issued_date.control.value || null,
        note: controls.note.control.value.trim() || null,
        retention_applies: on ? 1 : 0,
        retention_cap_bp: on ? number(controls.retention_cap_bp.control.value) : null,
        retention_rate_bp: on ? number(controls.retention_rate_bp.control.value) : null,
        release_policy: on ? policy.value : null,
        release_split_bp: on && policy.value === "split"
          ? number(controls.release_split_bp.control.value) : null,
      });
      onDone(`${controls.po_number.control.value.trim() || "PO"} updated`);
    });
}

function reviseDialog(po, project, onDone) {
  const kind = h("select", { "aria-label": "Kind" },
    h("option", { value: "variation" },
      "Variation \u2014 the contract changed"),
    h("option", { value: "correction" },
      "Correction \u2014 the figure was wrong"));
  const controls = {
    kind: field("What happened", kind,
      "A variation changes the contract from a date. A correction says it "
      + "was always the corrected figure."),
    amount_cents: field("New value",
      moneyInput(po.amount_cents, { "aria-label": "New value" }), "Ex-GST"),
    effective_date: field("Effective",
      h("input", { type: "date", "aria-label": "Effective date" }),
      "The day the contract changed"),
    reason: field("Reason",
      h("textarea", { rows: 2, "aria-label": "Reason" })),
  };
  // A correction has no date on which it became wrong.
  const syncKind = () => {
    controls.effective_date.wrap.hidden = kind.value !== "variation";
  };
  kind.addEventListener("change", syncKind);
  syncKind();

  sheet(`Revise ${po.po_number || "PO"} \u00b7 ${project.name}`,
    fmt.money(po.amount_cents),
    { fields: Object.values(controls).map((c) => c.wrap),
      controls: Object.values(controls), byKey: controls },
    "Record it",
    async () => {
      const cents = toCents(controls.amount_cents.control.value);
      if (cents === null) {
        controls.amount_cents.setError("not an amount");
        throw new Error("");
      }
      const result = await api("POST", `/api/pos/${po.customer_po_id}/revise`, {
        amount_cents: cents,
        kind: kind.value,
        reason: controls.reason.control.value.trim(),
        effective_date: controls.effective_date.control.value || null,
      });
      onDone(result.changed
        ? `${kind.value}: ${fmt.money(result.from)} \u2192 `
          + fmt.money(result.amount_cents)
        : "No change \u2014 the value was already that");
    });
}

function moveDialog(po, project, projects, onDone) {
  const target = h("select", { "aria-label": "Project" },
    h("option", { value: "" }, "Select a project"),
    projects
      .filter((p) => p.id !== project.id)
      .map((p) => h("option", { value: String(p.id) },
        `${p.name} \u00b7 ${p.job_code}`)));
  const controls = { project_id: field("Move to", target,
    "Only possible while no claims are billed against this order") };
  sheet(`Move ${po.po_number || "PO"}`, fmt.money(po.amount_cents),
    { fields: [controls.project_id.wrap],
      controls: [controls.project_id], byKey: controls },
    "Move it",
    async () => {
      if (!target.value) {
        controls.project_id.setError("pick a project");
        throw new Error("");
      }
      const result = await api("POST", `/api/pos/${po.customer_po_id}/move`,
                               { project_id: Number(target.value) });
      onDone(`${result.po_number || "PO"} moved to ${result.to}`);
    });
}

async function removePo(po, onDone) {
  if (!window.confirm(
      `Delete ${po.po_number || "this PO"} of ${fmt.money(po.amount_cents)}? `
      + "This cannot be undone.")) return;
  try {
    await api("DELETE", `/api/pos/${po.customer_po_id}`);
    onDone(`${po.po_number || "PO"} deleted`);
  } catch (err) {
    onDone(err.message);
  }
}

function remainderNote(data) {
  const remaining = data.remaining_cents ?? 0;
  const forecast = data.forecast_cents ?? 0;
  const gap = remaining - forecast;
  if (gap === 0) {
    return h("span", { class: "muted" },
      `\u00b7 ${fmt.money(remaining)} left to invoice, all of it forecast`);
  }
  return h("span", { class: "gap-note" },
    `\u00b7 ${fmt.money(remaining)} left to invoice, `
    + `${fmt.money(forecast)} forecast \u2014 `
    + (gap > 0
       // Work that is still to bill but sits in no month.
       ? `${fmt.money(gap)} not planned into a month yet`
       // More planned than the contract allows for.
       : `${fmt.money(-gap)} more planned than the contract covers`));
}

export async function poPanel(project, canWrite, onChange, onEdit,
                              projects, canDelete) {
  const data = await api("GET", `/api/projects/${project.id}/pos`);
  const revisions = {};
  for (const r of data.revisions) {
    (revisions[r.customer_po_id] = revisions[r.customer_po_id] || []).push(r);
  }

  // The contract is not an order and does not belong in a list of orders.
  // It is a migrated row carrying claims and retention, so it is described
  // in the heading instead -- counting it as ordered is what made a
  // $295,000 contract read $422,833.
  const orders = data.pos.filter((p) => !p.is_placeholder);
  const contractRows = data.pos.filter((p) => p.is_placeholder);
  const ordered = orders.reduce((sum, p) => sum + p.amount_cents, 0);
  const heldOnContract = contractRows.reduce(
    (sum, p) => sum + (p.held_cents || 0), 0);
  const claimsOnContract = contractRows.reduce(
    (sum, p) => sum + p.claim_count, 0);
  const invoicedOnContract = contractRows.reduce(
    (sum, p) => sum + p.claimed_cents, 0);
  const rows = orders.map((po) => {
    const history = revisions[po.customer_po_id] || [];
    return h("div", { class: "po" },
      h("div", { class: "po-head" },
        h("span", { class: "mono po-number" },
          po.po_number || "(no number)"),
        h("span", { class: "po-amount" }, fmt.money(po.amount_cents)),
        po.issued_date ? h("span", { class: "muted mono" }, po.issued_date) : null,
        po.retention_applies
          ? h("span", { class: "muted" },
              `retention ${fmt.rate(po.retention_cap_bp)}`
              + (po.held_cents ? `, ${fmt.money(po.held_cents)} held` : ""))
          : null,
        h("span", { class: "spacer" }),
        h("span", { class: "muted" },
          `${po.claim_count} claim${po.claim_count === 1 ? "" : "s"}`
          + (po.claimed_cents ? `, ${fmt.money(po.claimed_cents)} invoiced` : "")),
        canWrite
          ? h("span", { class: "row-actions" },
              h("button", { type: "button",
                            onclick: (e) => {
                              e.stopPropagation();
                              editPoDialog(po, project, onChange);
                            } }, "Edit"),
              h("button", { type: "button",
                            title: "Change the value, saying whether the "
                                   + "contract grew or the figure was wrong",
                            onclick: (e) => {
                              e.stopPropagation();
                              reviseDialog(po, project, onChange);
                            } }, "Revise"),
              // Only while nothing is billed against it: a claim carries
              // both the project and the PO, and moving one without the
              // other leaves them disagreeing.
              po.claim_count
                ? null
                : h("button", { type: "button",
                                onclick: (e) => {
                                  e.stopPropagation();
                                  moveDialog(po, project, projects || [], onChange);
                                } }, "Move"),
              canDelete && !po.claim_count
                ? h("button", { type: "button", class: "danger",
                                onclick: (e) => {
                                  e.stopPropagation();
                                  removePo(po, onChange);
                                } }, "Delete")
                : null)
          : null),
      po.note ? h("div", { class: "muted po-note" }, po.note) : null,
      // History, because a value that changed without a recorded reason is
      // a figure nobody can defend later.
      history.length
        ? h("div", { class: "po-history" }, history.map((r) => h("div",
            { class: "po-revision" },
            h("span", { class: `tag is-${r.kind || "unknown"}` },
              r.kind || "unrecorded"),
            h("span", { class: "mono" },
              `${fmt.money(Number(r.old_value))} \u2192 `
              + fmt.money(Number(r.new_value))),
            r.effective_date
              ? h("span", { class: "mono muted" }, `from ${r.effective_date}`)
              : null,
            h("span", { class: "muted" }, r.reason || ""),
            r.changed_by ? h("span", { class: "muted" }, r.changed_by) : null)))
        : null);
  });

  return h("div", { class: "po-panel" },
    h("div", { class: "po-panel-head" },
      h("h3", null,
        `Contract ${fmt.money(data.contract_value_cents ?? 0)}`),
      h("span", { class: "muted" },
        orders.length
          ? `\u00b7 ${fmt.money(ordered)} ordered across ${orders.length} PO`
            + `${orders.length === 1 ? "" : "s"}`
          : "\u00b7 no customer order raised yet"),
      // Where the claims actually sit. They hang off the migrated contract
      // row until real orders exist, and hiding that would leave someone
      // wondering which order was invoiced.
      // Left to invoice against what is actually planned. Equal is the
      // healthy case and says nothing; a gap is the finding.
      remainderNote(data),
      claimsOnContract
        ? h("span", { class: "muted" },
            `\u00b7 ${claimsOnContract} claim`
            + `${claimsOnContract === 1 ? "" : "s"} against the contract`
            + (invoicedOnContract
               ? `, ${fmt.money(invoicedOnContract)} invoiced` : "")
            + (heldOnContract
               ? `, ${fmt.money(heldOnContract)} retention held` : ""))
        : null,
      h("span", { class: "spacer" }),
      onEdit
        ? h("button", { type: "button",
                        onclick: (e) => { e.stopPropagation(); onEdit(); } },
            "Edit project")
        : null,
      canWrite
        ? h("button", { type: "button", class: "primary",
                        onclick: (e) => {
                          e.stopPropagation();
                          addPoDialog(project, onChange);
                        } }, "Add PO")
        : null),
    orders.length
      ? h("div", { class: "po-list" }, rows)
      : h("div", { class: "muted" },
          "No customer order raised yet. The contract is what the job is "
          + "worth; an order is what the customer has actually committed "
          + "to, and on some jobs those arrive as the work does."));
}

export { addPoDialog };
