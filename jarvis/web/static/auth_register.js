/* Passkey enrollment page wiring (external file: no 'unsafe-inline' CSP). */

(function () {
  "use strict";

  var form = document.getElementById("enroll-form");
  var err = document.getElementById("passkey-error");

  if (!window.jarvisPasskeys || !jarvisPasskeys.supported()) {
    form.hidden = true;
    document.getElementById("passkey-unsupported").hidden = false;
    return;
  }

  form.addEventListener("submit", async function (evt) {
    evt.preventDefault();
    err.hidden = true;
    var result;
    try {
      result = await jarvisPasskeys.register(
        document.getElementById("passkey-name").value.trim());
    } catch (e) {
      if (e.name === "AbortError" || e.name === "NotAllowedError") return;
      err.textContent = e.message || "Passkey setup failed. Try again.";
      err.hidden = false;
      return;
    }
    if (result.recovery_codes && result.recovery_codes.length) {
      showRecoveryCodes(result.recovery_codes);
    } else {
      window.location.assign("/");
    }
  });

  function showRecoveryCodes(codes) {
    var card = document.getElementById("enroll-card");
    card.replaceChildren(
      document.getElementById("recovery-template").content.cloneNode(true));
    document.getElementById("recovery-codes").textContent = codes.join("\n");
    document.getElementById("recovery-copy").addEventListener("click", function () {
      navigator.clipboard.writeText(codes.join("\n"));
      this.textContent = "Copied";
    });
  }
})();
