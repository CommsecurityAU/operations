// main.js — shell wiring. One screen so far; the router is a switch, and
// stays a switch until there is a reason for it not to be.

import { api, h, mount } from "./app.js";
import * as projects from "./projects.js";
import * as worklist from "./worklist.js";

const SCREENS = { projects, worklist };

async function boot() {
  const identity = document.getElementById("identity");
  const view = document.getElementById("view");
  const status = document.getElementById("status");

  try {
    const me = await api("GET", "/api/me");
    mount(identity, h("span", null,
      me.display_name,
      h("span", { class: "roles" },
        me.roles.length ? ` ${me.roles.map((r) => r.role).join(" ")}` : " no access")));
  } catch {
    // Not signed in: the server will redirect the browser at /login.
    window.location.href = "/login";
    return;
  }

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
