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
Add the data model and config for dashboard authentication. First of seven PRs: schema and config
ONLY — nothing enforced, so it merges without locking anyone out. Read CLAUDE.md. Branch off main.

CONFIG (jarvis/config/schema.py). _StrictModel forbids extra keys, so config/jarvis.yaml.example must
be updated in the SAME PR or the app won't boot.

  class AuthConfig(_StrictModel):
      enabled: bool = False          # default OFF; we flip it on later
      allowed_emails: list[str] = [] # the closed allow-list. No open signup, ever.
      rp_id: str = "localhost"       # WebAuthn Relying Party ID
      rp_name: str = "Jarvis"
      expected_origin: str = "http://localhost:8080"
      session_ttl_days: int = 30
      session_idle_timeout_days: int = 7
      code_ttl_minutes: int = 10
      step_up_window_minutes: int = 5

  Add `auth: AuthConfig = Field(default_factory=AuthConfig)` to JarvisConfig. rp_id and
  expected_origin are NOT interchangeable — rp_id excludes the port, expected_origin includes it.

MIGRATION (alembic/versions/0015_auth.py, next after 0014_documents — match its style). Per CLAUDE.md:
any inserted UUID uses uuid4().hex, NEVER str(uuid4()) — dashed UUIDs silently miss on SQLite PK
lookups.

  users                (id PK, email TEXT UNIQUE NOT NULL, user_handle BLOB NOT NULL, created_at,
                        disabled_at NULL)
  auth_codes           (id PK, user_id FK, code_hash NOT NULL, nonce_hash NULL, expires_at NOT NULL,
                        consumed_at NULL, attempts INT DEFAULT 0, requested_ip NULL, requested_at)
  sessions             (id PK, user_id FK, token_hash TEXT UNIQUE NOT NULL, created_at, last_seen_at,
                        expires_at, revoked_at NULL, last_auth_at NOT NULL, user_agent NULL, ip NULL)
  webauthn_credentials (credential_id TEXT PK, user_id FK, public_key BLOB NOT NULL, sign_count INT
                        DEFAULT 0, transports NULL, aaguid NULL, backup_eligible, backup_state,
                        name NULL, created_at, last_used_at NULL)
  recovery_codes       (id PK, user_id FK, code_hash NOT NULL, consumed_at NULL)

  Index sessions.token_hash, auth_codes.user_id, webauthn_credentials.user_id. Datetimes use
  TZDateTime (jarvis/persistence/db.py) — naive datetimes RAISE.

MODELS (jarvis/persistence/models.py): the five *Row classes, in the existing style.

  user_handle = secrets.token_bytes(16) at user creation, NEVER rotated (rotating orphans discoverable
  credentials). W3C §14.6.1 is NORMATIVE: it MUST NOT contain PII — not the email, not a hash of it.

REPOSITORY (jarvis/persistence/repositories.py — per CLAUDE.md the ONLY way to touch the DB):

  class AuthRepo:
    - get_or_create_user(email)  # caller enforces the allow-list
    - create_auth_code(user_id, code_hash, nonce_hash, expires_at, ip)
    - consume_auth_code(code_hash) -> user_id | None — a single atomic CAS, NOT read-then-write:
        UPDATE auth_codes SET consumed_at = :now
         WHERE code_hash = :hash AND consumed_at IS NULL AND expires_at > :now
        RETURNING user_id
      Zero rows = already consumed or expired — that is what makes concurrent redemption safe.
    - create_session / get_session_by_token_hash / touch_session / revoke_session
    - revoke_all_sessions_for_user(user_id), delete_expired_sessions_and_codes()
    - credential CRUD; recovery-code create/consume (same atomic CAS)

  Tokens and codes are SHA-256 hashed at rest, NOT Fernet-encrypted: we only compare, never read back.
  Do NOT reuse jarvis/oauth/crypto.py — it solves the reversible-secret problem.

TESTS:
  - test_migration_0015_auth.py — real alembic via subprocess against a scratch DB, modelled on
    tests/integration/test_migration_0011.py.
  - test_auth_repo.py — real SQLite, with a CONCURRENCY test firing two simultaneous
    consume_auth_code() calls on one code via asyncio.gather: exactly one returns a user_id.

`make check` before the PR. No blind `make fmt` — format only this branch's files.
```

---

## Prompt 2 — Session layer + deny-by-default middleware

```
Add the session layer and the auth middleware. After this PR the dashboard is LOCKED, so this PR must
also ship enough of the emailed-code login for me to actually get in. If you'd rather keep it small,
ship the middleware with auth.enabled defaulting to False and flip it in PR 3 — say which you chose.

Read CLAUDE.md, jarvis/web/app.py and jarvis/web/security.py first.

SESSION LAYER (new jarvis/auth/sessions.py):
  - issue_session(user_id, request) -> raw token: secrets.token_urlsafe(32); store only the SHA-256
    hash; last_auth_at = now.
  - Cookie: "__Host-jarvis_session", Secure, HttpOnly, Path=/, SameSite=lax.

    SameSite MUST be lax, not strict. This is a trap, not a preference: GET /oauth/callback arrives
    as a CROSS-SITE TOP-LEVEL REDIRECT from an external OAuth provider, and a Strict cookie is NOT
    sent on that navigation — the callback would appear logged-out and the whole MCP OAuth flow would
    break. Leave a comment saying so.

    The __Host- prefix requires Secure + Path=/ + no Domain, so the cookie will NOT set over plain
    http — correct behind TLS, but it breaks local dev on http://localhost. Make the Secure flag and
    the prefix conditional on config (auth.secure_cookies, default True; docs tell devs to set it
    False locally).
  - Rotate the session token at every authentication event (OWASP session-management guidance —
    defeats fixation). NIST does not say this; cite OWASP.
  - Valid = not revoked, not past expires_at, last_seen_at within the idle timeout. Touch
    last_seen_at on use, throttled to at most once a minute so we aren't writing to SQLite on every
    request.

MIDDLEWARE (jarvis/web/auth_middleware.py): DENY BY DEFAULT. Exempt paths ONLY:
  - /healthz          (Docker healthcheck + monitoring)
  - /events/webhook   (already Bearer-authed via secrets.compare_digest — do NOT double-auth it, and
                       do NOT let session auth shadow its 404-when-disabled behavior)
  - /auth/*           (the login routes themselves)
  - /static/*
Everything else needs a session, including /, /mcp/*, /settings/*, /events/stream and /oauth/*.
Unauthenticated HTML requests redirect to /auth/login. Unauthenticated HTMX requests (HX-Request
present) return 401 + an HX-Redirect header — a plain redirect gets swallowed into a partial swap and
the user sees a login form injected into a table cell. Add request.state.user for downstream
handlers.

FIX THE EXISTING CSRF HOLE (jarvis/web/security.py:46): _is_same_origin() currently returns True when
BOTH Origin and Referer are absent. That fail-open fallback must become deny-by-default now the app
is internet-facing. Change it, then find every non-browser caller that legitimately POSTs without an
Origin header — /events/webhook is the one I know of (exempt from this middleware anyway, but VERIFY
that). Tell me what else you find; do not silently loosen it back.

SSE (jarvis/web/routes/events.py): the /events/stream generator loops on a 0.1s tick, so a stream
opened BEFORE a revoke keeps streaming forever. Re-validate the session inside that loop (every ~10s,
not every tick) and break cleanly when the session is gone. EventSource cannot send custom headers —
which is exactly why the session must ride the cookie.

CLEANUP JOB: an APScheduler job (jarvis/scheduler/) calling delete_expired_sessions_and_codes()
daily, so the tables don't grow forever.

TESTS (tests/integration/): every exempt path reachable without a session; a representative protected
path 302s (and returns 401 + HX-Redirect for an HX-Request); an expired session rejected; a revoked
session rejected; the token rotates on login; the SSE stream terminates after revocation.

make check must be green.
```

---

## Prompt 3 — Emailed one-time code (enrollment + recovery)

```
Implement the emailed one-time-code flow. Read CLAUDE.md, and Part 3 of
docs/2026-07-10-dashboard-authentication.md for the Mailtrap specifics.

FRAMING (module docstring): NIST SP 800-63B-4 §3.1.3.1 says "Email SHALL NOT be used for out-of-band
authentication" but explicitly permits emailed codes for ADDRESS VALIDATION and RECOVERY — so this is
the enrollment/recovery channel and the passkey (PR 4) is the authenticator.

Ship a 6-DIGIT CODE, not a clickable link: a scanner, Safe Links or an AV prefetcher cannot burn a
token that has no URL to GET. WorkOS deprecated its magic-link API for exactly this.

ROUTES (jarvis/web/routes/auth.py): GET/POST /auth/login, GET/POST /auth/verify (→ issue session),
POST /auth/logout, POST /auth/logout-all.

ENUMERATION RESISTANCE: POST /auth/login must return an IDENTICAL body, status AND TIMING for on- and
off-allow-list emails (rate-limit responses too). A generic message is not enough — CVE-2026-26185
(Directus) had one, but validated before equalizing timing, leaving a ~500ms delta. NEVER early-return
on a miss: the mail send IS the timing oracle, so background it and make both branches do equal work.

CODE DESIGN:
  - 6 digits from secrets.randbelow; store only the SHA-256 hash (PR 1).
  - 10-minute TTL — engineering judgment; do NOT claim OWASP/NIST mandates it (neither names a TTL).
  - Consume-once via AuthRepo's atomic CAS. Never SELECT-then-UPDATE.
  - Max 5 verify attempts. A new code invalidates outstanding ones and must NOT reset a live one's.
  - Same-browser binding: on request set a short-lived signed nonce cookie, store its hash on the row,
    require it at verification (no UX cost — the code is typed into the tab that requested it).

RATE LIMITING (jarvis/auth/ratelimit.py): hand-rolled in-memory token bucket, per-email AND per-IP —
single-process uvicorn, so no Redis-shaped dependency. TRAP: per-IP limiting is worthless if the client
IP is spoofable, so uvicorn must run with --forwarded-allow-ips set to the PROXY'S IP ONLY, never "*".

MAILER (jarvis/auth/mailer.py): a `Mailer` Protocol with one send method, implemented against MAILTRAP,
plus a "console" mailer that logs the code (local dev, hermetic tests).

  MAILTRAP IS TWO PRODUCTS AND THE WRONG ONE FAILS SILENTLY. Sandbox (sandbox.smtp.mailtrap.io) is a
  FAKE SMTP server: it captures mail and NEVER DELIVERS IT, so production wired to it returns 250 OK on
  every send, looks healthy, and no login code ever arrives. Sending (live.smtp.mailtrap.io / the send
  API) is the real product. Support both, keep them unmistakably distinct in config, and FAIL LOUDLY at
  startup if production points at a sandbox host.

  PREFER THE HTTP API (POST https://send.api.mailtrap.io/api/send via httpx, already a dependency) over
  blocking smtplib, which stalls this single-process app's event loop — Discord and the scheduler with
  it. I did NOT verify the auth header: take it from the dashboard, not a blog post. The sending domain
  must be VERIFIED or nothing sends, and an unverified demo domain can only mail the registration
  address — fine for one person, silently broken when a second is added.

  CONFIG (MailConfig, a _StrictModel sibling of AuthConfig; update config/jarvis.yaml.example in the
  SAME PR):
    provider: Literal["mailtrap_api", "mailtrap_smtp", "console"] = "console"
    api_token: str | None      # ${JARVIS_MAILTRAP_TOKEN}
    smtp_host / smtp_port      # smtp path only; sandbox vs live selected here
    from_addr: str             # must be on the verified domain
    sandbox: bool = False      # if True, refuse to start when auth.enabled

  A send failure is audited, but the USER-FACING RESPONSE MUST NOT CHANGE — that would reintroduce the
  enumeration oracle.

TESTS: enumeration resistance (identical status+body, and assert no early return); code expiry; the
5-attempt lockout; a new code not resetting the old counter; nonce mismatch; the rate limiter; a mailer
failure not changing the response.
```

---

## Prompt 4 — Passkeys (the actual authenticator)

```
Add WebAuthn/passkey registration and login with py_webauthn. Read CLAUDE.md first.

DEPENDENCY: add `webauthn` to pyproject.toml. Read the actual changelog — anything written against v2
will NOT compile against v3 — and TELL ME what you picked:
  - 3.0.0 (2026-06-29): BSD-3, Production/Stable, requires-python >=3.10. A MAJOR bump, "the PQC
    release" — ML-DSA post-quantum verification, prefers EdDSA, rejects malformed CBOR.
  - 2.8.0: the conservative pin.

Server-side only: four ceremony functions on the root module, plus options_to_json and
base64url_to_bytes. Small surface does NOT mean cheap — challenge storage and expiry, session binding,
credential persistence, rp_id/origin config and sign-count policy are all ours to write.

ROUTES:
  POST /auth/passkey/register/begin|complete   (require an authenticated session)
  POST /auth/passkey/login/begin|complete      (unauthenticated → issues a session)
  GET  /settings/passkeys                      (list / rename / delete credentials)

A passkey may ONLY be registered from inside an already-authenticated session — that is what binds it
to an account established via the emailed code.

MANDATORY ENROLLMENT: after an emailed-code login, a user with ZERO credentials is forced to
/auth/passkey/register before any other route is usable. NIST §3.1.3.1 prohibits email as an
out-of-band AUTHENTICATION channel, so "emailed code only" must never be a steady state.

Generate 8 one-time RECOVERY CODES at first enrollment, display them ONCE, store only hashes — the
backstop if every passkey is lost AND email is down.

CHALLENGES: stored server-side (short-TTL table or the session row), 5-minute expiry, single-use. Never
trust a challenge echoed back by the client.

CONFIG: rp_id, rp_name, expected_origin from AuthConfig (PR 1).

  THE rp_id TRAP — get this wrong and nothing works:
  - rp_id must be a DOMAIN: the exact hostname or a registrable parent domain. IP ADDRESSES ARE
    PROHIBITED by the spec, so passkeys will NOT work at http://192.168.x.x:8080 — only on the real
    reverse-proxied domain, or localhost.
  - PORTS ARE EXCLUDED from rp_id but INCLUDED in the expected origin. py_webauthn takes expected_rp_id
    and expected_origin as separate, non-interchangeable params, and a port mismatch on
    expected_origin is the single most common integration error.
  - Local dev: rp_id="localhost", expected_origin="http://localhost:8080" (localhost is the explicit
    exception to the HTTPS requirement). Credentials registered against localhost are BOUND to it and
    must be re-registered in production — say so in the docs.
  - Cross-device (phone QR) testing needs a real HTTPS domain or a tunnel; Safari is flakiest on
    http://localhost, so validate on Chrome/Firefox locally.

CREDENTIAL STORAGE: credential_id, public_key, sign_count, transports, aaguid, backup_eligible,
backup_state, name, created_at, last_used_at (table from PR 1).

  SIGN COUNTER: store it and LOG regressions, but DO NOT hard-fail. Google's passkey guidance
  deprecates relying on the counter — synced passkeys generally return 0, so a hard-fail bricks
  legitimate logins. This is deliberate; comment it so nobody "fixes" it later.
  USER HANDLE: users.user_handle (random 16 bytes, PR 1). NEVER the email — W3C §14.6.1 is normative
  that it MUST NOT contain PII, because an authenticator can surface it WITHOUT user verification.

Use discoverable credentials + user verification "preferred", and wire up CONDITIONAL UI (passkey
autofill) on the login page so the passkey is the path of least resistance and the emailed code is the
fallback — not the reverse.

FRONTEND: vanilla JS in the existing Jinja/HTMX templates — no SPA framework, no CDN script.

TESTS: both ceremonies with a mocked authenticator; challenge expiry and reuse rejected; origin and
rp_id mismatch rejected; a sign-count regression logged but NOT fatal; registration rejected without a
session; a zero-credential user forced to enroll.
```

---

## Prompt 5 — Step-up re-authentication on sensitive routes

```
Add step-up re-authentication. Read CLAUDE.md first.

WHY (put this in the PR description): this dashboard drives an LLM agent with tool access and holds
Fernet-encrypted OAuth tokens for Gmail, Calendar and Fastmail. An attacker with a stolen live session
is not "reading my dashboard" — they are one click from a full mailbox compromise. A 30-day session is
the right UX for reading the dashboard and the wrong posture for editing the OAuth token vault; OWASP's
MFA guidance supports re-authentication for sensitive actions.

REQUIRE A FRESH PASSKEY ASSERTION (within auth.step_up_window_minutes, default 5) for:
  - /mcp/providers/*, /mcp/connect/*, /mcp/disconnect/*   (the OAuth token vault)
  - /settings/*                                           (includes the allow-list)
  - deleting a passkey                                    (itself a sensitive action)
  - POST /auth/logout-all
Read the real route table in jarvis/web/routes/ and confirm the list with me before implementing — I
would rather you ask than guess wrong in either direction.

MECHANISM: sessions.last_auth_at (PR 1). A protected route checks now - last_auth_at <
step_up_window; if stale, challenge for a passkey assertion and update last_auth_at on success.
Implement as a FastAPI dependency, not middleware, so it is declared per-route and visible at the call
site.

HTMX — the fiddly part: these routes are hit by partial swaps, so the challenge cannot redirect (it
would get swapped into a table cell). Return 401 with an HX-Trigger that opens a step-up modal, and
re-issue the original request after the assertion succeeds — without losing a form the user had
already filled in.

Every step-up challenge, success and failure goes to the audit trail.

TESTS: a stale session challenged; a fresh one passes; a successful assertion updates last_auth_at; the
HTMX 401 + HX-Trigger path; an unauthenticated request never reaching the step-up check.
```

---

## Prompt 6 — Edge hardening

```
Harden the public-internet edge. Read CLAUDE.md first. Nothing here is speculative — each item is a
specific known trap.

1. CSRF SYNCHRONIZER TOKEN. PR 2 closed the fail-open hole in SameOriginUnsafeMethodMiddleware
   (jarvis/web/security.py); now add a real token on top: per-session, in a hidden form field and an
   hx-headers attribute, verified on every unsafe method. NIST SP 800-63B-4 §5.1: "POST/PUT content
   SHALL contain a session identifier that the RP SHALL verify to protect against cross-site request
   forgery." Origin-checking + SameSite=Lax is defensible alone, but an agent-driving dashboard wants
   both. Make it work with hx-post/hx-delete without hand-annotating every element — a body-level
   hx-headers is the clean way.

2. TRUSTED HOSTS + PROXY HEADERS.
   - TrustedHostMiddleware with the real domain (config-driven).
   - uvicorn --forwarded-allow-ips = THE PROXY'S IP ONLY, never "*". If uvicorn trusts X-Forwarded-For
     from any source, an attacker spoofs the header and defeats PR 3's per-IP rate limiting entirely.
     Make it configurable and document it loudly.
   - Verify request.client.host is the REAL client IP end-to-end once the proxy is in front, and write
     the test that would catch a regression here.

3. SECURITY HEADERS: HSTS (long max-age, includeSubDomains), X-Content-Type-Options: nosniff,
   Referrer-Policy: strict-origin-when-cross-origin, X-Frame-Options: DENY.

4. CSP — UNVERIFIED by my research; treat it empirically, not from a blog. htmx needs 'unsafe-eval' for
   hx-vals / hx-on expression evaluation UNLESS you set htmx.config.allowEval=false and avoid those
   attributes. Grep the templates for what we actually use, write the TIGHTEST CSP that genuinely
   works, and TEST IT IN A REAL BROWSER — do not ship a CSP you have only reasoned about. If we do need
   'unsafe-eval', say so plainly rather than shipping a CSP that silently breaks the UI.

5. AUDIT LOGGING through the existing AuditRepo, with IP and user agent: every login attempt, code
   request, passkey ceremony, step-up, logout, rate-limit trip and session revocation.

6. LOGIN-PAGE HARDENING: exponential backoff on repeated failures from the same IP; a global cap on
   in-flight codes.

TESTS for each. make check green.
```

---

## Prompt 7 — Deployment: reverse proxy, docs, and a real end-to-end check

```
Wire up deployment and prove the whole thing actually works. Read CLAUDE.md — deploys are pull-based on
the server (CI publishes the image; the server runs `make prod-pull && make prod-up`), and each fix
costs a redeploy round-trip, so reproduce locally FIRST.

1. REVERSE PROXY: a Caddyfile (Caddy gets Let's Encrypt right with the least config) for the real
   domain, terminating TLS and proxying to jarvis:8080, setting X-Forwarded-For / -Proto / -Host
   correctly. uvicorn must trust ONLY Caddy's IP (--forwarded-allow-ips). Show both sides together —
   this pair is where per-IP rate limiting silently dies if it's wrong.

2. COMPOSE (docker-compose.prod.yml): check whether JARVIS_SECRETS_KEY is passed as a plain env var. My
   notes say `docker inspect` exposes container Env, so a Fernet key passed that way is readable through
   the read-only Docker proxy. If it is, move it to a file/Docker secret and flag it in the PR — that
   key now protects the auth tables too.

3. DOCS (README or docs/auth.md):
   - First-run bootstrap: how the first allow-listed email gets in and enrolls a passkey.
   - How to add/remove someone from the allow-list — removal must revoke their sessions.
   - rp_id vs expected_origin, the local-dev values, and the fact that passkeys DO NOT work over a bare
     LAN IP (the spec prohibits IP-address rp_ids) — only the real domain or localhost. Credentials
     registered against rp_id=localhost must be re-registered in production.
   - MAILTRAP SETUP, with both silent failure modes called out so loudly they cannot be missed:
       (a) SANDBOX vs SENDING. live.smtp.mailtrap.io / the send API delivers real mail;
           sandbox.smtp.mailtrap.io CAPTURES it and delivers NOTHING while still returning success.
           Production pointed at the sandbox = every login code silently vanishes.
       (b) The sending DOMAIN MUST BE VERIFIED (4 CNAME + 1 TXT/DMARC; 15 min–24 h to propagate). An
           unverified demo domain can only mail the registration address — which works for a one-person
           allow-list and then breaks silently when a second person is added.
   - The recovery-code flow, and the plain statement that BECAUSE AN EMAILED CODE IS THE RECOVERY PATH,
     THE SECURITY FLOOR OF THIS SYSTEM IS THE RECIPIENT'S EMAIL INBOX. Anyone on the allow-list should
     have a passkey / hardware 2FA on their email account and should print the one-time recovery codes
     and store them offline — those are the only escape hatch that does not route through email.

4. END-TO-END VERIFICATION — not just the unit tests. Bring the stack up locally and drive the real
   flows: emailed code → session → forced passkey enrollment → logout → passkey login → step-up on /mcp
   → session revocation killing a live SSE stream. Use the /verify skill. Tell me what you actually
   observed, and if something doesn't work, say so plainly rather than reporting green.

5. Confirm the pre-existing non-browser callers still work: /healthz reachable unauthenticated (Docker
   healthcheck), and POST /events/webhook still authenticating on its Bearer token and still 404ing when
   no token is configured.
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
