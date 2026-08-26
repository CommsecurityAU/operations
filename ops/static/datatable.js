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

import { fmt, h, mount } from "./app.js";

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

    // Filter values sort by the column's sortKey where it has one, so
    // months read Jul-26, Aug-26, Sep-26 rather than Apr, Aug, Dec. A list
    // of months in alphabetical order is a list nobody can scan.
    const order = new Map();
    for (const row of model.rows) {
      const value = row[key];
      if (value === null || value === undefined || value === "") continue;
      const rank = col && col.sortKey ? row[col.sortKey] : value;
      const seen = order.get(String(value));
      if (seen === undefined || rank < seen) order.set(String(value), rank);
    }
    const values = [...order.keys()].sort((a, b) => {
      const x = order.get(a);
      const y = order.get(b);
      return x < y ? -1 : x > y ? 1 : 0;
    });

    const chosen = new Set((model.filterDefaults || {})[key] || []);
    state.filters[key] = chosen;

    const caption = document.createTextNode(`All ${label.toLowerCase()}`);
    const button = h("button", {
      type: "button", class: "filter-btn",
      "aria-haspopup": "true", "aria-expanded": "false",
      onclick: (e) => { e.stopPropagation(); toggle(); },
    }, caption, h("span", { class: "caret" }, "\u25BE"));

    const boxes = values.map((v) => {
      const input = h("input", {
        // Named explicitly, not only by the wrapping <label>: a screen
        // reader announcing the checkbox on its own should still say what
        // it filters.
        type: "checkbox", value: String(v), "aria-label": String(v),
        checked: chosen.has(String(v)),
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

  // Export what is ON SCREEN: the filtered set, every page of it. Exporting
  // the whole table regardless of filters would hand someone a file that
  // does not match the figures they were just looking at.
  function toCsv(rows) {
    const cell = (v) => {
      const text = v === null || v === undefined ? "" : String(v);
      return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
    };
    const lines = [model.columns.map((c) => cell(c.label)).join(",")];
    for (const row of rows) {
      lines.push(model.columns.map((c) => {
        const raw = row[c.key];
        // Money goes out as a number, never a formatted string.
        return cell(c.key.endsWith("_cents") ? fmt.plain(raw) : raw);
      }).join(","));
    }
    return lines.join("\r\n");
  }

  const exportBtn = model.exportName
    ? h("button", { type: "button", title: "Download the rows currently shown" },
        "Export CSV")
    : null;
  if (exportBtn) {
    exportBtn.addEventListener("click", () => {
      const blob = new Blob(["\uFEFF" + toCsv(visible())],
                            { type: "text/csv;charset=utf-8" });
      const url = URL.createObjectURL(blob);
      const stamp = new Date().toISOString().slice(0, 10);
      const link = h("a", { href: url, download: `${model.exportName}-${stamp}.csv` });
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    });
  }

  const controls = h("div", { class: "controls" },
    search, filterEls,
    h("span", { class: "spacer" }), count, exportBtn, prev, next);

  function headerCell(col) {
    const active = state.sortKey === (col.sortKey || col.key);
    return h("th", {
      class: col.align === "right" ? "num" : null,
      "aria-sort": active ? (state.sortDir === 1 ? "ascending" : "descending") : "none",
      onclick: () => {
        // A column may sort on a different field from the one it shows:
        // `Sep-26` reads well and sorts alphabetically, which puts April
        // first. The month sorts on its start date instead.
        const key = col.sortKey || col.key;
        if (state.sortKey === key) state.sortDir = -state.sortDir;
        else { state.sortKey = key; state.sortDir = 1; }
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
      const attrs = { class: model.rowClass ? model.rowClass(row) : null };
      if (model.onRowClick) {
        attrs.class = [attrs.class, "clickable"].filter(Boolean).join(" ");
        attrs.tabindex = "0";
        attrs.role = "button";
        attrs.onclick = () => model.onRowClick(row);
        // Rows are reachable and operable from the keyboard, or the whole
        // edit path is mouse-only.
        attrs.onkeydown = (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            model.onRowClick(row);
          }
        };
      }
      tbody.appendChild(h("tr", attrs,
        model.columns.map((col) => {
          const raw = row[col.key];
          const text = col.fmt ? col.fmt(raw, row) : (raw ?? "");
          const cls = [col.align === "right" ? "num" : null, col.cls ? col.cls(raw, row) : null]
            .filter(Boolean).join(" ");
          // A truncated cell keeps its full value on hover, so narrowing a
          // column never loses information -- it only stops one long value
          // pushing every column after it off the screen.
          const title = cls.includes("text") ? String(raw ?? "") : null;
          return h("td", { class: cls || null, title },
                   text instanceof Node ? text : String(text));
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
