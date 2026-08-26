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
  // For export, not display: a spreadsheet needs a number, and "$1,234.56"
  // arrives in Excel as text that will not sum.
  plain(cents) {
    if (cents === null || cents === undefined) return "";
    const neg = cents < 0;
    const whole = Math.trunc(Math.abs(cents) / 100);
    const part = Math.abs(cents) % 100;
    return `${neg ? "-" : ""}${whole}.${String(part).padStart(2, "0")}`;
  },
  money(cents) {
    if (cents === null || cents === undefined) return "";
    return AUD.format(cents / 100);
  },
  // Accounting convention, and the source workbook's own: a zero is a dash.
  // A column of "$0.00" is ink carrying no information, and it competes with
  // the figures that do.
  moneyDash(cents) {
    if (cents === null || cents === undefined) return "";
    return cents === 0 ? "\u2013" : AUD.format(cents / 100);
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

// toCents / moneyInput — the ONE money conversion in the browser, mirroring
// ops/money.py being the one on the server.
//
// Nothing else may multiply or divide by 100. Two places that convert money
// is two places to disagree, and they always eventually do.
const MONEY_SHAPE = /^-?\d*\.?\d{0,2}$/;

export function toCents(text) {
  const clean = String(text ?? "").replace(/[$,\s]/g, "");
  if (clean === "") return 0;
  if (!MONEY_SHAPE.test(clean)) return null;      // null means "not an amount"
  const neg = clean.startsWith("-");
  const [whole, frac = ""] = clean.replace("-", "").split(".");
  // Through strings, not arithmetic: 19.99 * 100 is 1998.9999999999998.
  const cents = Number(whole || 0) * 100 + Number((frac + "00").slice(0, 2));
  return neg ? -cents : cents;
}

// A text input that SHOWS dollars the way the rest of the app does, and
// accepts them however they are typed. Formatted when it does not have
// focus, plain while being edited -- commas appearing mid-keystroke is the
// reason people distrust money fields.
export function moneyInput(cents, attrs) {
  const input = h("input", {
    type: "text", inputmode: "decimal", ...(attrs || {}),
  });
  const show = (c) => { input.value = c === null || c === undefined ? "" : fmt.money(c); };
  show(cents);
  input.addEventListener("focus", () => {
    const c = toCents(input.value);
    if (c !== null) input.value = (c / 100).toFixed(2);
    input.select();
  });
  input.addEventListener("blur", () => {
    const c = toCents(input.value);
    if (c !== null) show(c);          // leave it alone if it is not an amount,
  });                                 // so the error message can point at it
  return input;
}

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
