# Jarvis Roadmap — Beyond a "Morning Brief Bot"

> Ten goals to evolve Jarvis from a reactive, cron/user-driven agent into a proactive,
> event-driven personal assistant — with the guardrails that make proactivity safe.
> Derived from a deep-research pass (6 angles, 27 sources, 25 claims adversarially
> verified 3-vote → 23 confirmed) cross-referenced against this codebase.

## How to use this file

Each goal below is a **self-contained brief** designed to be pasted into a fresh Claude Code
session. A goal block gives you: objective, why it matters, the concrete work, the files most
likely involved, a done-when checklist, a rough turn estimate, dependencies, and gotchas.

- **Paste one goal at a time.** They are sized to be a focused session each.
- **Respect the phase order.** Phase 1 (guardrails) must land before Phase 2 (proactivity) —
  opening inbound event channels before the guardrails exist is a live security risk, not a
  style preference.
- **"Estimated turns"** is a rough sizing for a focused session (design + implement + tests),
  assuming the described files are roughly where expected. Treat as ballpark, not a budget.
- Always run `make check` (lint + tests) before considering a goal done. Migration changes need
  a test under `tests/integration/` that runs real alembic.

## Codebase anchor points (verified July 2026)

| Area | Path |
|------|------|
| Dispatcher (sole `InvocationRequest` producer; allow-list, dedup, concurrency) | `jarvis/core/dispatcher.py` |
| Output routing | `jarvis/core/output_router.py`, `jarvis/scheduler/scheduled_output.py` |
| Tool permission policy | `jarvis/mcp/tool_policy.py`, `jarvis/mcp/approval_policy.py` |
| MCP lifecycle (single-owner-task invariant) | `jarvis/mcp/manager.py` |
| Agent runner / model selection | `jarvis/agents/runner.py`, `jarvis/agents/factory.py` |
| Discord adapter | `jarvis/channels/` |
| Scheduler + oauth jobs | `jarvis/scheduler/scheduler.py`, `jarvis/scheduler/oauth_jobs.py` |
| Digest seed templates (incl. Daily Brief) | `jarvis/digests/seeds.py` |
| OAuth (flow, store, catalog, crypto, discovery) | `jarvis/oauth/` |
| Vector memory | `jarvis/memory/vector_store.py`, `embeddings.py`, `service.py`, `summarizer.py` |
| Dashboard routes (incl. existing `events.py` SSE, `actions.py`) | `jarvis/web/routes/` |
| stdio MCP server config | `config/mcp-servers.yaml` |
| Persistence (repos are the only way to touch the DB) | `jarvis/persistence/` |

**Non-negotiable conventions** (from `CLAUDE.md`): persistence goes through repositories; datetimes
use `TZDateTime` (aware UTC); the **MCP single-owner-task invariant** (all connect/replace/close/refresh
funnelled through one lifecycle task in `mcp/manager.py`); migrations write UUIDs as `uuid4().hex`;
secrets Fernet-encrypted with `JARVIS_SECRETS_KEY`; branch off `main` and open a PR.

---

# Phase 1 — Foundation: make autonomy safe before adding it

*All three are refinements to systems that already exist. No new architecture. They gate Phase 2.*

## Goal 1 — Trigger-source-aware tool policy & injection hardening  *(Rec #2)*

- **Phase:** 1 · **Depends on:** nothing · **Estimated turns:** 4–6

### Objective
Untrusted inbound content (email bodies, calendar invites) can never drive a high-consequence
tool call. Establish a `trigger_source` distinction so event/schedule-triggered turns run with a
restricted tool scope, and tag untrusted text in the LLM context as non-authoritative.

### Why
Opening inbound channels in Phase 2 exposes a demonstrated **zero-click indirect prompt-injection**
surface — a crafted Google Calendar invite or email can instruct the agent to exfiltrate data or
send messages, with no user acceptance needed ("Invitation Is All You Need" vs Gemini; EchoLeak vs
M365 Copilot; OWASP LLM01). This goal is the precondition that makes Goal 4 safe.

### Key work
- Thread a `trigger_source` enum (`user` | `scheduled` | `event`) from the dispatcher/runner into
  the tool-policy decision path.
- In `jarvis/mcp/tool_policy.py` (and/or `approval_policy.py`), add a rule: `event`- and
  `scheduled`-triggered turns default to **read-only** tool scope — no compose/send/delete/external
  side effects — regardless of the per-tool heuristic.
- Provenance-tag untrusted content injected into the prompt (email/invite bodies) so it is clearly
  delimited as data, not instructions (e.g. wrapped/marked in `jarvis/agents/runner.py` context
  assembly). The user's own message remains the only authoritative instruction source.

### Likely files
`jarvis/mcp/tool_policy.py`, `jarvis/mcp/approval_policy.py`, `jarvis/core/dispatcher.py`,
`jarvis/core/types.py`, `jarvis/agents/runner.py`, tests under `tests/unit/` + `tests/integration/`.

### Done when
- [ ] `InvocationRequest` carries a trigger source and it reaches the policy layer.
- [ ] An integration test feeds a hostile calendar-invite/email body into an `event`-triggered turn
      and asserts a send/delete/external tool is **denied**.
- [ ] User-initiated turns retain today's behavior (no regression).
- [ ] `make check` green.

### Gotchas
- Don't weaken policy for `user` turns — this is purely about adding restriction for non-user triggers.
- Keep the tag/delimiter format simple and model-legible; don't rely on it alone — pair with scope reduction.

---

## Goal 2 — Risk-contingent Action Inbox  *(Rec #3)*

- **Phase:** 1 · **Depends on:** Goal 1 (shares the policy layer) · **Estimated turns:** 4–6

### Objective
Shift the approval default from "confirm unless read-only" to "auto-allow reversible / low-blast-radius,
gate only irreversible or sensitive." Cut approval volume ~10× while raising trust.

### Why
Verified (arXiv:2510.04465, N=450): an agent that acts autonomously but escalates only on detected
sensitivity beat both always-confirm and full-autonomy — higher perceived control, better privacy-leak
detection (68% vs 58%). High-volume confirm-gates "collapse into rubber-stamping" (approval fatigue /
automation-complacency literature). Fewer, better-targeted gates = more real oversight.

### Key work
- Introduce a blast-radius / reversibility classification for tool calls (reversible + low-scope →
  auto-allow; irreversible, destructive, or sensitive-recipient → gate) in the policy layer.
- Optionally derive a "sensitivity" signal from `jarvis/memory/` (known-sensitive contacts/topics/
  preferences) so escalation is context-aware, not just a static tool list.
- Ensure every auto-allowed action is still written to the audit log (`jarvis/web/routes/audit.py` /
  the audit event stream) — reducing gates must not reduce visibility.

### Likely files
`jarvis/mcp/tool_policy.py`, `jarvis/mcp/approval_policy.py`, `jarvis/web/routes/actions.py`,
`jarvis/memory/service.py` (sensitivity signal), audit plumbing, tests.

### Done when
- [ ] In a representative session, gated actions drop to only irreversible/sensitive ones.
- [ ] Every auto-allowed action still appears in the audit log.
- [ ] Unit tests cover the classification (a reversible read auto-allows; a destructive/irreversible
      call still gates).
- [ ] `make check` green.

### Gotchas
- Reversibility ≠ read-only. A draft-create is reversible; a send is not. Classify by *effect*, not verb.
- Don't hardcode a brittle allowlist — express the rule in terms of blast radius + reversibility so new
  tools inherit sane defaults.

---

## Goal 3 — Notification budget & priority tiers  *(Rec #5)*

- **Phase:** 1 · **Depends on:** nothing (but pairs with Goals 4/8) · **Estimated turns:** 3–5

### Objective
Never exceed ~3–5 unsolicited messages/day. Low-priority events coalesce into the existing Daily Brief
digest instead of firing standalone pings.

### Why
Verified (extracted, central): users tolerate only ~3–5 unsolicited AI notifications/day across all
sources before they mute and uninstall. Effective systems map priority tiers → delivery channels
(P1 interrupt-now … P4 digest-only). This is the discipline that keeps Goal 4's proactivity from
backfiring into notification spam.

### Key work
- Add a priority classifier + rolling daily rate-limiter to `jarvis/core/output_router.py`.
- Define tiers (P1 interrupt now → P4 digest-only) and a per-day budget.
- Sub-threshold events accumulate and roll into the next scheduled digest run rather than sending
  immediately.

### Likely files
`jarvis/core/output_router.py`, `jarvis/scheduler/scheduled_output.py`, `jarvis/digests/seeds.py`
(digest assembly), persistence for the rolling counter (via a repository), tests.

### Done when
- [ ] A synthetic burst of ~20 low-priority events in an hour yields **one** digest entry, not 20 pings.
- [ ] A P1 event still delivers immediately.
- [ ] The daily budget resets correctly across a day boundary (mind `TZDateTime` / aware UTC).
- [ ] `make check` green.

### Gotchas
- Persist the rolling count through a repository, not in-memory — it must survive a process restart.
- Watch the day-boundary math: all datetimes are aware UTC via `TZDateTime`.

---

# Phase 2 — The shift: event-driven proactivity

*Now safe to build. This is the paradigm change: Jarvis stops being purely cron/user-driven.*

## Goal 4 — Inbound event watcher feeding the dispatcher  *(Rec #1)*

- **Phase:** 2 · **Depends on:** Goals 1, 3 (hard) · **Estimated turns:** 8–12 (do a design spike first)

### Objective
Jarvis wakes an agent turn on real-world events (new mail, calendar change, or a generic webhook),
not just on a cron schedule. A second class of `InvocationRequest` producer feeds the existing
dispatcher, with coalescing to avoid wake-thrash.

### Why
The headline gap. Verified pattern (agenticmail, OpenClaw): an inbound watcher (IMAP IDLE → SSE,
Gmail Pub/Sub, or authenticated webhook) spawns a one-shot model turn on the event, with a short
per-source coalescing window (~30s). Unlocks "notify me when X happens," email triage, and same-day
calendar reactions.

### Key work
- Add an inbound event source as a **new producer** into `jarvis/core/dispatcher.py`, reusing its
  existing concurrency + dedup gate. Start with **one** source (recommend: an authenticated webhook
  receiver route, or IMAP IDLE / Gmail Pub/Sub for mail).
- Tag emitted requests with `trigger_source = event` so Goal 1's reduced scope applies.
- Add a per-source/per-thread **coalescing window** (~30s) so a burst produces one turn, not N.
- **Architecture constraint (critical):** the watcher must live *outside* the MCP single-owner-task
  lifecycle. Implement it as an external webhook receiver (a `jarvis/web/routes/` endpoint —
  note `events.py` already exists) or a dedicated asyncio task that only **enqueues** to the
  dispatcher. It must never enter/exit MCP anyio cancel scopes off the lifecycle task — doing so
  corrupts anyio state and tears down the event loop (see `tests/integration/test_mcp_manager_lifecycle.py`).

### Likely files
`jarvis/core/dispatcher.py`, `jarvis/core/types.py`, a new watcher module + a
`jarvis/web/routes/` endpoint (or extend `events.py`), `jarvis/channels/` if reusing adapter plumbing,
integration tests.

### Done when
- [ ] A new email (or webhook POST) triggers an agent turn within seconds.
- [ ] Event-triggered turns run under Goal 1's reduced tool scope.
- [ ] A burst of events coalesces into a single turn (test the window).
- [ ] `tests/integration/test_mcp_manager_lifecycle.py` stays green under event load — the MCP
      invariant is provably untouched.
- [ ] Inbound webhook endpoint is authenticated (Bearer or equivalent).
- [ ] `make check` green.

### Gotchas
- **Do the design spike before coding.** The single-process async + MCP single-owner-task constraints
  are the real risk here — decide watcher placement (in-process asyncio subscriber vs external webhook
  receiver) explicitly and write it down.
- Authenticate the inbound endpoint — an open webhook is a trivial abuse/injection vector.
- Dedup: reuse the dispatcher's bounded-LRU so a redelivered webhook doesn't double-fire.

---

## Goal 5 — Presence & action-trace signals  *(Rec #4)*

- **Phase:** 2 · **Depends on:** Goal 4 · **Estimated turns:** 2–4

### Objective
No silent autonomy. When Jarvis acts without a user prompt, it emits a concise "did X because Y"
trace to Discord and the audit stream.

### Why
Verified (arXiv:2502.18658, N=18): proactivity raised efficiency but caused disruption and
"diminished users' awareness of what the AI was doing"; presence indicators + interaction context
alleviated it. Silent agents erode trust even when correct.

### Key work
- For any autonomous (non-user-initiated) action, emit a short rationale trace via the
  `OutputRouter` (Discord) and the existing audit SSE feed (`jarvis/web/routes/events.py` /
  `audit.py`).
- Keep it terse and subject to Goal 3's budget (traces shouldn't blow the notification ceiling —
  route routine traces to the audit feed / digest, reserve Discord for the noteworthy).

### Likely files
`jarvis/core/output_router.py`, `jarvis/web/routes/events.py`, `jarvis/web/routes/audit.py`,
`jarvis/agents/runner.py`.

### Done when
- [ ] Every event-triggered autonomous action produces a visible after-the-fact trace.
- [ ] Traces respect the Goal 3 notification budget (routine ones don't ping Discord).
- [ ] `make check` green.

### Gotchas
- Reuse the audit stream that already exists rather than inventing a new channel.

---

# Phase 3 — Quick capability wins (parallelizable, mostly config)

*Each is days of work, largely dropping MCP servers into the existing layer. Order among them is free.*

## Goal 6 — Google Tasks integration for reminders & to-dos  *(Rec #6)*

- **Phase:** 3 · **Depends on:** Goal 2 (write actions gate through the new policy) · **Estimated turns:** 3–5

### Objective
Native task capture and reminders against **Google Tasks** (the user's actual task backend), via an
MCP server wired through Jarvis's existing Google OAuth flow.

### Why
Your survey flagged no native reminders/tasks. Google Tasks MCP servers use the Google Tasks API over
OAuth — the **same pattern already running for Gmail and Calendar** — so this reuses `jarvis/oauth/`
provider/connection infrastructure rather than new plumbing.

### Key work
- Evaluate a community Google Tasks MCP server (no first-party one exists):
  [`zcaceres/gtasks-mcp`](https://github.com/zcaceres/gtasks-mcp),
  [`arpitbatra123/mcp-googletasks`](https://github.com/arpitbatra123/mcp-googletasks),
  [`ktmage/mcp-google-tasks`](https://github.com/ktmage/mcp-google-tasks).
- **Vet auth handling before trusting it with a Google credential.** Several write OAuth tokens to
  disk with their own scheme — that conflicts with Jarvis's convention of Fernet-encrypted secrets in
  the DB. Prefer the one whose auth model fits `jarvis/oauth/crypto.py` + `store.py`, or thin-wrap/fork
  it so tokens live in Jarvis's encrypted store.
- Add a `tasks` scope; register the server as a stdio server (`config/mcp-servers.yaml`) or an HTTP/
  OAuth connection via the `/mcp` dashboard, following the Gmail/Calendar precedent.

### Likely files
`config/mcp-servers.yaml` (if stdio) or `jarvis/oauth/catalog.py` + a migration seeding a provider
(if wired as a managed connection like Gmail), plus the Daily Brief prompt if surfacing tasks there.

### Done when
- [ ] "Remind me to X" creates a real Google Tasks entry; "what's on my task list" reads it back.
- [ ] Write actions (create/complete/delete) gate per Goal 2's policy.
- [ ] Credentials live in Jarvis's encrypted store, not a plaintext token file.
- [ ] `make check` green.

### Gotchas
- **Not first-party** (Google publishes no official Tasks MCP) — vet maintenance + auth before trusting.
- If seeding a built-in provider like Gmail/Calendar, the migration must use `uuid4().hex` and there
  must be a `test_migration_*` that runs real alembic + a `test_migration_seed_matches_catalog`-style
  check (see the Fastmail/Gmail/Calendar precedent).

---

## Goal 7 — Morning-brief data enrichment  *(Rec #7)*

- **Phase:** 3 · **Depends on:** nothing · **Estimated turns:** 2–3

### Objective
The Daily Brief carries real weather (and optionally markets), not just whatever mail/calendar tools
happen to return.

### Why
Today the "Daily Brief" is just a prompt over connected tools — no weather/news/markets. Verified
free, no-API-key MCP servers drop straight into the existing MCP layer: `open-meteo-mcp` (17 weather
tools, no key), `stock-scanner-mcp` (~11 modules key-free).

### Key work
- Register [`open-meteo-mcp`](https://github.com/cmer81/open-meteo-mcp) (weather, no key) — and
  optionally [`stock-scanner-mcp`](https://github.com/yyordanov-tradu/stock-scanner-mcp) — as stdio
  MCP servers in `config/mcp-servers.yaml`.
- Update the Daily Brief prompt in `jarvis/digests/seeds.py` to pull a local forecast (+ markets).
- Verify current tool inventories — counts drift between releases; "real-time" free market data is
  often delayed.

### Likely files
`config/mcp-servers.yaml`, `jarvis/digests/seeds.py`.

### Done when
- [ ] Tomorrow's brief includes a real local forecast sourced from the new server.
- [ ] (If enabled) markets data appears.
- [ ] `make check` green.

### Gotchas
- Weather needs a location — parameterize it (config/preference), don't hardcode.
- Confirm the servers run under the container's runtime (Node/Python availability in the image).

---

## Goal 8 — Self-hosted push via ntfy  *(Rec #8)*

- **Phase:** 3 · **Depends on:** Goal 3 (tiers) · **Estimated turns:** 2–4

### Objective
Reach the user's phone/desktop when they're not in Discord, via a self-hosted ntfy instance.

### Why
Interface is Discord-DM-only today. Verified: `ntfy` delivers to phone/desktop/web via a simple HTTP
POST to a self-hosted topic — no third-party cloud push. Pairs with Goal 3's tiers (P1 → push,
P4 → digest).

### Key work
- Add an **ntfy output target** to `jarvis/core/output_router.py` (HTTP POST to a configured topic URL).
- Wire delivery to Goal 3's priority tiers so only P1/urgent events push.
- Config for the ntfy base URL + topic + any auth token (Fernet-encrypted if secret).

### Likely files
`jarvis/core/output_router.py`, config loader / settings, tests.

### Done when
- [ ] A P1 event lands as a push notification on the phone via the self-hosted ntfy instance.
- [ ] Low-priority events do **not** push (they digest).
- [ ] `make check` green.

### Gotchas
- Store any ntfy auth token via the Fernet-encrypted secret path, not plaintext config.
- Respect the notification budget — push is the most intrusive channel; reserve it for the top tier.

---

# Phase 4 — The moat, then polish

## Goal 9 — RAG over your own documents (a real "second brain")  *(Rec #9)*

- **Phase:** 4 · **Depends on:** nothing (independent) · **Estimated turns:** 8–12

### Objective
Jarvis answers over the user's *own* content — notes, PDFs, attachments — not just chat history and
approved preferences. The biggest long-term differentiator.

### Why
Verified: the defining feature of leading peers (Khoj) is answering "from the internet **and your
docs**." Jarvis's `sqlite-vec` memory today holds only run-summaries + preferences, not user content.
(Cite the *capability* — the specific vector-store/voice internals attributed to Khoj were refuted in
research, so design your own retrieval layer; don't copy an assumed stack.)

### Key work
- Extend the existing embedding pipeline (`jarvis/memory/embeddings.py`, `vector_store.py`) to ingest
  a document corpus (a folder, Fastmail attachments, or Drive).
- Add an ingestion/indexing path (chunking + embedding + storage) and a **retrieval tool** the agent
  can call during a run.
- Handle incremental re-indexing (source-hash idempotency, like the existing recall-summary dedup).

### Likely files
`jarvis/memory/vector_store.py`, `jarvis/memory/embeddings.py`, `jarvis/memory/service.py`, a new
ingestion module, a new MCP/agent tool for retrieval, persistence (migration for a documents table —
`uuid4().hex`, aware UTC, repository access), integration tests incl. real alembic.

### Done when
- [ ] A question answerable only from an added document is answered with the right passage retrieved.
- [ ] Re-ingesting an unchanged source is idempotent (no duplicate chunks).
- [ ] Graceful degradation if sqlite-vec is unavailable (match existing memory behavior).
- [ ] `make check` green; migration test runs real alembic.

### Gotchas
- Reuse the existing `sqlite-vec` + source-hash idempotency patterns rather than a parallel system.
- Chunking/token budgets: keep retrieved context within the runner's history bounds
  (`_HISTORY_MAX_CHARS` etc. in `jarvis/agents/runner.py`).

---

## Goal 10 — Streaming responses on Discord  *(Rec #10)*

- **Phase:** 4 · **Depends on:** nothing · **Estimated turns:** 3–5

### Objective
Replace the single final message with live streaming output for a responsive feel.

### Why
The runner currently sends one final message (no token streaming to Discord). Contained UX win:
stream by live-editing a draft message + typing indicator while tools run.

### Key work
- In `jarvis/channels/` (Discord adapter) + `jarvis/agents/runner.py`, stream partial output by
  editing a draft message and showing a typing indicator during tool calls.
- Handle known failure modes: stuck typing indicator, Discord edit rate-limits (debounce/throttle edits).

### Likely files
`jarvis/channels/` (Discord adapter), `jarvis/agents/runner.py`, `jarvis/core/output_router.py`.

### Done when
- [ ] A multi-step run visibly streams/updates in Discord instead of going silent then dumping a block.
- [ ] Edit rate-limits are respected (no 429 spam); typing indicator always clears.
- [ ] `make check` green.

### Gotchas
- Debounce edits — Discord will rate-limit aggressive message edits.
- **Deliberately excluded: voice/STT.** The surveyed projects' voice stacks were refuted/unverified in
  research; don't add voice until there's a concrete self-hostable path worth committing to.

---

## Sequencing & dependency summary

```
Phase 1 (guardrails)      Goal 1 ─┐
                          Goal 2 ─┤─── gate ───► Phase 2
                          Goal 3 ─┘
Phase 2 (proactivity)     Goal 4 (design spike first) ──► Goal 5
Phase 3 (quick wins)      Goal 6 · Goal 7 · Goal 8   (independent; 7 & 8 can start anytime)
Phase 4 (moat + polish)   Goal 9 (independent) · Goal 10 (independent)
```

- **Hard gate:** Do not start Goal 4 until Goals 1 and 3 are done.
- **Design spike:** Goal 4 deserves an explicit architecture decision (watcher placement vs the MCP
  single-owner-task invariant) written down before implementation.
- Goals 7, 8, 9, 10 are independent and can be picked up opportunistically.

## Research provenance & caveats

- Sourced from a deep-research pass: 6 angles, 27 sources fetched, 25 claims verified by 3-vote
  adversarial check → 23 confirmed, 2 refuted.
- **Refuted (do not rely on):** OpenClaw cross-platform voice wake-word; Khoj's specific
  Qdrant/FAISS + Whisper internals. Cite Khoj's document-RAG *capability*, not its stack.
- **Time-sensitive:** MCP tool counts drift; free "real-time" market data is often delayed; the
  Todoist MCP moved repos (irrelevant here — we use Google Tasks). Re-verify any external server
  before wiring it.
- **Weakest evidence:** the two autonomy/proactivity HCI studies are preprints with self-report
  measures and modest N (18, 450); principles generalize but aren't proven for a general personal
  agent. Voice as a channel had weak/refuted support — hence its exclusion.
- None of this was validated against Jarvis's live event-loop / MCP constraints — Goal 4's integration
  is the real open design question.
