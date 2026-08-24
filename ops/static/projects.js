// projects.js — the project register screen.

import { api, fmt, h, mount, stateMessage } from "./app.js";
import { datatable } from "./datatable.js";

const COLUMNS = [
  { key: "name", label: "Project" },
  { key: "job_code", label: "Job code", cls: () => "mono" },
  { key: "status", label: "Status" },
  { key: "purchase_order_cents", label: "Contract", align: "right", fmt: fmt.money },
  { key: "invoiced_prior_cents", label: "Invoiced prior", align: "right", fmt: fmt.money },
  { key: "orders_in_hand_cents", label: "Orders in hand", align: "right",
    fmt: fmt.money, cls: (v) => (v < 0 ? "neg" : null) },
];

function figure(label, value, kind) {
  return h("div", { class: kind ? `figure ${kind}` : "figure" },
    h("span", { class: "label" }, label),
    h("span", { class: "value" }, value));
}

export async function render(root) {
  mount(root, stateMessage("Loading projects", null, false));
  let payload;
  try {
    payload = await api("GET", "/api/projects");
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

  const rows = payload.projects;
  if (!rows.length) {
    mount(root, stateMessage("No projects yet",
      "Import the register, or add the first project.", false));
    return;
  }

  const total = (key) => rows.reduce((sum, r) => sum + (r[key] || 0), 0);
  const flagged = rows.filter((r) => r.needs_resolution).length;

  mount(root, h("div", null,
    h("div", { class: "page-head" },
      h("h1", null, "Project register"),
      h("span", { class: "eyebrow" }, "FY27")),
    h("div", { class: "figures" },
      figure("Projects", fmt.num(rows.length)),
      figure("Contract value", fmt.money(total("purchase_order_cents"))),
      figure("Invoiced prior", fmt.money(total("invoiced_prior_cents"))),
      figure("Orders in hand", fmt.money(total("orders_in_hand_cents")), "is-primary"),
      // "Flagged", not "Need a job number": 4 of these DO have job numbers,
    // they just share one across two projects by work type. A label that
    // describes the wrong thing is worse than no label.
    flagged ? figure("Flagged for review", fmt.num(flagged), "is-exception") : null),
    datatable({
      columns: COLUMNS,
      rows,
      filters: ["status"],
      searchKeys: ["name", "job_code"],
      rowClass: (r) => (r.needs_resolution ? "flagged" : null),
    })));
}
