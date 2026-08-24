// projects.js — the project register screen.

import { api, fmt, h, mount, stateMessage } from "./app.js";
import { datatable } from "./datatable.js";
import { projectForm } from "./projectform.js";

// Survives the re-render that follows a save.
let pending = null;

// Contract and Invoiced Prior are context; Orders in Hand is the answer.
// The hierarchy is carried by VALUE, not hue -- adding a second colour would
// spend the accent on a column that is not an exception.
const zero = (v) => (v === 0 ? "zero" : null);

const COLUMNS = [
  { key: "name", label: "Project", cls: () => "link" },
  { key: "job_code", label: "Job code", cls: () => "mono" },
  { key: "client", label: "Client", cls: () => "muted" },
  { key: "type", label: "Type", cls: () => "mono" },
  { key: "status", label: "Status", cls: () => "muted" },
  { key: "purchase_order_cents", label: "Contract", align: "right",
    fmt: fmt.moneyDash, cls: (v) => ["secondary", zero(v)].filter(Boolean).join(" ") },
  { key: "invoiced_prior_cents", label: "Invoiced prior", align: "right",
    fmt: fmt.moneyDash, cls: (v) => ["secondary", zero(v)].filter(Boolean).join(" ") },
  { key: "orders_in_hand_cents", label: "Orders in hand", align: "right",
    fmt: fmt.moneyDash,
    cls: (v) => (v < 0 ? "neg" : (v === 0 ? "zero" : "primary")) },
];

function figure(label, kind) {
  const value = document.createTextNode("");
  const el = h("div", { class: kind ? `figure ${kind}` : "figure" },
    h("span", { class: "label" }, label),
    h("span", { class: "value" }, value));
  return { el, set: (text) => { value.nodeValue = text; } };
}

export async function render(root) {
  mount(root, stateMessage("Loading projects", null, false));
  let payload, reference, me;
  try {
    [payload, reference, me] = await Promise.all([
      api("GET", "/api/projects"),
      api("GET", "/api/reference"),
      api("GET", "/api/me"),
    ]);
  } catch (err) {
    // The server's own wording, plus what to do about it.
    mount(root, stateMessage(
      err.status === 403 ? "No entity access" : "Could not load projects",
      err.status === 403
        ? "Your account has no role on any entity yet. An administrator grants access; it applies on your next click, without signing in again."
        : err.message,
      true));
    return;
  }

  const all = payload.projects;
  const roles = new Set(me.roles.map((r) => r.role));
  const canWrite = roles.has("operations");
  const canDelete = roles.has("admin");

  const notice = h("div", { class: "notice", hidden: true });
  const openForm = (project) => projectForm({
    project, reference, canDelete,
    onSaved: (_saved, _editing, message) => {
      pending = message;
      render(root);
    },
    onDeleted: () => render(root),
  });

  if (!all.length) {
    if (pending) {
    notice.textContent = pending;
    notice.hidden = false;
    pending = null;
  }

  mount(root, h("div", { class: "content" },
      stateMessage("No projects yet",
        canWrite ? "Add the first one, or import the register."
                 : "Nothing has been added on your entities yet.", false),
      canWrite ? h("div", { class: "state-action" },
        h("button", { type: "button", class: "primary",
                      onclick: () => openForm(null) }, "New project")) : null));
    return;
  }

  // Figures are built ONCE and their values updated as filters change --
  // rebuilding them would drop the filter panel's focus and close it.
  const figures = {
    projects: figure("Projects"),
    contract: figure("Contract value"),
    prior: figure("Invoiced prior"),
    oih: figure("Orders in hand", "is-primary"),
    flagged: figure("Flagged for review", "is-attention"),
  };
  const scope = document.createTextNode("FY27");

  function summarise(rows) {
    const total = (key) => rows.reduce((sum, r) => sum + (r[key] || 0), 0);
    const flagged = rows.filter((r) => r.needs_resolution).length;
    figures.projects.set(rows.length === all.length
      ? fmt.num(rows.length)
      : `${fmt.num(rows.length)} of ${fmt.num(all.length)}`);
    figures.contract.set(fmt.money(total("purchase_order_cents")));
    figures.prior.set(fmt.money(total("invoiced_prior_cents")));
    figures.oih.set(fmt.money(total("orders_in_hand_cents")));
    figures.flagged.set(fmt.num(flagged));
    // Say plainly when the figures describe a subset. A total that silently
    // means something narrower than its label is how a dashboard misleads.
    scope.nodeValue = rows.length === all.length ? "FY27" : "FY27 \u00b7 filtered";
    figures.flagged.el.hidden = flagged === 0;
  }

  if (pending) {
    notice.textContent = pending;
    notice.hidden = false;
    pending = null;
  }

  mount(root, h("div", { class: "content" },
    h("div", { class: "page-head" },
      h("h1", null, "Project register"),
      h("span", { class: "eyebrow" }, scope),
      h("span", { class: "spacer" }),
      canWrite ? h("button", { type: "button", class: "primary",
                               onclick: () => openForm(null) },
                   "New project") : null),
    notice,
    h("div", { class: "figures" },
      figures.projects.el, figures.contract.el, figures.prior.el,
      figures.oih.el, figures.flagged.el),
    datatable({
      columns: COLUMNS,
      rows: all,
      filters: ["type", "status", "client"],
      searchKeys: ["name", "job_code", "client", "project_lead"],
      onVisible: summarise,
      rowClass: (r) => (r.needs_resolution ? "flagged" : null),
      // Read-only users get no click affordance at all, rather than a row
      // that opens a form they cannot save.
      onRowClick: canWrite ? openForm : null,
    })));
}
