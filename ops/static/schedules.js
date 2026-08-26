// schedules.js — recurring claims, and when the agreement is up.
//
// Maintenance is one agreement spread over a year, not twelve claims
// someone typed. The renewal date is the point: an agreement that lapses
// unnoticed is revenue that simply stops, and a spreadsheet of twelve rows
// never tells you it is about to.
//
// Every recurring project arrived with its rows ALREADY TYPED, so a new
// schedule adopts them rather than generating over the top. Generate then
// fills only the gaps.

import { api, fmt, h, moneyInput, mount, stateMessage, toCents } from "./app.js";

// Survives the re-render that follows an action. Setting a notice and then
// rebuilding the screen wipes it -- which made `Adopt` look like it had
// done nothing at all, when what it had done was correctly report that
// there was nothing left to adopt.
let pending = null;

const STATE_LABEL = {
  overdue: "Overdue", due: "Due", future: "Upcoming", "no date set": "No date",
};

function field(label, control, hint) {
  const error = h("span", { class: "field-error" });
  const wrap = h("label", { class: "field" },
    h("span", { class: "field-label" }, label),
    control,
    hint ? h("span", { class: "field-hint" }, hint) : null,
    error);
  return { wrap, control, setError: (m) => {
    error.textContent = m || "";
    wrap.classList.toggle("has-error", !!m);
  } };
}

function scheduleForm({ schedule, projects, periods, onSaved }) {
  const editing = !!schedule;
  const s = schedule || {};

  const project = h("select", { "aria-label": "Project" },
    h("option", { value: "" }, "Select a project"),
    projects.map((p) => h("option", { value: String(p.id) },
      `${p.name} \u00b7 ${p.job_code}`)));
  const period = (selected) => h("select", { "aria-label": "Period" },
    periods.map((p) => h("option",
      { value: String(p.id), selected: p.id === selected },
      `${p.label}  ${p.fy_label}`)));

  const fields = {
    description: field("Description",
      h("input", { type: "text", value: s.description || "",
                   "aria-label": "Description" }),
      "Labels every claim this makes"),
    amount_cents: field("Amount each time",
      moneyInput(s.amount_cents ?? 0, { "aria-label": "Amount" }), "Ex-GST"),
    frequency: field("Frequency",
      h("select", { "aria-label": "Frequency" },
        ["monthly", "quarterly", "annual"].map((f) => h("option",
          { value: f, selected: f === s.frequency }, f)))),
    start_period_id: field("First month", period(s.start_period_id)),
    end_period_id: field("Last month", period(s.end_period_id)),
    renewal_date: field("Renewal date",
      h("input", { type: "date", value: s.renewal_date || "",
                   "aria-label": "Renewal date" }),
      "When the agreement itself is up"),
    renewal_notice_days: field("Notice",
      h("input", { type: "number", min: "0",
                   value: String(s.renewal_notice_days ?? 60),
                   "aria-label": "Notice days" }),
      "Days before it starts chasing"),
    renewal_note: field("Renewal note",
      h("textarea", { rows: 2, "aria-label": "Renewal note" },
        s.renewal_note || "")),
  };

  const summary = h("div", { class: "form-error", hidden: true });
  const save = h("button", { type: "button", class: "primary" },
    editing ? "Save" : "Create schedule");
  const cancel = h("button", { type: "button" }, "Cancel");

  const dialog = h("dialog", { class: "sheet", "aria-label": "Schedule" },
    h("div", { class: "sheet-head" },
      h("h2", null, editing ? s.description : "New schedule"),
      editing ? h("span", { class: "mono muted" }, s.project_name) : null),
    h("div", { class: "form-grid" },
      editing ? null : field("Project", project).wrap,
      Object.values(fields).map((f) => f.wrap)),
    summary,
    h("div", { class: "sheet-foot" },
      h("span", { class: "spacer" }), cancel, save));

  save.addEventListener("click", async () => {
    for (const f of Object.values(fields)) f.setError("");
    summary.hidden = true;
    const cents = toCents(fields.amount_cents.control.value);
    if (cents === null) {
      fields.amount_cents.setError("not an amount");
      return;
    }
    const payload = {
      description: fields.description.control.value.trim(),
      amount_cents: cents,
      frequency: fields.frequency.control.value,
      start_period_id: Number(fields.start_period_id.control.value),
      end_period_id: Number(fields.end_period_id.control.value),
      renewal_date: fields.renewal_date.control.value || null,
      renewal_notice_days: Number(fields.renewal_notice_days.control.value),
      renewal_note: fields.renewal_note.control.value.trim(),
    };
    if (!editing) {
      const chosen = Number(project.value);
      if (!chosen) {
        summary.textContent = "Pick a project";
        summary.hidden = false;
        return;
      }
      payload.project_id = chosen;
      const p = projects.find((x) => x.id === chosen);
      payload.customer_po_id = p && p.customer_po_id;
    }
    save.disabled = true;
    try {
      const result = editing
        ? await api("PATCH", `/api/schedules/${s.schedule_id}`, payload)
        : await api("POST", "/api/schedules", payload);
      dialog.close();
      // Say what adoption did. A schedule that silently attached twelve
      // existing claims looks identical to one that did nothing.
      const adopted = result.adopted && result.adopted.adopted;
      onSaved(adopted
        ? `Adopted ${adopted} existing claim${adopted === 1 ? "" : "s"}`
        : null);
    } catch (err) {
      const detail = err.detail;
      if (detail && typeof detail === "object") {
        let rest = [];
        for (const [key, message] of Object.entries(detail)) {
          if (fields[key]) fields[key].setError(message);
          else rest.push(`${key}: ${message}`);
        }
        if (rest.length) {
          summary.textContent = rest.join("; ");
          summary.hidden = false;
        }
      } else {
        summary.textContent = err.message;
        summary.hidden = false;
      }
    } finally {
      save.disabled = false;
    }
  });
  cancel.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove());
  document.body.appendChild(dialog);
  dialog.showModal();
  return dialog;
}

export async function render(root) {
  mount(root, stateMessage("Loading schedules", null, false));
  let data, periods, projects, me;
  try {
    [data, periods, projects, me] = await Promise.all([
      api("GET", "/api/schedules"),
      api("GET", "/api/periods"),
      api("GET", "/api/projects"),
      api("GET", "/api/me"),
    ]);
  } catch (err) {
    mount(root, stateMessage("Could not load schedules", err.message, true));
    return;
  }
  const canWrite = new Set(me.roles.map((r) => r.role)).has("operations");
  const notice = h("div", { class: "notice", hidden: true });
  if (pending) {
    notice.textContent = pending;
    notice.hidden = false;
    pending = null;
  }
  // `say` for messages that stay on this render; `report` for ones that
  // must outlive it.
  const say = (m) => { notice.textContent = m || ""; notice.hidden = !m; };
  const report = (m) => { pending = m; render(root); };
  const reload = (message) => report(message);

  async function act(schedule, what) {
    try {
      const result = await api(
        "POST", `/api/schedules/${schedule.schedule_id}/${what}`);
      if (what === "adopt") {
        const spare = (result.not_adopted || []).length;
        // A month holding two claims is worth saying out loud: the schedule
        // can only own one of them, so the other is either a genuine extra
        // or a duplicate somebody should look at.
        const leftover = spare
          ? ` \u2014 ${spare} claim${spare === 1 ? "" : "s"} left unattached, `
            + `in ${[...new Set(result.not_adopted.map((x) => x.period))].join(", ")}`
            + " (a month can hold more than one, this schedule owns one)"
          : "";
        report(result.adopted
          ? `${schedule.description}: adopted ${result.adopted} existing `
            + `claim${result.adopted === 1 ? "" : "s"}`
              + (result.differing.length
                 ? `, ${result.differing.length} at a different amount` : "")
              + leftover
          : `${schedule.description}: nothing to adopt \u2014 every period `
            + "is already attached to this schedule" + leftover);
      } else {
        report(result.created
          ? `${schedule.description}: created ${result.created} claim`
            + `${result.created === 1 ? "" : "s"} (${result.periods.join(", ")})`
          : `${schedule.description}: every period already covered`);
      }
    } catch (err) {
      say(err.message);
    }
  }

  // What each schedule actually covers, month by month. The endpoint has
  // existed since the module was written; without it the only way to learn
  // what Generate would do was to press it.
  const opened = new Set();

  async function detailRow(schedule, colspan) {
    const cell = h("td", { colspan: String(colspan) }, "Loading\u2026");
    const tr = h("tr", { class: "detail-row" }, cell);
    try {
      const preview = await api(
        "GET", `/api/schedules/${schedule.schedule_id}/preview`);
      mount(cell, h("div", { class: "periods" },
        preview.periods.map((p) => {
          const extra = p.others.length
            ? h("span", { class: "period-extra" },
                `+${p.others.length} other`)
            : null;
          return h("div", { class: `period is-${p.state}` },
            h("span", { class: "period-month" }, p.label),
            h("span", { class: "period-amount" },
              p.claim ? fmt.money(p.claim.amount_cents) : "\u2013"),
            h("span", { class: "period-state" },
              p.state === "mine" ? (p.claim.status)
                : p.state === "unattached" ? "not this schedule"
                : "no claim"),
            extra);
        })));
    } catch (err) {
      mount(cell, document.createTextNode(err.message));
    }
    return tr;
  }

  const rows = data.schedules;
  const chasing = rows.filter((r) => r.renewal_state === "overdue"
                                  || r.renewal_state === "due");

  // Toggling is DOM surgery rather than a re-render: rebuilding the screen
  // would close every other row and lose the scroll position.
  async function toggle(event, schedule) {
    const tr = event.currentTarget;
    const next = tr.nextElementSibling;
    if (next && next.classList.contains("detail-row")) {
      next.remove();
      opened.delete(schedule.schedule_id);
      tr.setAttribute("aria-expanded", "false");
      return;
    }
    opened.add(schedule.schedule_id);
    tr.setAttribute("aria-expanded", "true");
    tr.after(await detailRow(schedule, 10));
  }

  mount(root, h("div", { class: "content" },
    h("div", { class: "page-head" },
      h("h1", null, "Schedules"),
      h("span", { class: "eyebrow" }, "recurring claims"),
      h("span", { class: "spacer" }),
      canWrite
        ? h("button", { type: "button", class: "primary",
                        onclick: () => scheduleForm({
                          projects: projects.projects, periods: periods.periods,
                          onSaved: reload }) }, "New schedule")
        : null),
    notice,
    // Renewals first and loudest: it is the only thing here with a
    // deadline, and the reason the screen exists.
    chasing.length
      ? h("div", { class: "renewals" },
          h("h2", null, "Renewals"),
          chasing.map((r) => h("div",
            { class: `renewal is-${r.renewal_state}` },
            h("span", { class: "renewal-state" }, STATE_LABEL[r.renewal_state]),
            h("span", { class: "renewal-what" },
              `${r.project_name} \u00b7 ${r.description}`),
            h("span", { class: "mono" }, r.renewal_date || "\u2013"),
            h("span", { class: "muted" },
              r.days_until < 0
                ? `${Math.abs(r.days_until)} days ago`
                : `in ${r.days_until} days`),
            r.renewal_note ? h("span", { class: "muted" }, r.renewal_note) : null)))
      : null,
    rows.length
      ? h("div", { class: "table-wrap" },
          h("table", null,
            h("thead", null, h("tr", null,
              h("th", null, "Project"),
              h("th", null, "Description"),
              h("th", null, "Every"),
              h("th", null, "From"),
              h("th", null, "To"),
              h("th", { class: "num" }, "Each"),
              h("th", { class: "num" }, "Covered"),
              h("th", { class: "num" }, "Value"),
              h("th", null, "Renewal"),
              h("th", null, ""))),
            h("tbody", null, rows.map((r) => h("tr",
              { class: [r.is_active ? null : "muted", "clickable"]
                  .filter(Boolean).join(" "),
                tabindex: "0", role: "button",
                "aria-expanded": String(opened.has(r.schedule_id)),
                onclick: (e) => toggle(e, r),
                onkeydown: (e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    toggle(e, r);
                  }
                } },
              h("td", { class: "text-wide", title: r.project_name }, r.project_name),
              h("td", { class: "text", title: r.description }, r.description),
              h("td", { class: "mono" }, r.frequency),
              h("td", { class: "mono" }, r.start_label),
              h("td", { class: "mono" }, r.end_label),
              h("td", { class: "num" }, fmt.money(r.amount_cents)),
              // A fraction, not a count: complete or not is the question.
              h("td", { class: r.generated_count >= r.expected_count
                               ? "num" : "num flagged-cell" },
                `${r.generated_count} / ${r.expected_count}`),
              h("td", { class: "num" }, fmt.moneyDash(r.generated_cents)),
              h("td", { class: "mono" }, r.renewal_date || "\u2013"),
              h("td", { onclick: (e) => e.stopPropagation() },
                canWrite && r.is_active
                ? h("span", { class: "row-actions" },
                    h("button", { type: "button",
                                  title: "Attach claims that already exist",
                                  onclick: () => act(r, "adopt") }, "Adopt"),
                    h("button", { type: "button",
                                  title: "Create the claims that do not exist yet",
                                  onclick: () => act(r, "generate") }, "Generate"),
                    h("button", { type: "button",
                                  onclick: () => scheduleForm({
                                    schedule: r, projects: projects.projects,
                                    periods: periods.periods, onSaved: reload }) },
                      "Edit"))
                : null)))))) 
      : stateMessage("No schedules yet",
          "Maintenance and SLA work belongs here: one agreement, not twelve "
          + "rows a year.", false)));
}
