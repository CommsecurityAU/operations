// projects.js — the project register screen.

import { api, fmt, h, mount, stateMessage, typeCell } from "./app.js";
import { datatable } from "./datatable.js";

// Survives the re-render that follows a save.
let pending = null;

// Contract and Invoiced Prior are context; Orders in Hand is the answer.
// The hierarchy is carried by VALUE, not hue -- adding a second colour would
// spend the accent on a column that is not an exception.
const zero = (v) => (v === 0 ? "zero" : null);

const COLUMNS = [
  { key: "name", label: "Project", cls: () => "link text-wide" },
  { key: "job_code", label: "Job code", cls: () => "mono" },
  { key: "client", label: "Client", cls: () => "muted text" },
  { key: "type", label: "Type", fmt: (v) => typeCell(v) },
  { key: "status", label: "Status", cls: () => "muted" },
  { key: "project_lead", label: "Lead", cls: () => "muted text",
    fmt: (v) => v || "\u2013" },
  { key: "contract_value_cents", label: "Contract", align: "right",
    fmt: fmt.moneyDash, cls: (v) => ["secondary", zero(v)].filter(Boolean).join(" ") },
  // What the customer has actually raised an order for. On a job where POs
  // arrive as the work does, the gap against Contract is what has not been
  // ordered yet -- which the workbook could not show at all.
  { key: "ordered_cents", label: "Ordered", align: "right",
    fmt: fmt.moneyDash, cls: (v) => (v ? "secondary" : "secondary zero") },
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
  let payload, reference, me, renewals;
  try {
    [payload, reference, me, renewals] = await Promise.all([
      api("GET", "/api/projects"),
      api("GET", "/api/reference"),
      api("GET", "/api/me"),
      api("GET", "/api/renewals"),
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

  // The panel and the form are INTERACTIONS, not page load: nobody sees
  // either until they click. Loading them with the register put 17 KB on
  // every visit for dialogs most visits never open.
  const openForm = async (project) => {
    const { projectForm } = await import("./projectform.js");
    return projectForm({
      project, reference, canDelete,
      onSaved: (_s, _e, message) => { pending = message; render(root); },
      onDeleted: () => render(root),
    });
  };
  const openPanel = async (row) => {
    const { poPanel } = await import("./popanel.js");
    return poPanel(row, canWrite, (message) => {
      pending = message;
      render(root);
    }, canWrite ? () => openForm(row) : null, all, canDelete);
  };

  const notice = h("div", { class: "notice", hidden: true });

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
    ordered: figure("Ordered"),
    prior: figure("Invoiced prior"),
    oih: figure("Orders in hand", "is-primary"),
    retention: figure("Retention held"),
    flagged: figure("Flagged for review", "is-attention"),
  };
  const scope = document.createTextNode("FY27");

  function summarise(rows) {
    const total = (key) => rows.reduce((sum, r) => sum + (r[key] || 0), 0);
    const flagged = rows.filter((r) => r.needs_resolution).length;
    figures.projects.set(rows.length === all.length
      ? fmt.num(rows.length)
      : `${fmt.num(rows.length)} of ${fmt.num(all.length)}`);
    figures.contract.set(fmt.money(total("contract_value_cents")));
    figures.ordered.set(fmt.money(total("ordered_cents")));
    figures.prior.set(fmt.money(total("invoiced_prior_cents")));
    figures.oih.set(fmt.money(total("orders_in_hand_cents")));
    // Money the customer is holding, not yet released. Hidden when there
    // is none, so it never reads as a zero that means something.
    const held = total("retention_held_cents");
    figures.retention.set(fmt.money(held));
    figures.retention.el.hidden = held === 0;
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
    // Renewals live on the Schedules screen, but nothing sends you there.
    // Only overdue and due ones appear -- a reminder that is always
    // present stops being a reminder.
    renewals.renewals.length
      ? h("div", { class: "renewals" },
          h("h2", null, "Renewals"),
          renewals.renewals.map((r) => h("div",
            { class: `renewal is-${r.renewal_state}` },
            h("span", { class: "renewal-state" },
              r.renewal_state === "overdue" ? "Overdue" : "Due"),
            h("span", { class: "renewal-what" },
              `${r.project_name} \u00b7 ${r.description}`),
            h("span", { class: "mono" }, r.renewal_date || "\u2013"),
            h("span", { class: "muted" },
              r.days_until < 0 ? `${Math.abs(r.days_until)} days ago`
                               : `in ${r.days_until} days`),
            h("a", { href: "#schedules", class: "muted" }, "Schedules"))))
      : null,
    h("div", { class: "figures" },
      figures.projects.el, figures.contract.el, figures.ordered.el,
      figures.prior.el,
      figures.oih.el, figures.retention.el, figures.flagged.el),
    datatable({
      columns: COLUMNS,
      rows: all,
      // The same set as Invoicing, less the two that cannot apply: a
      // project has no financial year or month of its own -- it spans them.
      // Filtering the register by FY would be filtering by nothing.
      filters: ["name", "type", "status", "client", "project_lead"],
      searchKeys: ["name", "job_code", "client", "project_lead"],
      onVisible: summarise,
      rowClass: (r) => (r.needs_resolution ? "flagged" : null),
      // Clicking a row opens what the project IS -- its customer orders --
      // rather than a form to change it. Edit lives inside, because the
      // detail is the natural place to decide you want to change something.
      detail: openPanel,
      exportName: "project-register",
    })));
}
