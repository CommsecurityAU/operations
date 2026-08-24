// main.js — shell wiring. One screen so far; the router is a switch, and
// stays a switch until there is a reason for it not to be.

import { api, h, mount } from "./app.js";
import * as projects from "./projects.js";

const SCREENS = { projects };

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
  const screen = SCREENS[name] || SCREENS.projects;
  await screen.render(view);
  mount(status, document.createTextNode(
    `ops \u00b7 ${new Date().toLocaleString("en-AU")}`));
}

window.addEventListener("hashchange", boot);
boot();
