# Event-driven invocation: authenticated webhook → coalescer → dispatcher

**Date:** 2026-07-09
**Status:** approved (autonomous /goal run)

## Objective

Jarvis wakes an agent turn on real-world events (new mail, calendar change, generic
webhook), not just on a cron schedule. A second class of `InvocationRequest` producer
feeds the existing `TriggerDispatcher`, with coalescing to avoid wake-thrash.

## What already exists

Groundwork from PRs #55/#56 means most of the trust plumbing is done:

- `EventTrigger` in `jarvis/core/types.py` (trusted `prompt` vs untrusted `content`),
  discriminated into the `Trigger` union; `InvocationRequest.trigger_source` derives
  `TriggerSource.EVENT` from it.
- `TriggerDispatcher.dispatch_event()` with bounded-LRU dedup on `external_id`.
- `AgentRunner` provenance-tags event content as untrusted (`_event_context`) and runs
  event turns under the reduced tool scope (`run_scope.py` contextvar → MCP approval
  policy). Verified end-to-end in `tests/integration/test_event_trigger_tool_scope.py`.
- `OutputRouter` + `NotificationGate` already route event-turn output to the first
  allow-listed Discord user under the daily notification budget.

What is missing is the **producer**: nothing in the app ever calls `dispatch_event`.

## Decision: watcher placement (the critical constraint)

**Chosen: an external webhook receiver — a FastAPI route in `jarvis/web/routes/`, plus
an in-process coalescer whose only job is to enqueue into the dispatcher.**

Alternatives considered:

1. **Authenticated webhook route (chosen).** Source-agnostic (mail forwarders, calendar
   push, home automation, `curl`), no new dependencies, no polling, trivially testable
   via the ASGI test client. Anything that can POST JSON can wake Jarvis.
2. **IMAP IDLE watcher task.** No inbound exposure needed, but adds an IMAP dependency,
   credential management, reconnect/flakiness handling — and it is mail-only. Can be
   added later as another producer feeding the same coalescer.
3. **Gmail Pub/Sub.** Requires GCP project setup and *still* needs an authenticated
   push receiver — i.e. it is a specialization of option 1, not an alternative.

**MCP single-owner-task invariant — why this design provably cannot violate it:**
the webhook handler and the coalescer's flush tasks never import or call
`MCPManager`. The only thing a flush task does is `await dispatcher.dispatch_event()`,
which runs the agent exactly the way Discord-handler tasks already do: the runner
*uses* already-connected SDK servers via `mcp_servers_provider`; it never performs
connect/replace/close, so no anyio cancel scope is entered or exited off the manager's
lifecycle task. The event path is a pure dispatcher client, same as every existing
trigger path.

## Components

### 1. `EventsConfig` (`jarvis/config/schema.py`, under `jarvis.yaml`)

```yaml
events:
  webhook_token: ${JARVIS_EVENTS_WEBHOOK_TOKEN}   # omit/empty → endpoint disabled (404)
  coalesce_window_sec: 30.0                        # >= 0
```

Defaults keep existing configs valid: token `None` (feature off), window 30s.

### 2. Dispatcher: expose the dedup LRU (`jarvis/core/dispatcher.py`)

New public `remember_if_new(external_id) -> bool` (False if already seen); the existing
channel-message and event dedup paths refactor onto it. The coalescer calls it **at
intake**, so a redelivered webhook (same `external_id`) is dropped before it can extend
or re-open a coalescing window — reusing the same bounded LRU rather than growing a
second dedup structure.

### 3. `EventCoalescer` (`jarvis/core/coalescer.py`)

- `submit(*, source, external_id, prompt, content, coalesce_key=None) -> "queued" | "duplicate"`.
  Synchronous and non-blocking (must be called on the event loop): dedups via the
  dispatcher LRU, appends to a per-key buffer (`key = source` or
  `source + ":" + coalesce_key`), and spawns one flush task per open window.
- Flush task: `sleep(window)`, then atomically (no awaits in between) pops the buffer
  and its task entry, then dispatches **one** merged `EventTrigger`:
  - `external_id = "turn:<first_id>+<n>"` — distinct from the per-event ids already in
    the LRU, so the merged dispatch is not self-suppressed, while an identical replay
    of the same burst still dedups.
  - `content` = the single payload, or numbered `Event i/n (id …)` sections.
  - `prompt` = first non-empty submitted prompt, else a standing default instruction.
- `shutdown()`: cancel pending flush tasks (undelivered events are dropped at shutdown,
  matching how channel adapters stop before teardown).
- The HTTP response never waits on the agent turn: webhook senders get a fast `202`;
  the turn runs on the flush task under the dispatcher's existing concurrency semaphore.

### 4. Webhook route (`jarvis/web/routes/webhooks.py`)

`POST /events/webhook`, strict pydantic body:

```json
{"source": "email", "external_id": "msg-123", "content": "...",
 "prompt": "optional standing instruction", "coalesce_key": "optional thread id"}
```

- No token configured → `404` (feature off, endpoint hidden).
- `Authorization: Bearer <token>` compared with `secrets.compare_digest` → else `401`.
- Returns `202 {"status": "queued"|"duplicate"}`.
- The existing `SameOriginUnsafeMethodMiddleware` passes non-browser POSTs (no
  Origin/Referer) untouched, so curl/services work; browser cross-site posts stay blocked.

Security notes: the bearer token means only operator-configured senders can submit, so
the `prompt` field is operator-trusted (same trust level as a schedule's prompt). The
`content` field remains untrusted regardless — it flows into `EventTrigger.content`,
which the runner provenance-tags and which runs under the reduced event tool scope.

### 5. Wiring (`jarvis/main.py`)

Build `EventCoalescer(dispatcher=…, window_sec=cfg.jarvis.events.coalesce_window_sec)`
after the dispatcher; new `AppContext.event_coalescer` field; `shutdown()` cancels it
right after channel adapters stop (no new triggers, then no pending windows).

## Error handling

- Flush-task dispatch failures are logged (`_log.exception`) — one bad turn never kills
  the coalescer; the window's task entry is already cleared, so later events reopen fresh.
- Invalid payloads → FastAPI 422; oversized `content` capped by schema (100k chars).
- `window_sec = 0` flushes on the next loop tick (still one dispatch per burst already
  buffered) — used by tests.

## Testing

- `tests/unit/test_event_coalescer.py` (stub dispatcher): burst → one dispatch with
  merged content; distinct sources/keys → separate dispatches; redelivered
  `external_id` → `"duplicate"`, no second window; flush honors the window; shutdown
  cancels cleanly.
- `tests/integration/test_web_events_webhook.py` (real `create_app` + real
  `TriggerDispatcher`/`AgentRunner` with a fake model): 404 when unconfigured; 401 on
  bad/missing token; 202 + agent run within the window; duplicate POST → `"duplicate"`
  and one run; burst of 3 → exactly one run; the run's trigger is an `EventTrigger`
  (⇒ `trigger_source == EVENT`, whose scope enforcement is already covered by
  `test_event_trigger_tool_scope.py`).
- `tests/integration/test_mcp_manager_lifecycle.py` must stay green — the event path
  adds no MCP calls, and the full suite (`make check`) verifies it.
