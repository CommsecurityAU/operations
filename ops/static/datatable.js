// datatable.js — one component seeding every read-only list view (§7).
//
// Contract:
//   model = { columns: [{key, label, align, fmt, cls}], rows,
//             filters?: [key], searchKeys?: [key], pageSize?, rowClass? }
//
// Controls are built ONCE and keep DOM identity across re-renders; only the
// rows are rebuilt. Rebuilding the whole thing would drop focus mid-keystroke
// and close an open select the moment you touched it — the sort of bug that
// makes a screen feel broken without anyone being able to say why.

import { h, mount } from "./app.js";

export function datatable(model) {
  const state = {
    sortKey: null, sortDir: 1, page: 0,
    search: "", filters: {},
    pageSize: model.pageSize || 100,
  };

  const tbody = h("tbody");
  const count = h("span", { class: "count" });
  const thead = h("thead");
  const prev = h("button", { type: "button", onclick: () => { state.page--; paint(); } }, "Previous");
  const next = h("button", { type: "button", onclick: () => { state.page++; paint(); } }, "Next");

  const search = model.searchKeys
    ? h("input", {
        type: "search", placeholder: "Search", "aria-label": "Search",
        oninput: (e) => { state.search = e.target.value.toLowerCase(); state.page = 0; paint(); },
      })
    : null;

  // Multi-select. A native <select multiple> needs ctrl-click to add a
  // second value, which nobody discovers, and shows no summary when closed.
  // A button plus a checkbox panel says what is selected without opening.
  function multiselect(key) {
    const col = model.columns.find((c) => c.key === key);
    const label = col ? col.label : key;
    const values = [...new Set(model.rows.map((r) => r[key])
      .filter((v) => v !== null && v !== ""))].sort();
    const chosen = new Set();
    state.filters[key] = chosen;

    const caption = document.createTextNode(`All ${label.toLowerCase()}`);
    const button = h("button", {
      type: "button", class: "filter-btn",
      "aria-haspopup": "true", "aria-expanded": "false",
      onclick: (e) => { e.stopPropagation(); toggle(); },
    }, caption, h("span", { class: "caret" }, "\u25BE"));

    const boxes = values.map((v) => {
      const input = h("input", {
        type: "checkbox", value: String(v),
        onchange: (e) => {
          if (e.target.checked) chosen.add(String(v)); else chosen.delete(String(v));
          state.page = 0;
          describe();
          paint();
        },
      });
      return h("label", { class: "filter-opt" }, input, h("span", null, String(v)));
    });

    const clear = h("button", {
      type: "button", class: "filter-clear",
      onclick: () => {
        chosen.clear();
        for (const b of boxes) b.firstChild.checked = false;
        state.page = 0; describe(); paint();
      },
    }, "Clear");

    const panel = h("div", { class: "filter-panel", role: "group",
                             "aria-label": label, hidden: true },
      boxes, clear);

    function describe() {
      // Name the values while they fit; a count once they do not. "3
      // selected" with no names is a filter you have to open to understand.
      const picked = [...chosen];
      const text = picked.length === 0 ? `All ${label.toLowerCase()}`
        : picked.length <= 2 ? `${label}: ${picked.join(", ")}`
        : `${label}: ${picked.length} of ${values.length}`;
      caption.nodeValue = text;
      button.classList.toggle("is-active", picked.length > 0);
    }

    function toggle(force) {
      const open = force === undefined ? panel.hidden : force;
      panel.hidden = !open;
      button.setAttribute("aria-expanded", String(open));
    }

    const wrap = h("div", { class: "filter" }, button, panel);
    wrap.addEventListener("click", (e) => e.stopPropagation());
    document.addEventListener("click", () => toggle(false));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") toggle(false);
    });
    describe();
    return wrap;
  }

  const filterEls = (model.filters || []).map(multiselect);

  const controls = h("div", { class: "controls" },
    search, filterEls,
    h("span", { class: "spacer" }), count, prev, next);

  function headerCell(col) {
    const active = state.sortKey === col.key;
    return h("th", {
      class: col.align === "right" ? "num" : null,
      "aria-sort": active ? (state.sortDir === 1 ? "ascending" : "descending") : "none",
      onclick: () => {
        if (state.sortKey === col.key) state.sortDir = -state.sortDir;
        else { state.sortKey = col.key; state.sortDir = 1; }
        state.page = 0;
        paint();
      },
    }, col.label, active ? h("span", { class: "sort" }, state.sortDir === 1 ? "\u25B2" : "\u25BC") : null);
  }

  function visible() {
    let rows = model.rows;
    for (const [key, chosen] of Object.entries(state.filters)) {
      if (chosen && chosen.size) {
        rows = rows.filter((r) => chosen.has(String(r[key])));
      }
    }
    if (state.search && model.searchKeys) {
      rows = rows.filter((r) => model.searchKeys.some((k) =>
        String(r[k] ?? "").toLowerCase().includes(state.search)));
    }
    if (state.sortKey) {
      const k = state.sortKey;
      rows = [...rows].sort((a, b) => {
        const x = a[k], y = b[k];
        if (x === y) return 0;
        if (x === null || x === undefined) return 1;   // blanks last, always
        if (y === null || y === undefined) return -1;
        const cmp = typeof x === "number" && typeof y === "number"
          ? x - y : String(x).localeCompare(String(y));
        return cmp * state.sortDir;
      });
    }
    return rows;
  }

  function paint() {
    const rows = visible();
    const pages = Math.max(1, Math.ceil(rows.length / state.pageSize));
    state.page = Math.min(Math.max(0, state.page), pages - 1);
    const start = state.page * state.pageSize;
    const slice = rows.slice(start, start + state.pageSize);

    mount(thead, h("tr", null, model.columns.map(headerCell)));

    while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
    for (const row of slice) {
      tbody.appendChild(h("tr", { class: model.rowClass ? model.rowClass(row) : null },
        model.columns.map((col) => {
          const raw = row[col.key];
          const text = col.fmt ? col.fmt(raw, row) : (raw ?? "");
          const cls = [col.align === "right" ? "num" : null, col.cls ? col.cls(raw, row) : null]
            .filter(Boolean).join(" ");
          return h("td", { class: cls || null }, text instanceof Node ? text : String(text));
        })));
    }

    mount(count, document.createTextNode(
      rows.length === model.rows.length
        ? `${rows.length} rows`
        : `${rows.length} of ${model.rows.length} rows`));
    prev.disabled = state.page === 0;
    next.disabled = state.page >= pages - 1;

    // The FILTERED set, not the page. Totals that changed when you paged
    // would be arithmetic about nothing.
    if (model.onVisible) model.onVisible(rows);
  }

  paint();
  return h("div", null, controls,
    h("div", { class: "table-wrap" }, h("table", null, thead, tbody)));
}
