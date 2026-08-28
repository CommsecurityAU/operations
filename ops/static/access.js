// access.js — who can do what.
//
// Roles are enumerated and NO ROLE IMPLIES ANOTHER: an admin who is not
// also a viewer cannot read the register. That has caught us twice, so the
// grid shows all four roles per entity as checkboxes rather than a single
// "level" that would imply a hierarchy the system does not have.
//
// Until this screen existed, granting a role meant running Python against
// the database. That is fine for one person and impossible for anyone else.

import { api, h, mount, stateMessage } from "./app.js";

let pending = null;

function when(ts) {
  if (!ts) return "\u2013";
  return new Date(ts * 1000).toISOString().slice(0, 10);
}

export async function render(root) {
  mount(root, stateMessage("Loading users", null, false));
  let data;
  try {
    data = await api("GET", "/api/users");
  } catch (err) {
    mount(root, err.status === 403
      ? stateMessage("Admin only",
          "Managing access needs the admin role on an entity.", false)
      : stateMessage("Could not load users", err.message, true));
    return;
  }

  const notice = h("div", { class: "notice", hidden: true });
  if (pending) {
    notice.textContent = pending;
    notice.hidden = false;
    pending = null;
  }
  const report = (message) => { pending = message; render(root); };

  async function toggle(user, entity, role, on) {
    try {
      await api(on ? "POST" : "DELETE", `/api/users/${user.id}/roles`,
                { entity_id: entity.id, role });
      report(`${user.display_name}: ${on ? "granted" : "revoked"} ${role}`);
    } catch (err) {
      report(err.message);
    }
  }

  async function setActive(user, active) {
    if (!active && !window.confirm(
        `Switch off ${user.display_name}? Every session they hold stops `
        + "working immediately.")) return;
    try {
      await api("PATCH", `/api/users/${user.id}`, { is_active: active });
      report(`${user.display_name}: ${active ? "reactivated" : "switched off"}`);
    } catch (err) {
      report(err.message);
    }
  }

  const held = (user, entityId, role) =>
    user.roles.some((r) => r.entity_id === entityId && r.role === role);

  function roleCell(user, entity, role) {
    const editable = data.administers.includes(entity.id) && user.is_active;
    const on = held(user, entity.id, role);
    if (!editable) {
      return h("td", { class: on ? "num" : "num zero" },
        on ? "\u25cf" : "\u2013");
    }
    const box = h("input", {
      type: "checkbox", checked: on,
      "aria-label": `${role} for ${user.display_name} on ${entity.name}`,
    });
    box.addEventListener("change", () => toggle(user, entity, role, box.checked));
    return h("td", { class: "num" }, box);
  }

  const rows = [];
  for (const user of data.users) {
    for (const entity of data.entities) {
      rows.push(h("tr", { class: user.is_active ? null : "muted" },
        h("td", { class: "text-wide", title: user.email },
          user.display_name,
          user.id === data.me ? h("span", { class: "tag" }, "you") : null,
          user.is_active ? null : h("span", { class: "tag is-off" }, "off")),
        h("td", { class: "muted text", title: user.email }, user.email),
        data.entities.length > 1
          ? h("td", { class: "muted" }, entity.name) : null,
        ...data.roles.map((role) => roleCell(user, entity, role)),
        h("td", { class: "mono muted" }, when(user.last_seen_ts)),
        h("td", null,
          data.administers.length && user.id !== data.me
            ? h("button", { type: "button",
                            class: user.is_active ? "danger" : null,
                            onclick: () => setActive(user, !user.is_active) },
                user.is_active ? "Switch off" : "Reactivate")
            : null)));
    }
  }

  mount(root, h("div", { class: "content" },
    h("div", { class: "page-head" },
      h("h1", null, "Access"),
      h("span", { class: "eyebrow" }, "roles by entity")),
    notice,
    h("div", { class: "table-wrap" },
      h("table", null,
        h("thead", null, h("tr", null,
          h("th", null, "User"),
          h("th", null, "Email"),
          data.entities.length > 1 ? h("th", null, "Entity") : null,
          ...data.roles.map((r) => h("th", { class: "num" }, r)),
          h("th", null, "Last seen"),
          h("th", null, ""))),
        h("tbody", null, rows))),
    // Said once, here, because the alternative is someone concluding the
    // system is broken when an admin cannot see the register.
    h("p", { class: "muted note" },
      "No role implies another. An admin who is not also a viewer cannot "
      + "read the register \u2014 grant every role the person needs.")));
}
