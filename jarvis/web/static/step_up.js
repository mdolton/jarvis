/* Step-up re-authentication UI.

   Two entry points share the passkey ceremony (jarvisPasskeys.stepUp):

   1. htmx partial swaps — a gated route answers 401 with an HX-Trigger of
      "jarvis-step-up-required". htmx performs no swap on a 401, so the form
      that triggered the request is still in the DOM with its values. We
      catch htmx:responseError, open the modal, run the assertion, and replay
      the original request from the same element — nothing typed is lost.

   2. full-page form posts — the server renders step_up.html (401) with the
      submitted fields echoed into a hidden replay form; the #step-up-page
      wiring below submits it (or navigates, for GET) after the assertion. */

(function () {
  "use strict";

  const page = document.getElementById("step-up-page");
  if (page) {
    const btn = document.getElementById("step-up-page-confirm");
    const errorEl = document.getElementById("step-up-page-error");
    btn.addEventListener("click", async function () {
      btn.disabled = true;
      errorEl.textContent = "";
      try {
        await window.jarvisPasskeys.stepUp();
        const form = document.getElementById("step-up-replay");
        if (form) form.submit();
        else if (page.dataset.method === "GET") window.location.assign(page.dataset.action);
        else window.history.back();
      } catch (err) {
        errorEl.textContent = err.message || "Passkey confirmation failed.";
        btn.disabled = false;
      }
    });
  }

  const dialog = document.getElementById("step-up-modal");
  if (!dialog) return;
  const confirmBtn = document.getElementById("step-up-confirm");
  const cancelBtn = document.getElementById("step-up-cancel");
  const errorEl = document.getElementById("step-up-error");
  let pending = null; // the failed htmx request, replayed after the assertion

  document.body.addEventListener("htmx:responseError", function (evt) {
    const xhr = evt.detail.xhr;
    if (!xhr || xhr.status !== 401) return;
    const trigger = xhr.getResponseHeader("HX-Trigger") || "";
    if (trigger.indexOf("jarvis-step-up-required") === -1) return;
    pending = evt.detail;
    errorEl.textContent = "";
    confirmBtn.disabled = false;
    dialog.showModal();
  });

  confirmBtn.addEventListener("click", async function () {
    confirmBtn.disabled = true;
    errorEl.textContent = "";
    try {
      await window.jarvisPasskeys.stepUp();
      dialog.close();
      if (pending) {
        const cfg = pending.requestConfig;
        // source: the original element, so htmx re-collects its (still
        // intact) form values and honors its hx-target/hx-swap.
        htmx.ajax(cfg.verb, cfg.path, { source: pending.elt });
        pending = null;
      }
    } catch (err) {
      errorEl.textContent = err.message || "Passkey confirmation failed.";
      confirmBtn.disabled = false;
    }
  });

  cancelBtn.addEventListener("click", function () {
    pending = null;
    dialog.close();
  });
})();
