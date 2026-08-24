// app.js — the three primitives. Components use nothing else for DOM or
// network (CS-OP-ARCH-002 §7).

// h(tag, attrs, ...children) — element builder.
//
// createElement / textContent / setAttribute only. `innerHTML` appears
// NOWHERE in static/, including in here: there is no blessed exception, so
// the guardrail is a flat grep and cannot be argued with. Text goes in as
// text, so a supplier called `<script>` renders as those nine characters.
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "dataset") for (const [dk, dv] of Object.entries(v)) el.dataset[dk] = dv;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

// api(method, path, body?) — the ONLY fetch in the codebase.
//
// Throws on non-2xx with the server's own message, so callers never inspect
// status codes and no screen invents its own error wording.
export async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let payload = null;
  if (text) { try { payload = JSON.parse(text); } catch { payload = null; } }
  if (!res.ok) {
    const err = new Error((payload && payload.error) || `${res.status} ${res.statusText}`);
    err.status = res.status;
    err.detail = payload && payload.detail;
    throw err;
  }
  return payload;
}

// fmt — money arrives as integer cents and is formatted here, once.
// Nothing divides by 100 anywhere else.
const AUD = new Intl.NumberFormat("en-AU", {
  style: "currency", currency: "AUD", minimumFractionDigits: 2,
});

export const fmt = {
  money(cents) {
    if (cents === null || cents === undefined) return "";
    return AUD.format(cents / 100);
  },
  date(iso) {
    if (!iso) return "";
    const [y, m, d] = iso.slice(0, 10).split("-");
    return `${d}/${m}/${y}`;
  },
  num(n, dp = 0) {
    if (n === null || n === undefined) return "";
    return Number(n).toLocaleString("en-AU", {
      minimumFractionDigits: dp, maximumFractionDigits: dp,
    });
  },
};

// mount(el, node) — replace a container's contents without innerHTML.
export function mount(el, node) {
  while (el.firstChild) el.removeChild(el.firstChild);
  if (node) el.appendChild(node);
}

// Empty and error screens are directions, not moods: say what happened and
// what to do about it.
export function stateMessage(title, detail, isError) {
  return h("div", { class: isError ? "state is-error" : "state" },
    h("h2", null, title),
    detail ? h("p", null, detail) : null);
}
