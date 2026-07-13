/* Passkey (WebAuthn) ceremonies for the Jarvis dashboard. Vanilla JS, no
   dependencies. The server (py_webauthn options_to_json) hands out options
   with base64url-encoded buffers; everything binary is decoded here on the
   way in and re-encoded on the way out. */

(function () {
  "use strict";

  function b64uToBuf(s) {
    const pad = "=".repeat((4 - (s.length % 4)) % 4);
    const bin = atob(s.replace(/-/g, "+").replace(/_/g, "/") + pad);
    return Uint8Array.from(bin, (c) => c.charCodeAt(0)).buffer;
  }

  function bufToB64u(buf) {
    const bytes = new Uint8Array(buf);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  async function postJSON(url, body) {
    const headers = { "Content-Type": "application/json" };
    // Synchronizer token, minted into the page at render time (see
    // jarvis/web/csrf.py). Empty pre-login — the server only demands it on
    // requests that carry a session cookie.
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && meta.content) headers["X-CSRF-Token"] = meta.content;
    const resp = await fetch(url, {
      method: "POST",
      headers: headers,
      body: JSON.stringify(body || {}),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.error || "request failed");
    return data;
  }

  function decodeCreationOptions(options) {
    options.challenge = b64uToBuf(options.challenge);
    options.user.id = b64uToBuf(options.user.id);
    (options.excludeCredentials || []).forEach((c) => { c.id = b64uToBuf(c.id); });
    return options;
  }

  function decodeRequestOptions(options) {
    options.challenge = b64uToBuf(options.challenge);
    (options.allowCredentials || []).forEach((c) => { c.id = b64uToBuf(c.id); });
    return options;
  }

  function serializeCreation(cred) {
    const r = cred.response;
    return {
      id: cred.id,
      rawId: bufToB64u(cred.rawId),
      type: cred.type,
      authenticatorAttachment: cred.authenticatorAttachment || null,
      clientExtensionResults: cred.getClientExtensionResults(),
      response: {
        clientDataJSON: bufToB64u(r.clientDataJSON),
        attestationObject: bufToB64u(r.attestationObject),
        transports: r.getTransports ? r.getTransports() : [],
      },
    };
  }

  function serializeAssertion(cred) {
    const r = cred.response;
    return {
      id: cred.id,
      rawId: bufToB64u(cred.rawId),
      type: cred.type,
      authenticatorAttachment: cred.authenticatorAttachment || null,
      clientExtensionResults: cred.getClientExtensionResults(),
      response: {
        clientDataJSON: bufToB64u(r.clientDataJSON),
        authenticatorData: bufToB64u(r.authenticatorData),
        signature: bufToB64u(r.signature),
        userHandle: r.userHandle ? bufToB64u(r.userHandle) : null,
      },
    };
  }

  /* Register a new passkey inside the current session. Resolves with the
     server response ({verified, credential_id, recovery_codes}). */
  async function register(name) {
    const begin = await postJSON("/auth/passkey/register/begin");
    const cred = await navigator.credentials.create({
      publicKey: decodeCreationOptions(begin.options),
    });
    return postJSON("/auth/passkey/register/complete", {
      challenge_id: begin.challenge_id,
      credential: serializeCreation(cred),
      name: name || null,
    });
  }

  /* Sign in with a passkey. With {conditional: true} this arms the browser's
     passkey autofill (conditional UI) and resolves only if the user picks
     one; pass an AbortController signal so a modal request can cancel it. */
  async function login(opts) {
    opts = opts || {};
    const begin = await postJSON("/auth/passkey/login/begin");
    const request = { publicKey: decodeRequestOptions(begin.options) };
    if (opts.conditional) request.mediation = "conditional";
    if (opts.signal) request.signal = opts.signal;
    const cred = await navigator.credentials.get(request);
    const done = await postJSON("/auth/passkey/login/complete", {
      challenge_id: begin.challenge_id,
      credential: serializeAssertion(cred),
    });
    if (done.redirect) window.location.assign(done.redirect);
    return done;
  }

  /* Step-up: re-assert the CURRENT session's passkey for a sensitive route.
     Resolves with {verified: true}; the server stamps last_auth_at. */
  async function stepUp() {
    const begin = await postJSON("/auth/step-up/begin");
    const cred = await navigator.credentials.get({
      publicKey: decodeRequestOptions(begin.options),
    });
    return postJSON("/auth/step-up/complete", {
      challenge_id: begin.challenge_id,
      credential: serializeAssertion(cred),
    });
  }

  async function conditionalLoginAvailable() {
    return !!(
      window.PublicKeyCredential &&
      PublicKeyCredential.isConditionalMediationAvailable &&
      (await PublicKeyCredential.isConditionalMediationAvailable())
    );
  }

  window.jarvisPasskeys = {
    supported: function () { return !!window.PublicKeyCredential; },
    register: register,
    login: login,
    stepUp: stepUp,
    conditionalLoginAvailable: conditionalLoginAvailable,
  };
})();
