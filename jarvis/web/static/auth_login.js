/* Login page wiring (external file: the CSP has no 'unsafe-inline'). */

(async function () {
  "use strict";

  if (!window.jarvisPasskeys || !jarvisPasskeys.supported()) return;
  var btn = document.getElementById("passkey-signin");
  var err = document.getElementById("passkey-error");
  btn.hidden = false;
  var conditionalAbort = null;

  btn.addEventListener("click", async function () {
    if (conditionalAbort) conditionalAbort.abort();
    err.hidden = true;
    try {
      await jarvisPasskeys.login();
    } catch (e) {
      if (e.name === "AbortError" || e.name === "NotAllowedError") return;
      err.textContent = e.message || "Passkey sign-in failed.";
      err.hidden = false;
    }
  });

  // Conditional UI: offer passkeys via autofill on the email field so
  // the passkey is the path of least resistance. Silently ignore
  // dismissal/abort — the emailed-code form still works.
  if (await jarvisPasskeys.conditionalLoginAvailable()) {
    conditionalAbort = new AbortController();
    jarvisPasskeys
      .login({ conditional: true, signal: conditionalAbort.signal })
      .catch(function () {});
  }
})();
