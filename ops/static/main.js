// main.js — shell wiring. One screen so far; the router is a switch, and
// stays a switch until there is a reason for it not to be.

import { api, h, mount } from "./app.js";

// Screens load WHEN OPENED, not on every page. Statically importing all of
// them meant the page weight was the sum of every module -- the projects
// register paid for the invoicing grid it never showed. A dynamic import
// is one line and the browser caches it after the first visit.
const SCREENS = {
  projects: () => import("./projects.js"),
  claims: () => import("./claims.js"),
  schedules: () => import("./schedules.js"),
  worklist: () => import("./worklist.js"),
  procurement: () => import("./procurement.js"),
  dashboard: () => import("./dashboard.js"),
  expenses: () => import("./expenses.js"),
  access: () => import("./access.js"),
};

async function boot() {
  const identity = document.getElementById("identity");
  const view = document.getElementById("view");
  const status = document.getElementById("status");

  let me;
  try {
    me = await api("GET", "/api/me");
  } catch (err) {
    // Do NOT bounce straight to /login.
    //
    // An automatic redirect makes the app unusable the moment sign-in is
    // broken: the page leaves before anyone can read why, and it leaves so
    // fast there is no way to reach this origin's console. A screen with a
    // button costs one click and keeps the failure visible and local.
    mount(view, h("div", { class: "content" },
      h("div", { class: "state" },
        h("h2", null, err.status === 401 ? "Not signed in" : "Cannot sign in"),
        h("p", null, err.status === 401
          ? "Your session has ended, or you have not signed in yet."
          : err.message),
        h("p", { class: "state-action" },
          h("a", { class: "signin", href: "/login" }, "Sign in with Google")))));
    mount(status, document.createTextNode("not signed in"));
    return;
  }
  mount(identity, h("span", null,
    me.display_name,
    h("span", { class: "roles" },
      me.roles.length ? ` ${me.roles.map((r) => r.role).join(" ")}` : " no access")));

  // A tab for a screen this person cannot open is a tab that goes to a
  // refusal. The server is what actually refuses -- hiding a link is not a
  // control and is not meant to be -- but a navigation that offers doors
  // nobody can walk through is a navigation nobody trusts.
  //
  // Only screens with a role BEYOND the baseline are listed: everything
  // else needs `viewer`, which anyone who got this far already has.
  const NEEDS = {
    dashboard: "finance",
    expenses: "finance",
    access: "admin",
  };
  const held = new Set(me.roles.map((r) => r.role));
  for (const [screen, role] of Object.entries(NEEDS)) {
    if (held.has(role)) continue;
    const link = document.querySelector(`.nav a[href="#${screen}"]`);
    if (link) link.hidden = true;
  }

  let name = (location.hash.replace("#", "") || "projects");
  // Typing the URL of a hidden screen still reaches it, and still gets the
  // server's refusal with its explanation. What it must not do is leave
  // the shell on a tab that is not there.
  if (NEEDS[name] && !held.has(NEEDS[name])) name = "projects";
  for (const link of document.querySelectorAll(".nav a")) {
    if (link.getAttribute("href") === `#${name}`) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }

  // The open count belongs in the navigation, not only on the worklist
  // screen: an unresolved code is a problem you should see without going
  // looking for it.
  try {
    const { open } = await api("GET", "/api/worklist");
    const badge = document.getElementById("worklist-count");
    if (badge) {
      badge.textContent = open ? String(open) : "";
      badge.hidden = !open;
    }
  } catch { /* a viewer with no grants; the nav simply shows no count */ }

  const load = SCREENS[name] || SCREENS.projects;
  const screen = await load();
  await screen.render(view);
  mount(status, document.createTextNode(
    `ops \u00b7 ${new Date().toLocaleString("en-AU")}`));
}

window.addEventListener("hashchange", boot);
boot();
