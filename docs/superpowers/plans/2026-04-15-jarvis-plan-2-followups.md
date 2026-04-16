# Plan 2 Follow-ups (Tracked Debt)

Items surfaced by the end-of-Plan-2 code review that were deliberately deferred rather than fixed inline. Each item has a clear home in a subsequent plan.

## Address before Plan 3 starts

These two items will bite Plan 3's Discord adapter directly if not handled first:

### `MCPToolRepo.replace_for_server` wipes `policy_override` on every reconnect

`jarvis/persistence/repositories.py` — `replace_for_server` deletes existing tool rows and re-inserts with `policy_override=None`. Today no one writes that column, so no user-visible bug. But once Plan 3 wires up a confirmation flow, every MCPManager reconnect (or even bootstrap) silently wipes user choices.

**Fix:** before the delete, read existing `{name → policy_override}` for the server. After re-insert, re-apply the override map for tools that still exist.

### `ChannelAdapter` protocol is not yet defined; `allowed_refs` resolution is unspecified

Spec §5.4 promises `ChannelAdapter` in `jarvis/channels/base.py`. Plan 2 didn't add it because Plan 3 owns Discord. But `TriggerDispatcher.dispatch_channel_message(msg, allowed_refs=...)` requires the caller to pass the allow-list — meaning Plan 3's `DiscordAdapter` will need to know about config.

**Decide before Plan 3:** either move the allow-list lookup into the dispatcher (give it a config reference), or document the expectation that adapters resolve their own allow-list from config and pass it in.

## Plan 3+ debt

### `idle_timeout_sec` column is dead weight

`ConversationRow.idle_timeout_sec` exists but is never read or written. Plan 1 followups promised "wire it in Plan 2" — only the call-site parameter (`find_or_create_open(idle_timeout_sec=...)`) was wired. Plan 2 followup promised `ConversationRepo.get_idle_timeout` as a reader; not implemented.

**Pick one:** drop the column (YAGNI), or implement per-conversation overrides as `conversation.idle_timeout_sec or config.idle_timeout_sec`.

### `AuditEventType.TOOL_RESULT` is defined but never emitted

The tracer maps `FunctionSpanData → TOOL_CALL`. The SDK emits a single span per tool call carrying both the args and the result, so we'd need to either (a) split into two events with `TOOL_CALL` for the call and `TOOL_RESULT` for the output, or (b) drop the `TOOL_RESULT` enum value.

### MCPManager has no background reconnect loop

Spec §5.5 says: "Background reconnect loop with exponential backoff." Plan 2 implements only start-time connect — servers that fail at start stay failed; servers that die mid-run are not resurrected. Add an async background task in `MCPManager.start` that retries failed servers with exponential backoff (capped at 60s).

### `ConfigWatcher` is implemented but never wired into `bootstrap`

Plan 1's `ConfigWatcher` is fully tested and ready, but `bootstrap()` doesn't start it. Plan 4 (or the dashboard plan) should wire it: when MCP server list or LLM endpoint changes, restart MCPManager / install a new LLM client.

### CLI `invoke` doesn't emit `channel.sent`

Spec §5.10 requires every output path to record `channel.sent` (or `output.suppressed`). The CLI currently `typer.echo`s the result without an audit event. Plan 5 (dashboard "Run Now") will likely need this same path; consolidate.

### Improve `_extract_text` in `AgentRunner`

`agents/runner.py:_extract_text` returns `""` if `result.final_output` is None. For tool-calling agents whose run ends in a tool result rather than text, users see an empty reply. At minimum log a warning; ideally surface the last tool result's text or a "no text reply" placeholder.

## Code-organization notes

### Boundary smell: `persistence/` imports from `mcp/`

`repositories.py` imports `MCPToolDescriptor` from `jarvis/mcp/descriptor.py`. The persistence layer is conceptually a leaf, so depending on a sibling feature module is mildly off. Consider moving `MCPToolDescriptor` into `jarvis/core/types.py` (alongside other shared Pydantic models) or into a dedicated `jarvis/core/contracts.py`.

### Async lifecycle inconsistency

Across the four long-running components — `AuditLogger`, `MCPManager`, `JarvisTraceProcessor`, `ConfigWatcher` — start/stop semantics differ:

- `AuditLogger.start` raises on double-start; supports stop-then-start.
- `MCPManager.start` has no idempotency guard; double-start would corrupt `_sdk_servers`.
- `JarvisTraceProcessor` has no start/stop — it's a passive object held by reference.
- `ConfigWatcher.start` raises on double-start (matches AuditLogger).

Either standardize on a shared protocol/base class, or document the expected contract per component.

### `AgentRunResult.conversation_id: Any  # UUID`

Type-annotated as `Any` for simplicity. Tighten to `UUID` for IDE/type-checker benefit.

## Resolved during end-of-Plan-2 review

- **SDK global state cross-test leak** — fixed in `155cf05` via `tests/conftest.py` autouse fixture that resets `set_trace_processors([])` after every test.
