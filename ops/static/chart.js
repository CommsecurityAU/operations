// chart.js — the few chart shapes this platform needs, drawn as SVG.
//
// Not a charting library. Four functions, no dependencies, and the same
// design tokens as everything else — a library would be ninety kilobytes
// to draw rectangles, and would bring its own type scale and palette to
// argue with ours.
//
// Every value is CENTS. Formatting happens once, at the edge.

import { fmt, h } from "./app.js";

const NS = "http://www.w3.org/2000/svg";

export function svgEl(name, attrs, ...kids) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.appendChild(typeof kid === "string"
      ? document.createTextNode(kid) : kid);
  }
  return node;
}

// A money axis that lands on round numbers. `$1,234,567` as a tick label
// tells you nothing; `$1.2m` tells you the scale, which is all an axis is
// for.
function niceMax(value) {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10]) {
    if (value <= step * magnitude) return step * magnitude;
  }
  return 10 * magnitude;
}

function scale(values) {
  const top = niceMax(Math.max(0, ...values));
  const bottom = Math.min(0, ...values);
  return { top, bottom: bottom < 0 ? -niceMax(-bottom) : 0 };
}

// Top right, over the plot: a legend under a chart makes the eye leave the
// picture, find the key, and come back. The LABEL carries the series
// colour as well as the swatch — two things saying the same thing, so it
// still reads if one of them is missed.
//
// A CLASS, not an inline style. The Content-Security-Policy is
// `default-src 'self'`, which blocks inline `style` attributes — so the
// first version of this set colours that the browser silently dropped, and
// the legend rendered grey while the bars beside it were correct. The bars
// worked because SVG `fill` is an attribute rather than CSS.
function legend(items) {
  return h("div", { class: "legend" }, items.map((s) =>
    h("span", { class: `legend-item ${s.cls}` },
      h("span", { class: "swatch" }),
      s.label)));
}

/**
 * Columns with optional lines over them.
 *
 * `series` entries are `{ key, label, kind: "bar"|"line", color, stack }`.
 * Bars sharing a `stack` sit on top of one another; a line is drawn
 * through the value at each column.
 */
export function combo(rows, series, options) {
  const opts = options || {};
  // Wider viewBox for the same rendered width: everything drawn inside it
  // scales down together, which shrinks the type without touching each
  // size. The chart was chunky rather than mis-proportioned.
  const W = 960, H = 300, padL = 62, padR = 10, padT = 14, padB = 30;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const step = plotW / Math.max(1, rows.length);
  const barW = Math.min(38, step * 0.5);

  const stacks = new Map();
  for (const s of series.filter((x) => x.kind === "bar")) {
    const key = s.stack || s.key;
    stacks.set(key, (stacks.get(key) || []).concat(s));
  }
  const totals = rows.map((_r, i) => {
    let best = 0;
    for (const group of stacks.values()) {
      best = Math.max(best, group.reduce((t, s) => t + (rows[i][s.key] || 0), 0));
    }
    for (const s of series.filter((x) => x.kind === "line")) {
      best = Math.max(best, rows[i][s.key] || 0);
    }
    return best;
  });
  const lows = rows.map((_r, i) => Math.min(
    0, ...series.map((s) => rows[i][s.key] || 0)));
  const { top, bottom } = scale([...totals, ...lows]);
  const span = top - bottom || 1;
  const y = (v) => padT + plotH - ((v - bottom) / span) * plotH;

  const ticks = [];
  for (let i = 0; i <= 4; i += 1) {
    const value = bottom + (span / 4) * i;
    ticks.push(svgEl("g", null,
      svgEl("line", { x1: padL, x2: W - padR, y1: y(value), y2: y(value),
                      class: value === 0 ? "axis" : "grid" }),
      svgEl("text", { x: padL - 6, y: y(value) + 3, class: "tick end" },
        fmt.shortMoney(value))));
  }

  const bars = [];
  rows.forEach((row, i) => {
    const centre = padL + step * i + step / 2;
    let n = 0;
    for (const [, group] of stacks) {
      let base = 0;
      const x = centre - barW / 2 + n * 0;
      for (const s of group) {
        const value = row[s.key] || 0;
        if (!value) continue;
        const from = y(base + Math.max(0, value));
        const to = y(base);
        base += value;
        bars.push(svgEl("rect", {
          x, y: Math.min(from, to), width: barW,
          height: Math.max(1, Math.abs(to - from)),
          class: `${s.cls}${row.projected ? " projected" : ""}`,
        }, svgEl("title", null,
                 `${row.label} \u00b7 ${s.label} ${fmt.money(value)}`)));
      }
      n += 1;
    }
  });

  const lines = series.filter((s) => s.kind === "line").map((s) => {
    const points = rows.map((row, i) =>
      `${padL + step * i + step / 2},${y(row[s.key] || 0)}`).join(" ");
    return svgEl("g", null,
      svgEl("polyline", { points, fill: "none", "stroke-width": "2",
                          class: `series-line ${s.cls}` }),
      rows.map((row, i) => svgEl("circle", {
        cx: padL + step * i + step / 2, cy: y(row[s.key] || 0), r: 3,
        class: s.cls,
      }, svgEl("title", null,
               `${row.label} \u00b7 ${s.label} ${fmt.money(row[s.key] || 0)}`))));
  });

  const labels = rows.map((row, i) => svgEl("text", {
    x: padL + step * i + step / 2, y: H - 8, class: "tick mid",
  }, row.label));

  return h("figure", { class: "chart wide" },
    h("div", { class: "chart-head" },
      opts.title ? h("figcaption", null, opts.title) : h("span", null, ""),
      legend(series)),
    svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "plot",
                   role: "img", "aria-label": opts.title || "chart" },
      ticks, bars, lines, labels));
}
