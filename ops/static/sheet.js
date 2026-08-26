// sheet.js — a modal form, and the field inside it.
//
// Shared because three screens grew their own version and they drifted:
// the register's dialogs carried a note and an issue date while the
// invoicing grid used two `window.prompt` calls that silently dropped
// both. Where an action is taken from should change what it does, not what
// can be recorded while doing it.
//
// Native <dialog>: Escape closes it, focus is trapped, and the backdrop
// comes free. Rebuilding that by hand is where accessibility quietly goes.

import { h } from "./app.js";

export function field(label, control, hint) {
  const error = h("span", { class: "field-error" });
  const wrap = h("label", { class: "field" },
    h("span", { class: "field-label" }, label),
    control,
    hint ? h("span", { class: "field-hint" }, hint) : null,
    error);
  return { wrap, control, setError: (m) => {
    error.textContent = m || "";
    wrap.classList.toggle("has-error", !!m);
  } };
}

export function sheet(title, subtitle, body, confirmLabel, onConfirm) {
  const summary = h("div", { class: "form-error", hidden: true });
  const go = h("button", { type: "button", class: "primary" }, confirmLabel);
  const cancel = h("button", { type: "button" }, "Cancel");
  const dialog = h("dialog", { class: "sheet", "aria-label": title },
    h("div", { class: "sheet-head" },
      h("h2", null, title),
      subtitle ? h("span", { class: "mono muted" }, subtitle) : null),
    h("div", { class: "form-grid" }, body.fields),
    summary,
    h("div", { class: "sheet-foot" },
      h("span", { class: "spacer" }), cancel, go));

  go.addEventListener("click", async () => {
    summary.hidden = true;
    for (const f of body.controls) f.setError("");
    go.disabled = true;
    try {
      await onConfirm();
      dialog.close();
    } catch (err) {
      const detail = err.detail;
      if (detail && typeof detail === "object") {
        const rest = [];
        for (const [key, message] of Object.entries(detail)) {
          const target = body.byKey[key];
          if (target) target.setError(message);
          else rest.push(`${key}: ${message}`);
        }
        if (rest.length) {
          summary.textContent = rest.join("; ");
          summary.hidden = false;
        }
      } else {
        summary.textContent = err.message;
        summary.hidden = false;
      }
    } finally {
      go.disabled = false;
    }
  });
  cancel.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => dialog.remove());
  document.body.appendChild(dialog);
  dialog.showModal();
  return dialog;
}

