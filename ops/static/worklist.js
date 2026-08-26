// worklist.js — decide the ambiguous job codes.
//
// Its emptiness is STP-5's gate, so the count is the headline figure: this
// screen exists to reach zero, not to be lived in.

import { api, fmt, h, mount, stateMessage } from "./app.js";

const ACTION_LABEL = {
  issue: "Issue the next number",
  assign: "Assign a known code",
  keep: "Keep as is",
  dismiss: "Not project work",
};

// keep and dismiss both leave the register looking wrong to the next reader.
const NEEDS_REASON = new Set(["keep", "dismiss"]);

function resolveDialog({ issue, nextCode, onDone }) {
  const action = h("select", { "aria-label": "Action" },
    Object.entries(ACTION_LABEL).map(([v, label]) =>
      h("option", { value: v, selected: v === (issue.class === "C" ? "keep" : "issue") },
        label)));

  const codeInput = h("input", { type: "text", placeholder: "JN-1234",
                                 "aria-label": "Job code" });
  const codeRow = h("label", { class: "field" },
    h("span", { class: "field-label" }, "Job code"), codeInput);

  const reasonInput = h("textarea", { rows: 2, "aria-label": "Reason",
    placeholder: "Why this is the right answer" });
  const reasonRow = h("label", { class: "field" },
    h("span", { class: "field-label" }, "Reason"), reasonInput,
    h("span", { class: "field-hint" },
      "Recorded against the resolution. Without it, this gets re-raised."));

  const preview = h("span", { class: "mono muted" });
  const error = h("div", { class: "form-error", hidden: true });
  const confirm = h("button", { type: "button", class: "primary" }, "Resolve");
  const cancel = h("button", { type: "button" }, "Cancel");

  function sync() {
    const a = action.value;
    codeRow.hidden = a !== "assign";
    // Class C always needs a reason; the schema requires it.
    reasonRow.hidden = !(NEEDS_REASON.has(a) || issue.class === "C");
    preview.textContent = a === "issue" ? `will become ${nextCode}` : "";
  }
  action.addEventListener("change", sync);

  const dialog = h("dialog", { class: "sheet", "aria-label": "Resolve job code" },
    h("div", { class: "sheet-head" },
      h("h2", null, issue.project_name),
      h("span", { class: "mono muted" }, issue.raw_code),
      h("span", { class: "tag warn" }, `class ${issue.class}`)),
    h("div", { class: "form-grid" },
      h("label", { class: "field" },
        h("span", { class: "field-label" }, "Action"), action, preview),
      codeRow, reasonRow),
    error,
    h("div", { class: "sheet-foot" },
      h("span", { class: "spacer" }), cancel, confirm));

  confirm.addEventListener("click", async () => {
    error.hidden = true;
    confirm.disabled = true;
    try {
      const result = await api("POST", `/api/worklist/${issue.id}/resolve`, {
        action: action.value,
        job_code: codeInput.value.trim() || null,
        reason: reasonInput.value.trim() || null,
      });
      dialog.close();
      const extra = result.cascaded.length
        ? ` — also closed ${result.cascaded.length} sibling issue${result.cascaded.length > 1 ? "s" : ""}, that code is no longer shared`
        : "";
      onDone(`${issue.project_name}: ${result.job_code}${extra}`);
    } catch (err) {
      const detail = err.detail;
      error.textContent = detail && typeof detail === "object"
        ? Object.entries(detail).map(([k, v]) => `${k}: ${v}`).join("; ")
        : err.message;
      error.hidden = false;
    } finally {
      confirm.disabled = false;
    }
  });

  cancel.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove());
  document.body.appendChild(dialog);
  sync();
  dialog.showModal();
}

let pending = null;

export async function render(root) {
  mount(root, stateMessage("Loading worklist", null, false));
  let data, me;
  try {
    [data, me] = await Promise.all([
      api("GET", "/api/worklist"), api("GET", "/api/me"),
    ]);
  } catch (err) {
    mount(root, stateMessage("Could not load the worklist", err.message, true));
    return;
  }

  const canResolve = me.roles.some((r) => r.role === "operations");
  const notice = h("div", { class: "notice", hidden: !pending });
  if (pending) { notice.textContent = pending; pending = null; }

  if (!data.issues.length) {
    mount(root, h("div", { class: "content" }, notice,
      stateMessage("Nothing to resolve",
        "Every job code is accounted for. This is the STP-5 gate.", false)));
    return;
  }

  const byClass = {};
  for (const i of data.issues) (byClass[i.class] ||= []).push(i);

  const section = (cls, issues) => h("div", { class: "worklist-group" },
    h("div", { class: "page-head" },
      h("h2", null, `Class ${cls}`),
      h("span", { class: "eyebrow" }, `${issues.length} open`)),
    h("p", { class: "group-help" }, data.help[cls]),
    h("div", { class: "table-wrap" },
      h("table", null,
        h("thead", null, h("tr", null,
          h("th", null, "Project"), h("th", null, "Code"),
          h("th", null, "Client"), h("th", null, "Type"),
          h("th", null, cls === "C" ? "Also on" : ""), h("th", null, ""))),
        h("tbody", null, issues.map((i) => h("tr", null,
          h("td", { class: "text-wide", title: i.project_name }, i.project_name),
          h("td", { class: "mono" }, i.raw_code),
          h("td", { class: "muted text", title: i.client }, i.client),
          h("td", { class: "mono" }, i.type),
          // Only meaningful for class C. Five projects holding the string
          // "TBA" is not a shared code, and showing "5" there reads as one.
          h("td", { class: "num" },
            cls === "C" && i.shared_by > 1 ? `${fmt.num(i.shared_by)} projects` : ""),
          h("td", { class: "num" },
            canResolve
              ? h("button", { type: "button",
                  onclick: () => resolveDialog({ issue: i,
                    nextCode: data.next_job_code,
                    onDone: (msg) => { pending = msg; render(root); } }) },
                  "Resolve")
              : h("span", { class: "muted" }, "operations role"))))))));

  mount(root, h("div", { class: "content" },
    h("div", { class: "page-head" },
      h("h1", null, "Job code worklist"),
      h("span", { class: "eyebrow" }, `${data.open} open`)),
    notice,
    h("div", { class: "figures" },
      h("div", { class: "figure is-attention" },
        h("span", { class: "label" }, "Open"),
        h("span", { class: "value" }, fmt.num(data.open))),
      h("div", { class: "figure" },
        h("span", { class: "label" }, "Next job number"),
        h("span", { class: "value" }, data.next_job_code))),
    Object.keys(byClass).sort().map((cls) => section(cls, byClass[cls]))));
}
