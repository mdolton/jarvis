/* Passkey management page wiring (external file: no 'unsafe-inline' CSP).
   Forms carrying data-confirm get a native confirm() gate — the inline
   onsubmit this replaces would violate the CSP. */

(function () {
  "use strict";

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (evt) {
      if (!window.confirm(form.dataset.confirm)) evt.preventDefault();
    });
  });

  var btn = document.getElementById("passkey-add");
  if (!btn) return;
  var err = document.getElementById("passkey-error");
  if (!window.jarvisPasskeys || !jarvisPasskeys.supported()) {
    btn.disabled = true;
    btn.textContent = "Passkeys unsupported in this browser";
    return;
  }
  btn.addEventListener("click", async function () {
    err.hidden = true;
    try {
      var name = prompt("Name this passkey (e.g. Pixel fingerprint)") || "";
      await jarvisPasskeys.register(name.trim());
      window.location.reload();
    } catch (e) {
      if (e.name === "AbortError" || e.name === "NotAllowedError") return;
      err.textContent = e.message || "Passkey setup failed.";
      err.hidden = false;
    }
  });
})();
