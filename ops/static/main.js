// main.js — shell wiring. One screen so far; the router is a switch, and
// stays a switch until there is a reason for it not to be.

import { api, h, mount } from "./app.js";
import * as projects from "./projects.js";
import * as claims from "./claims.js";
import * as worklist from "./worklist.js";

const SCREENS = { projects, claims, worklist };

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

  const name = (location.hash.replace("#", "") || "projects");
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

  const screen = SCREENS[name] || SCREENS.projects;
  await screen.render(view);
  mount(status, document.createTextNode(
    `ops \u00b7 ${new Date().toLocaleString("en-AU")}`));
}

window.addEventListener("hashchange", boot);
boot();
