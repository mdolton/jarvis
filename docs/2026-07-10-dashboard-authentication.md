# Jarvis dashboard authentication — research findings + build plan

Research: 111 agents, 28 sources, 117 claims extracted, 25 adversarially verified (20 confirmed, 5 refuted).
Date: 2026-07-10.

---

## Part 1 — What the research changed about the design

### The load-bearing finding: email cannot be the authenticator

NIST SP 800-63B-4 §3.1.3.1 (final, 2025-07-31), verified 3-0:

> "Email SHALL NOT be used for out-of-band authentication because it may be vulnerable to: access
> using only a password; interception in transit or at intermediate mail servers; rerouting attacks..."

immediately followed by:

> "Confirmation codes that are sent to validate email addresses or are issued as recovery codes ...
> are not authentication processes and not affected by the above prohibition."

So the correct framing is **not** "magic link = login, passkey = optional extra". It is:

- **Passkey = the authenticator.** Mandatory, not optional.
- **Emailed code = account verification + enrollment + recovery.** Explicitly permitted by NIST in
  exactly that role.

This inverts the original ask, and the inversion is *load-bearing for Jarvis specifically*: this
dashboard drives an LLM agent with tool access and holds Fernet-encrypted Gmail / Calendar /
Fastmail OAuth tokens. An auth bypass is not "a stranger reads my dashboard" — it is **full mailbox
compromise**. And because email is the recovery path, **the system's real security floor is the
email account** — which is another reason not to also make it the front door.

OWASP's MFA Cheat Sheet (verified 3-0) says MFA should be *mandatory* for administrative and
high-privilege users. Every Jarvis user is effectively an admin.

### Second finding: ship a code, not a clickable link

The mail-scanner / link-prebotting problem (Outlook Safe Links, corporate scanners, AV
prefetchers GET the link and burn the single-use token before the human clicks) is best solved by
**not shipping a fetchable URL at all**.

WorkOS **deprecated** its clickable magic-link API. Its current AuthKit passwordless primitive is a
six-digit one-time code, 10-minute expiry (verified 3-0). A scanner cannot consume a token that has
no URL to GET.

The verification pass caught a mechanism error worth repeating: WorkOS's immunity comes from the
*absence of a link*, **not** from same-browser binding — it does not enforce same-tab redemption.
Don't confuse the two.

### Third finding: build it in-app

`py_webauthn` (PyPI `webauthn`, duo-labs) — **v3.0.0, released 2026-06-29**, BSD-3, Production/Stable,
`requires-python >=3.10` (fine for your 3.12 pin). Four ceremony functions on the root module plus
helpers. Server-side only, framework-agnostic.

Two caveats the panel insisted on:
- 3.0.0 was **11 days old** at research time and is a **major** bump ("the PQC release" — adds ML-DSA
  post-quantum verification, prefers EdDSA, rejects malformed CBOR). v2-era tutorials will not
  compile against it. Read the changelog; pinning **2.8.0** is the conservative alternative.
- "Small surface" ≠ "nearly free". Challenge storage/expiry, session binding, credential
  persistence, rp_id/origin config and sign-count policy are **all yours to write**.

The build-vs-buy comparison is the **weakest part of this research** — see Gaps below. Only Pocket ID
survived verification, and it's a full OIDC *provider*, meaning you'd write an OIDC relying-party
flow into FastAPI *and* run a second container. Given you already have SQLite + a typed-repository
layer + Fernet-at-rest + `JARVIS_SECRETS_KEY`, in-app is the lowest operational tax. But present
this as a well-reasoned recommendation, **not** as "every alternative was evaluated and lost".

---

## Part 2 — The recommended architecture

```
First run       : email on allow-list → 6-digit code → session → MUST enroll a passkey
Daily login     : passkey (conditional UI / autofill). No email round-trip at all.
Recovery        : emailed 6-digit code → session → re-enroll passkey
                  (+ one-time recovery codes shown once at enrollment)
Sensitive routes: /mcp/*, /settings/* require a FRESH passkey assertion (step-up, 5-min window)
```

**Sessions:** opaque token, `secrets.token_urlsafe(32)`, **SHA-256 hashed at rest** (not Fernet —
you never need to read it back; you only need to compare). Stored in SQLite. Server-side so you get
real revocation ("sign out all devices", revoke on allow-list removal). APScheduler job prunes
expired rows.

**Cookie:** `__Host-jarvis_session`, `Secure` (NIST **SHALL**), `HttpOnly`, `Path=/`,
**`SameSite=Lax`** (NIST: SHOULD be Lax *or* Strict — but see the trap below), opaque contents.
Rotate the token at the authentication event (OWASP; NIST does not actually say this — cite OWASP).

**Auth middleware:** deny-by-default. Explicit exempt list only:
`/healthz`, `/events/webhook` (already bearer-authed), `/auth/*`, `/static/*`.

### The traps — each of these will bite

| Trap | Why | Fix |
|---|---|---|
| **`SameSite=Strict` breaks `/oauth/callback`** | An external OAuth provider does a **cross-site top-level redirect** back into your app. Strict cookies are **not sent** on that navigation → the callback appears logged-out. | Use `SameSite=Lax`. |
| **SSE + cookies** | `EventSource` **cannot set custom headers**, so `/events/stream` must ride the session cookie. Worse: a stream opened *before* a revoke keeps streaming forever. | Cookie auth + **re-check the session on each heartbeat tick** and break the loop when revoked. |
| **Mail scanners burn the token** | Safe Links GETs your link. | Ship a **code**, not a link. If you ship a link too: GET serves an interstitial, **POST** consumes. |
| **WebAuthn `rp_id`** | Must be a **domain**. **IP addresses are prohibited** — passkeys will **not** work at `http://192.168.x.x:8080`. Ports are excluded from `rp_id` but **included** in expected origin. | `rp_id` from config. Local dev: `rp_id="localhost"`, `expected_origin="http://localhost:8080"`. Credentials registered against `localhost` are bound to it and must be re-registered in prod. |
| **WebAuthn user handle** | W3C §14.6.1 is **normative**: the user handle **MUST NOT** contain PII — **not the email address**, not an unsalted hash of it. An authenticator can surface it *without* user verification. | `user_handle = secrets.token_bytes(16)`, stable forever (rotating it orphans discoverable credentials). |
| **Timing-based user enumeration** | A generic response body is **not enough**. CVE-2026-26185 (Directus) is a live 2026 proof: generic message, but validation ran *before* timing equalization → ~500ms delta. The mail send **is** the timing oracle. | **Never early-return** on an allow-list miss. Enqueue the send off the request path so both branches do identical in-request work. |
| **Consume-once race on SQLite** | Read-then-write races on concurrent clicks. | Single atomic CAS: `UPDATE auth_codes SET consumed_at=:now WHERE id=:id AND consumed_at IS NULL RETURNING user_id`. Zero rows = already consumed. |
| **Spoofable `X-Forwarded-For`** | If uvicorn trusts XFF from anyone, an attacker spoofs the header and walks past per-IP rate limits. | `--forwarded-allow-ips=<proxy IP only>`, never `*`. |
| **The existing CSRF hole** | `SameOriginUnsafeMethodMiddleware._is_same_origin()` returns **`True` when both Origin and Referer are absent** (`jarvis/web/security.py:46`). | Close it to deny-by-default **and** add a synchronizer token. NIST §5.1: "POST/PUT content SHALL contain a session identifier that the RP SHALL verify to protect against CSRF." |
| **Sign counter** | Google's guidance **deprecates** relying on it — synced passkeys generally return 0. | Store it, log regressions, **do not hard-fail**. |

### Data model

```
users              (id, email UNIQUE ← this IS the allow-list, user_handle BLOB(16),
                    created_at, disabled_at)
auth_codes         (id, user_id, code_hash, nonce_hash, expires_at, consumed_at,
                    attempts, requested_ip, requested_at)
sessions           (id, user_id, token_hash, created_at, last_seen_at, expires_at,
                    revoked_at, user_agent, ip, last_auth_at ← for step-up freshness)
webauthn_credentials (credential_id PK, user_id, public_key, sign_count, transports,
                    aaguid, backup_eligible, backup_state, name, created_at, last_used_at)
recovery_codes     (id, user_id, code_hash, consumed_at)
```

---

## Part 3 — Honest gaps in this research

The verification pass killed 5 claims and left real holes. **Do not let the plan pretend otherwise.**

1. **Email provider: NOTHING verified by the research.** No 2026 pricing, free-tier limits, or
   deliverability comparisons survived verification. **Decision (operator's, 2026-07-10):
   Mailtrap**, on an existing account. Details below were confirmed against Mailtrap's live docs,
   not the research pass.

   ⚠️ **The one mistake that silently breaks everything: Mailtrap is two products.**
   - **Email Sandbox** (`sandbox.smtp.mailtrap.io`) is a *fake* SMTP server. It **captures** mail
     into a web inbox and **never delivers it to the recipient**. Wire this up by accident and the
     app looks perfectly healthy — 250 OK on every send — while **no login code ever reaches
     anyone**. This is the default thing people integrate first, and it is the wrong one here.
   - **Email Sending** (`live.smtp.mailtrap.io`) is the real transactional delivery product. **This
     is the one we need.**

   Sandbox is genuinely useful for local dev and tests, so the `Mailer` Protocol should let us point
   at either — but the two must be *unmistakably* distinct in config, and production must fail loudly
   if it is pointed at the sandbox.

   **Live sending requires a verified domain.** Mailtrap: *"You can send emails to your recipients
   only from domains that have a Verified status. If the status is Pending or Rejected, you won't be
   able to send emails."* Verification is 4 CNAME records (domain verification, 2× DKIM, tracking) +
   1 TXT (DMARC), then an automated compliance review; DNS propagation takes 15 min–24 h. You already
   own the domain you're proxying the dashboard on, so verify that one.

   **The demo-domain trap:** an unverified Mailtrap demo domain can only send to *the email address
   used at registration*. That would work for a single-user allow-list and then fail the moment you
   add the second trusted person — a failure that looks like "the code just doesn't arrive for them."
   Verify a real domain before adding anyone else.

   The mandatory-passkey design already **de-fangs the lockout failure mode**: an enrolled user logs
   in with **no email round-trip at all**, so a mail outage is not a lockout. One-time recovery codes
   at enrollment are the true backstop.

   **Upside of Mailtrap over the Gmail SMTP option we considered:** it breaks the correlated-failure
   problem. Gmail-as-recovery-channel would have meant one compromised Google account is *both* the
   way back into the dashboard *and* one of the crown jewels in Jarvis's OAuth vault. A dedicated
   sending service keeps the recovery channel separate from the secrets being protected. (The email
   *inbox* receiving the code is still a security floor — see below — but it's no longer the same
   credential as the sending path.)
2. **Rate limiting: nothing verified.** slowapi vs. hand-rolled was not settled. Judgment: for a
   single-process uvicorn app, an **in-memory token bucket is fine** and avoids a dependency.
3. **CSP + HTMX: unverified.** htmx needs `unsafe-eval` for `hx-vals`/`hx-on` expression evaluation
   *unless* you set `htmx.config.allowEval=false` and avoid those attributes. Treat as unverified —
   test it.
4. **The steelman was never refuted.** "Don't expose it publicly at all" (Tailscale/WireGuard-only)
   costs **zero application code** and has **no login attack surface**. It remains the strongest
   security answer on the merits. You've chosen public exposure — the mandatory passkey is what
   makes that defensible. Worth revisiting honestly: *is there actually a device you need access
   from that cannot run a VPN client?* If not, the safest build is no build.

**Claims that were REFUTED — do not repeat these:**
- OWASP names **no** specific magic-link TTL. Any 5/10/15-minute figure is engineering judgment, not
  a citation.
- OWASP does **not** endorse "entropy beats a slow hash" for tokens. (A single SHA-256 of
  `token_urlsafe(32)` is still defensible — just don't cite OWASP for it.)
- The OWASP Authentication Cheat Sheet does **not** endorse passkeys-as-second-factor; it frames
  FIDO/passkeys as a password *alternative*.
- NIST's supposed 10-minute / 6-digit OOB parameters could **not** be verified as stated.
- OWASP Cheat Sheets are **guidance**, not a certifiable standard. Write "OWASP prescribes", never
  "OWASP requires".

---

# Part 4 — The prompts

Seven sequential PR-sized prompts. Run them in order; each assumes the previous merged.
Each is written to be pasted into Claude Code as-is.

---

## Prompt 1 — Foundations: config, data model, migration, repository

```
Add the data model and configuration for dashboard authentication. This is the first of seven PRs;
it must NOT enforce auth yet — no middleware, no route changes. Schema and config only, so it can
merge safely without locking anyone out.

Read CLAUDE.md first. Branch off main.

CONFIG (jarvis/config/schema.py — note _StrictModel forbids extra keys, so the yaml and the model
must agree exactly, and config/jarvis.yaml.example must be updated in the same PR or the app won't
boot):

  class AuthConfig(_StrictModel):
      enabled: bool = False          # default OFF; we flip it on in PR 2
      allowed_emails: list[str] = [] # the closed allow-list. No open signup, ever.
      rp_id: str = "localhost"       # WebAuthn Relying Party ID
      rp_name: str = "Jarvis"
      expected_origin: str = "http://localhost:8080"
      session_ttl_days: int = 30
      session_idle_timeout_days: int = 7
      code_ttl_minutes: int = 10
      step_up_window_minutes: int = 5

  Add `auth: AuthConfig = Field(default_factory=AuthConfig)` to JarvisConfig.

  rp_id and expected_origin are SEPARATE and NOT interchangeable — rp_id excludes the port,
  expected_origin includes it. Document that in the yaml example.

MIGRATION (alembic/versions/0015_auth.py — next in sequence after 0014_documents):
Read alembic/versions/0014_documents.py first and match its style exactly.
CRITICAL, from CLAUDE.md: if you insert any UUIDs, use uuid4().hex, NEVER str(uuid4()) — dashed
UUIDs silently miss on SQLite PK lookups.

  users:                (id PK, email TEXT UNIQUE NOT NULL, user_handle BLOB NOT NULL,
                         created_at, disabled_at NULL)
  auth_codes:           (id PK, user_id FK->users, code_hash TEXT NOT NULL,
                         nonce_hash TEXT NULL, expires_at NOT NULL, consumed_at NULL,
                         attempts INT NOT NULL DEFAULT 0, requested_ip TEXT NULL, requested_at)
  sessions:             (id PK, user_id FK->users, token_hash TEXT UNIQUE NOT NULL, created_at,
                         last_seen_at, expires_at, revoked_at NULL, last_auth_at NOT NULL,
                         user_agent TEXT NULL, ip TEXT NULL)
  webauthn_credentials: (credential_id TEXT PK, user_id FK->users, public_key BLOB NOT NULL,
                         sign_count INT NOT NULL DEFAULT 0, transports TEXT NULL,
                         aaguid TEXT NULL, backup_eligible BOOL, backup_state BOOL,
                         name TEXT NULL, created_at, last_used_at NULL)
  recovery_codes:       (id PK, user_id FK->users, code_hash TEXT NOT NULL, consumed_at NULL)

  Index sessions.token_hash, auth_codes.user_id, webauthn_credentials.user_id.
  All datetimes use the TZDateTime type from jarvis/persistence/db.py — naive datetimes RAISE.

MODELS (jarvis/persistence/models.py): the five corresponding *Row classes, matching existing style.

  user_handle is the WebAuthn user handle. W3C WebAuthn §14.6.1 is NORMATIVE: it MUST NOT contain
  PII — it is NOT the email and NOT a hash of the email. Generate it as secrets.token_bytes(16) at
  user creation and NEVER rotate it (rotating orphans discoverable credentials used for passkey
  autofill). Put that reasoning in a short comment — it is a constraint the code cannot show.

REPOSITORY (jarvis/persistence/repositories.py — per CLAUDE.md this is the ONLY way feature code
touches the DB; no raw sessions in routes):

  class AuthRepo with:
    - get_or_create_user(email) — only for allow-listed emails; caller enforces the allow-list
    - create_auth_code(user_id, code_hash, nonce_hash, expires_at, ip)
    - consume_auth_code(code_hash) -> user_id | None
        MUST be a single atomic compare-and-swap, not a read-then-write:
          UPDATE auth_codes SET consumed_at = :now
           WHERE code_hash = :hash AND consumed_at IS NULL AND expires_at > :now
          RETURNING user_id
        Zero rows returned means already-consumed or expired. This is what makes concurrent
        clicks (and mail-scanner prefetches) safe. Do NOT implement it as SELECT-then-UPDATE.
    - create_session / get_session_by_token_hash / touch_session / revoke_session
    - revoke_all_sessions_for_user(user_id)   ← "sign out everywhere"
    - delete_expired_sessions_and_codes()     ← for the APScheduler cleanup job
    - credential CRUD (add/list/get/update sign_count and last_used_at/delete)
    - recovery code create/consume (same atomic CAS pattern as auth codes)

  Tokens and codes are SHA-256 hashed at rest, NOT Fernet-encrypted. Fernet is reversible and we
  never need to read these back — we only ever compare. Store hashlib.sha256(token.encode())
  .hexdigest(). Do NOT reuse jarvis/oauth/crypto.py here; it solves a different problem (it
  encrypts OAuth tokens that must be decrypted for use).

TESTS:
  - tests/integration/test_migration_0015_auth.py — runs REAL alembic via subprocess against a
    scratch DB, per CLAUDE.md's migration rule. Model the file on
    tests/integration/test_migration_0011.py.
  - tests/integration/test_auth_repo.py — real SQLite. Must include a CONCURRENCY test that fires
    two simultaneous consume_auth_code() calls against the same code with asyncio.gather and
    asserts exactly ONE returns a user_id and the other returns None.

Run `make check` before opening the PR. Do not run a blind `make fmt` — per my saved notes it
reformats ~40 unrelated files; format only the files this branch touches.
```

---

## Prompt 2 — Session layer + deny-by-default middleware

```
Add the session layer and the auth middleware. After this PR the dashboard is LOCKED — so this PR
must also ship the emailed-code login flow's server side well enough that I can actually get in.
If you'd rather keep the PR small, ship the middleware with auth.enabled defaulting to False and
flip it in PR 3; say which you chose and why.

Read CLAUDE.md and jarvis/web/app.py and jarvis/web/security.py first.

SESSION LAYER (new jarvis/auth/sessions.py):
  - issue_session(user_id, request) -> raw_token: secrets.token_urlsafe(32); store only the
    SHA-256 hash; set last_auth_at = now.
  - Cookie: name "__Host-jarvis_session", Secure=True, HttpOnly=True, Path="/", SameSite="lax".

    SameSite MUST be "lax", NOT "strict". This is a real trap, not a preference: GET /oauth/callback
    receives a CROSS-SITE TOP-LEVEL REDIRECT back from an external OAuth provider, and a Strict
    cookie is NOT sent on that navigation — so the callback would appear logged-out and the whole
    MCP OAuth flow would break. Leave a comment saying so.

    The __Host- prefix requires Secure + Path=/ + no Domain attribute. That means the cookie will
    NOT be set over plain http — which is correct for production behind TLS, but breaks local dev
    on http://localhost. Make the Secure flag + prefix conditional on a config/env flag
    (e.g. auth.secure_cookies, default True; docs tell devs to set it False locally).
  - Rotate the session token at every authentication event (OWASP session-management guidance —
    defeats session fixation). NIST does not actually say this; cite OWASP, not NIST.
  - Validate: not revoked, not past expires_at, and last_seen_at within idle timeout. Touch
    last_seen_at on use (throttle the write to at most once a minute so we're not writing to SQLite
    on every request).

MIDDLEWARE (jarvis/web/auth_middleware.py): DENY BY DEFAULT. Every route requires a valid session
unless explicitly exempt. Exempt paths ONLY:
  - /healthz            (Docker healthcheck + monitoring)
  - /events/webhook     (already authenticated with a Bearer token via secrets.compare_digest —
                         do NOT double-auth it, and do NOT let session auth shadow the 404-when-
                         disabled behavior)
  - /auth/*             (the login routes themselves)
  - /static/*
Everything else — including / , /mcp/*, /settings/*, /events/stream and /oauth/* — requires a
session. Unauthenticated HTML requests redirect to /auth/login; unauthenticated HTMX requests
(HX-Request header present) must return a response HTMX will act on — use 401 + an HX-Redirect
header, because a plain redirect gets swallowed into a partial swap and the user sees a login form
injected into a table cell.

Add `request.state.user` for downstream handlers.

FIX THE EXISTING CSRF HOLE — jarvis/web/security.py:46. _is_same_origin() currently returns True
when BOTH Origin and Referer are absent. That fail-open fallback must become deny-by-default now
that this app is internet-facing. Change it to return False, and check what breaks: non-browser
callers that legitimately POST without an Origin header. /events/webhook is the one I know of — it
is exempt from this middleware anyway, but VERIFY that and tell me what else you find. Do not
silently loosen it back.

SSE (jarvis/web/routes/events.py): the /events/stream generator loops on a 0.1s tick. A stream
opened BEFORE a session is revoked will otherwise keep streaming forever. Re-validate the session
inside that loop (every ~10s, not every tick) and break cleanly when the session is gone. Note that
EventSource cannot send custom headers, which is exactly why the session must ride the cookie.

CLEANUP JOB: an APScheduler job (see jarvis/scheduler/) calling
delete_expired_sessions_and_codes() daily, so the tables don't grow forever.

TESTS (tests/integration/): every exempt path reachable without a session; a representative
protected path 302s (and 401+HX-Redirect for an HX-Request); an expired session rejected; a revoked
session rejected; the session token rotates on login; the SSE stream terminates after revocation.

make check must be green.
```

---

## Prompt 3 — Emailed one-time code (enrollment + recovery)

```
Implement the emailed one-time-code flow. Read CLAUDE.md first.

IMPORTANT FRAMING — do not build a clickable magic link as the primary path.

NIST SP 800-63B-4 §3.1.3.1: "Email SHALL NOT be used for out-of-band authentication." The same
section explicitly permits emailed confirmation codes for ADDRESS VALIDATION and RECOVERY. So this
flow is the enrollment and recovery channel, NOT the day-to-day authenticator — the passkey (PR 4)
is the authenticator. Put that reasoning in the module docstring.

Ship a 6-DIGIT CODE, not a clickable link. WorkOS deprecated its magic-link API in favour of exactly
this. The reason is structural: a corporate mail scanner, Outlook Safe Links, or an AV prefetcher
cannot consume a token that has no URL to GET. A clickable link gets silently burned before the
human clicks it. (If you add a convenience link later, GET must serve an interstitial and only POST
may consume the token — but do not add it in this PR.)

ROUTES (jarvis/web/routes/auth.py):
  GET  /auth/login          — email form
  POST /auth/login          — request a code
  GET  /auth/verify         — code entry form
  POST /auth/verify         — verify code, issue session, redirect
  POST /auth/logout         — revoke session
  POST /auth/logout-all     — revoke every session for the user

ENUMERATION RESISTANCE — this is subtle and the naive version is broken:
  POST /auth/login must return an IDENTICAL response for an allow-listed and a non-allow-listed
  email — identical body, identical status, AND IDENTICAL TIMING. A generic message is NOT enough.
  CVE-2026-26185 (Directus, 2026) is a live proof: generic message present, but validation ran
  before the timing-equalization step, leaving a ~500ms delta — orders of magnitude above internet
  jitter, trivially measurable.

  Concretely: NEVER early-return on an allow-list miss. The mail send IS the timing oracle (it costs
  hundreds of ms). Hand the send to a background task so the on-list and off-list branches do
  identical in-request work, and always render the same "If that address is registered, we've sent
  a code" page. Rate-limit responses must be identical for on- and off-list addresses too.

CODE DESIGN:
  - 6 digits from secrets.randbelow — but store only the SHA-256 hash (per PR 1).
  - 10-minute TTL. NOTE: this number is engineering judgment. Do NOT write a comment claiming OWASP
    or NIST mandates it — the research explicitly refuted that; neither names a TTL.
  - Consume-once via the atomic CAS in AuthRepo. Never SELECT-then-UPDATE.
  - Max 5 verify attempts per code, then it's dead. Requesting a new code must NOT reset the
    attempt counter on a still-live old one.
  - Invalidate any outstanding codes for that email when a new one is requested.
  - Same-browser binding: when a code is requested, set a short-lived signed nonce cookie and store
    its hash on the auth_codes row; require it at verification. Because we ship a CODE the user
    types (not a link they click), this costs nothing in cross-device UX — they type the code into
    the tab they started in.

RATE LIMITING (jarvis/auth/ratelimit.py): hand-rolled in-memory token bucket. This is a
SINGLE-PROCESS uvicorn app, so in-memory is sufficient and beats adding a Redis-shaped dependency
we cannot satisfy. Limit per-email AND per-IP.

  TRAP: per-IP limiting is worthless — actively harmful — if the client IP is spoofable. Behind the
  reverse proxy, uvicorn must be started with --forwarded-allow-ips set to the PROXY'S IP ONLY,
  never "*". If it trusts X-Forwarded-For from anyone, an attacker sets the header themselves and
  walks straight past the limit. Wire this up and document it; PR 7 covers the proxy side.

MAILER (jarvis/auth/mailer.py): define a `Mailer` Protocol with one send method, and implement it
against MAILTRAP (I already have an account). Keep it behind the Protocol so swapping providers
later is a one-file change.

  ⚠️ READ THIS FIRST — MAILTRAP IS TWO DIFFERENT PRODUCTS AND PICKING THE WRONG ONE FAILS SILENTLY:
  - Email SANDBOX (sandbox.smtp.mailtrap.io) is a FAKE SMTP server. It CAPTURES mail into a web
    inbox and NEVER DELIVERS IT. If you wire this up for production, every send returns success, the
    app looks completely healthy, and NO LOGIN CODE EVER ARRIVES. This is the integration most
    tutorials show. It is NOT what production needs.
  - Email SENDING (live.smtp.mailtrap.io) is the real transactional delivery product. THIS IS THE
    ONE PRODUCTION USES.
  Support BOTH — sandbox is genuinely the right thing for local dev and manual testing — but make
  them unmistakably distinct in config, and make production FAIL LOUDLY at startup if it is pointed
  at a sandbox host. A silent no-delivery path in an auth system is exactly the bug that locks me
  out with no error to look at.

  PREFER THE HTTP API OVER SMTP. Mailtrap exposes POST https://send.api.mailtrap.io/api/send. Use it
  with httpx (ALREADY a dependency) rather than stdlib smtplib, because:
    - stdlib smtplib is BLOCKING. Calling it inline in an async route stalls the event loop for the
      whole SMTP round-trip — and in this single-process app that stalls the Discord adapter, the
      scheduler, and every other in-flight request, not just this one. httpx is async-native and the
      problem evaporates.
    - Fewer moving parts (no STARTTLS negotiation, no 535-auth-error debugging).
  I did NOT verify the exact auth header for the HTTP API — Mailtrap's docs page didn't state it and
  I won't guess. GET IT FROM THE DASHBOARD (Sending Domains → your domain → Integration → API) and
  put the real header in the code. Do not copy an auth header from a blog post.

  If you use SMTP instead, the LIVE credentials are: host live.smtp.mailtrap.io, port 587,
  username literally "api", password = your API token. (Sandbox uses a per-inbox username/password
  on sandbox.smtp.mailtrap.io — different creds entirely.) If you go this route you MUST run the
  blocking send off the event loop via asyncio.to_thread.

  DOMAIN VERIFICATION — the send will simply not work without it. Mailtrap: "You can send emails to
  your recipients only from domains that have a Verified status. If the status is Pending or
  Rejected, you won't be able to send emails." Verification = 4 CNAME records (domain verification,
  2x DKIM, tracking) + 1 TXT (DMARC), then an automated compliance review. DNS propagation is
  15 min - 24 h. Use the same domain the dashboard is proxied on.

  THE DEMO-DOMAIN TRAP, worth a doc line: an unverified Mailtrap demo domain can ONLY send to the
  email address used at registration. That silently works for a one-person allow-list and then
  breaks the instant a second trusted person is added — presenting as "the code just never arrives
  for them," with a 200 OK on our side. Flag it.

  CONFIG (a MailConfig sibling of AuthConfig — must be a _StrictModel, and config/jarvis.yaml.example
  must be updated in the SAME PR or the app won't boot):
    provider: Literal["mailtrap_api", "mailtrap_smtp", "console"] = "console"
    api_token: str | None      # ${JARVIS_MAILTRAP_TOKEN}
    smtp_host / smtp_port      # only for the smtp path; sandbox vs live selected here
    from_addr: str             # must be on the verified domain
    sandbox: bool = False      # if True, refuse to start when auth.enabled and not DEBUG
  Add a "console" mailer that just logs the code — that is how I develop locally without sending
  real mail at all, and it keeps tests hermetic.

  Secrets come via ${VAR} env expansion. NOTE config/loader.py's expand_env raises ConfigLoadError at
  LOAD time if the var is missing — good (fail fast), but it means the var must exist in
  docker-compose AND in local dev, or the app won't start.

  If the send raises, log it to the audit trail — but the USER-FACING RESPONSE MUST NOT CHANGE. A
  different response on send-failure would reintroduce the enumeration oracle we just closed.

  RATIONALE TO RECORD IN THE PR: a dedicated transactional sender keeps the recovery channel
  SEPARATE from the secrets Jarvis protects. (We considered Gmail SMTP and rejected it: Jarvis's
  OAuth vault holds tokens for that same Google account, so Gmail-as-recovery-channel would have made
  one compromised Google account both the way back INTO the dashboard and one of the crown jewels
  inside it.) The "mail is down = locked out" objection is largely defused anyway by the
  mandatory-passkey design: an ENROLLED user logs in with NO email round-trip at all. Mail is only on
  the enrollment and recovery paths.

  BE HONEST IN THE PR DESCRIPTION: the deep-research pass verified NOTHING about email-provider
  pricing or deliverability. Mailtrap is an operator decision; the product/host/verification details
  above come from Mailtrap's own live docs, not from that research.

AUDIT: log every code request, success, failure, and rate-limit trip via the existing AuditRepo.

TESTS: enumeration resistance (identical status+body for on/off-list — and assert the code path
does not early-return); code expiry; the 5-attempt lockout; a new code not resetting the old one's
counter; nonce-cookie mismatch rejected; rate limiter; a mailer failure not changing the response.
```

---

## Prompt 4 — Passkeys (the actual authenticator)

```
Add WebAuthn/passkey registration and login with py_webauthn. Read CLAUDE.md first.

DEPENDENCY: add `webauthn` to pyproject.toml. Version choice — check the changelog and TELL ME what
you picked:
  - 3.0.0 (released 2026-06-29) is the latest: BSD-3, Production/Stable, requires-python >=3.10.
    It's a MAJOR bump ("the PQC release" — ML-DSA post-quantum signature verification, prefers
    EdDSA, rejects malformed CBOR with duplicate keys).
  - 2.8.0 is the conservative pin.
Any tutorial you find written against v2 will NOT compile against v3. Read the actual changelog,
don't trust blog posts.

This library is server-side only and framework-agnostic — a small surface (four ceremony functions
on the root module: generate_registration_options / verify_registration_response /
generate_authentication_options / verify_authentication_response, plus options_to_json and
base64url_to_bytes; typed structs and exceptions live under webauthn.helpers). "Small surface" does
NOT mean cheap: challenge storage and expiry, session binding, credential persistence, rp_id/origin
config and sign-count policy are all OURS to write.

ROUTES:
  POST /auth/passkey/register/begin     (requires an authenticated session)
  POST /auth/passkey/register/complete  (requires an authenticated session)
  POST /auth/passkey/login/begin        (unauthenticated)
  POST /auth/passkey/login/complete     (unauthenticated → issues a session)
  GET  /settings/passkeys               (list / rename / delete credentials)

A passkey may ONLY be registered from inside an already-authenticated session. That's what binds it
to an account established via the emailed code.

MANDATORY ENROLLMENT: after logging in with an emailed code, if the user has ZERO credentials,
force them to /auth/passkey/register before any other route is usable. The emailed code is the
enrollment/recovery channel; the passkey is the authenticator. NIST §3.1.3.1 prohibits email as an
out-of-band AUTHENTICATION channel, so "email code only" must not be a steady state.

Also generate 8 one-time RECOVERY CODES at first enrollment, display them ONCE, store only hashes.
This is the real backstop if the user loses every passkey AND email is down.

CHALLENGES: store server-side (a short-TTL table or the existing session row), 5-minute expiry,
single-use. Never trust a challenge echoed back by the client.

CONFIG: rp_id, rp_name, expected_origin come from AuthConfig (PR 1).

  THE rp_id TRAP — get this wrong and nothing works:
  - rp_id must be a DOMAIN string: the exact hostname or a registrable parent domain (eTLD+1 or
    higher). IP ADDRESSES ARE PROHIBITED by the spec. Passkeys will NOT work when the dashboard is
    reached at http://192.168.x.x:8080 — only on the real reverse-proxied domain, or localhost.
  - PORTS ARE EXCLUDED from rp_id but INCLUDED in the expected origin. py_webauthn takes
    expected_rp_id and expected_origin as SEPARATE, non-interchangeable params, and a port mismatch
    on expected_origin is the single most common integration error.
  - Local dev: rp_id="localhost", expected_origin="http://localhost:8080". localhost is an explicit
    exception to WebAuthn's HTTPS requirement.
  - Credentials registered against rp_id="localhost" are BOUND to it and must be re-registered
    against the production RP ID. Say so in the docs.
  - Cross-device/hybrid (phone QR) testing needs a real HTTPS domain or a tunnel — a phone cannot
    reach your laptop's localhost. Safari is historically flakiest on http://localhost WebAuthn;
    validate on Chrome/Firefox locally.

CREDENTIAL STORAGE: persist credential_id, public_key, sign_count, transports, aaguid,
backup_eligible, backup_state, name, created_at, last_used_at (table from PR 1).

  SIGN COUNTER: store it and LOG a regression, but DO NOT hard-fail on it. Google's own passkey
  guidance deprecates relying on the signature counter — synced passkeys generally return 0, so a
  hard-fail bricks legitimate logins. This is deliberate; comment it so nobody "fixes" it later.

  USER HANDLE: use users.user_handle (random 16 bytes, from PR 1). NEVER the email — W3C §14.6.1 is
  normative that the user handle MUST NOT contain PII, because an authenticator can surface it
  WITHOUT user verification, leaking it to whoever holds the device.

Use discoverable credentials (resident keys) + user verification "preferred", and wire up
CONDITIONAL UI (passkey autofill) on the login page so the passkey is the path of least resistance
and the emailed code is the fallback — not the reverse.

FRONTEND: vanilla JS in the existing Jinja/HTMX templates. No SPA framework, no CDN script — match
the existing style in jarvis/web/templates/ and static/.

TESTS: registration and authentication ceremonies with a mocked authenticator; challenge expiry and
reuse rejected; origin/rp_id mismatch rejected; sign-count regression logged but NOT fatal;
registration rejected without a session; the zero-credential user forced to enroll.
```

---

## Prompt 5 — Step-up re-authentication on sensitive routes

```
Add step-up re-authentication. Read CLAUDE.md first.

WHY (put this in the PR description): this dashboard drives an LLM agent with tool access and holds
Fernet-encrypted OAuth tokens for Gmail, Calendar and Fastmail. An attacker with a stolen live
session is not "reading my dashboard" — they are one click from a full mailbox compromise. OWASP's
MFA guidance supports re-authentication for sensitive actions. A long-lived 30-day session is the
right UX for reading the dashboard and the WRONG posture for editing the OAuth token vault.

REQUIRE A FRESH PASSKEY ASSERTION (within auth.step_up_window_minutes, default 5) for:
  - /mcp/providers/*        (provider admin — the OAuth token vault)
  - /mcp/connect/*, /mcp/disconnect/*
  - /settings/*             (includes the allow-list)
  - /auth/passkey/* delete  (deleting a passkey is itself a sensitive action)
  - POST /auth/logout-all
Read the actual route table in jarvis/web/routes/ and confirm the list with me before implementing
— I would rather you ask than guess wrong in either direction.

MECHANISM: sessions.last_auth_at (PR 1). A step-up-protected route checks
now - last_auth_at < step_up_window; if stale, challenge for a passkey assertion and update
last_auth_at on success. Implement as a FastAPI dependency, not a middleware, so it's declared
per-route and visible at the call site.

HTMX INTERACTION — this is the fiddly part: these routes are hit by partial swaps, so a step-up
challenge cannot just redirect (it would get swapped into a table cell). Return a 401 with an
HX-Trigger that opens a step-up modal, and re-issue the original request after the assertion
succeeds. Get the re-issue right: the user must not lose the form they'd filled in.

Every step-up challenge, success and failure goes to the audit trail.

TESTS: a stale session challenged; a fresh one passes; a successful assertion updates last_auth_at;
the HTMX 401+HX-Trigger path; an unauthenticated request never reaches the step-up check.
```

---

## Prompt 6 — Edge hardening

```
Harden the public-internet edge. Read CLAUDE.md first. Nothing here is speculative — each item is a
specific known trap.

1. CSRF SYNCHRONIZER TOKEN. PR 2 closed the fail-open hole in SameOriginUnsafeMethodMiddleware
   (jarvis/web/security.py). Now add a real synchronizer token on top: per-session, in a hidden form
   field and an hx-headers attribute, verified on every unsafe method. NIST SP 800-63B-4 §5.1:
   "POST/PUT content SHALL contain a session identifier that the RP SHALL verify to protect against
   cross-site request forgery." Origin-checking + SameSite=Lax is defensible on its own, but for an
   agent-driving dashboard we want both. Make it work with HTMX's hx-post/hx-delete without
   annotating every element by hand (a body-level hx-headers is the clean way).

2. TRUSTED HOSTS + PROXY HEADERS.
   - TrustedHostMiddleware with the real domain (config-driven).
   - uvicorn --forwarded-allow-ips = THE PROXY'S IP ONLY. Never "*". If uvicorn trusts
     X-Forwarded-For from any source, an attacker spoofs the header and defeats the per-IP rate
     limiting from PR 3 entirely. Make it configurable and document it loudly.
   - Verify request.client.host is the REAL client IP end-to-end once the proxy is in front —
     write the test that would catch a regression here.

3. SECURITY HEADERS: HSTS (long max-age, includeSubDomains), X-Content-Type-Options: nosniff,
   Referrer-Policy: strict-origin-when-cross-origin, X-Frame-Options: DENY.

4. CSP. This one is UNVERIFIED by my research — treat it empirically, not from a blog.
   htmx needs 'unsafe-eval' for hx-vals / hx-on expression evaluation UNLESS you set
   htmx.config.allowEval=false and avoid those attributes. Grep the templates for what we actually
   use, then write the TIGHTEST CSP that genuinely works, and TEST IT IN A REAL BROWSER — do not
   ship a CSP you have only reasoned about. If we do need 'unsafe-eval', say so plainly in the PR
   rather than shipping a CSP that silently breaks the UI. Report what you found.

5. AUDIT LOGGING: every authentication event — login attempt, code request, passkey ceremony,
   step-up, logout, rate-limit trip, session revocation — through the existing AuditRepo, with IP
   and user agent.

6. LOGIN-PAGE HARDENING: exponential backoff on repeated failures from the same IP; a global cap on
   in-flight codes.

TESTS for each. make check green.
```

---

## Prompt 7 — Deployment: reverse proxy, docs, and a real end-to-end check

```
Wire up deployment and prove the whole thing actually works. Read CLAUDE.md — deploys are pull-based
on the server (CI publishes the image; the server runs `make prod-pull && make prod-up`), and each
fix costs a redeploy round-trip, so reproduce locally FIRST.

1. REVERSE PROXY: a Caddyfile (Caddy gets Let's Encrypt right with the least config) for the real
   domain, terminating TLS and proxying to jarvis:8080. It must set X-Forwarded-For / -Proto / -Host
   correctly, and the uvicorn side must trust ONLY Caddy's IP (--forwarded-allow-ips). Show both
   sides together — this pair is where per-IP rate limiting silently dies if it's wrong.

2. COMPOSE: update docker-compose.prod.yml. Check whether JARVIS_SECRETS_KEY is passed as a plain
   env var — my notes say `docker inspect` exposes container Env, so a Fernet key passed as an env
   var is readable through the read-only Docker proxy. If it is, move it to a file/Docker secret and
   flag it in the PR; that key now protects the auth tables too.

3. DOCS (README or docs/auth.md):
   - First-run bootstrap: how the first allow-listed email gets in and enrolls a passkey.
   - How to add/remove someone from the allow-list, and that removal must revoke their sessions.
   - The rp_id / expected_origin distinction, local-dev values, and the fact that passkeys DO NOT
     work over a bare LAN IP (spec prohibits IP-address rp_ids) — only on the real domain or
     localhost.
   - That credentials registered against rp_id=localhost must be re-registered in production.
   - MAILTRAP SETUP, with the two failure modes called out so loudly they cannot be missed:
       (a) SANDBOX vs SENDING. live.smtp.mailtrap.io / the send API delivers real mail;
           sandbox.smtp.mailtrap.io CAPTURES it and delivers NOTHING while still returning success.
           Production pointed at the sandbox = every login code silently vanishes.
       (b) The sending DOMAIN MUST BE VERIFIED (4 CNAME + 1 TXT/DMARC; 15 min-24 h to propagate).
           An unverified demo domain can only mail the registration address — which works for a
           one-person allow-list and then breaks silently when a second person is added.
   - The recovery-code flow, and the plain statement that BECAUSE AN EMAILED CODE IS THE RECOVERY
     PATH, THE SECURITY FLOOR OF THIS SYSTEM IS THE RECIPIENT'S EMAIL INBOX. Anyone on the allow-list
     should have a passkey / hardware 2FA on their email account, and should print the one-time
     recovery codes and store them offline — those are the only escape hatch that does not route
     through email at all.

4. END-TO-END VERIFICATION — do not just run the unit tests. Bring the stack up locally and drive
   the real flows: emailed code → session → forced passkey enrollment → logout → passkey login →
   step-up on /mcp → session revocation killing a live SSE stream. Use the /verify skill. Tell me
   what you actually observed, and if something doesn't work, say so plainly rather than reporting
   green.

5. Confirm the pre-existing non-browser callers still work: /healthz reachable unauthenticated
   (Docker healthcheck), and POST /events/webhook still authenticating on its Bearer token and still
   404ing when no token is configured.
```
```

---

## Suggested order and what each PR costs

| PR | Scope | Risk |
|---|---|---|
| 1 | Config, models, migration 0015, AuthRepo | Low — nothing enforced yet, safe to merge |
| 2 | Sessions + deny-by-default middleware + CSRF fix | **High — this is the lockout PR** |
| 3 | Emailed code + rate limiting + mailer | Medium |
| 4 | Passkeys (py_webauthn) | Medium — the rp_id trap is where time gets lost |
| 5 | Step-up re-auth | Low |
| 6 | Edge hardening (CSRF token, headers, CSP) | Medium — CSP is the unverified one |
| 7 | Caddy, docs, end-to-end verification | Low |

Keep `auth.enabled = False` until PR 4 is merged and you have successfully enrolled a passkey
locally. Flipping it on before you can complete a login is how you lock yourself out of your own
dashboard.
