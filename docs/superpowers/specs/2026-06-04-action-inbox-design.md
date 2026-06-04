# Action Inbox — Design

**Date:** 2026-06-04
**Status:** Draft for user review

## Goal

Add a durable Action Inbox so Jarvis can safely pause side-effecting MCP tool
calls, ask the operator for approval, and resume the original agent run after
the decision.

The v1 scope is intentionally broad for safety and narrow for product surface:

- Apply approvals across Discord messages, scheduled runs, and dashboard manual
  runs.
- Let read/search/list/fetch MCP tools keep running automatically.
- Pause non-read MCP tools in a dashboard Action Inbox before execution.
- Support approve and reject decisions, then resume the interrupted run and
  route the final output through the existing output path.

## Background

Jarvis already has most of the required foundation:

- `MCPManager` owns long-lived `MCPServerStdio`, `MCPServerStreamableHttp`, and
  `MCPServerSse` objects and passes them to `Agent(mcp_servers=...)`.
- `MCPToolRow.policy_override` is editable from `/mcp`.
- `jarvis/mcp/tool_policy.py` classifies tools as `auto` or `confirm` from
  user overrides, MCP annotations, and read-like tool name prefixes.
- `AgentRunner.run()` is the single place where Jarvis builds an SDK `Agent`,
  calls `Runner.run()`, persists messages, and returns an `AgentRunResult`.
- `TriggerDispatcher` already funnels Discord, scheduled, and manual runs
  through `AgentRunner`.

The installed `openai-agents` SDK supports local MCP approvals directly on
`MCPServerStdio`, `MCPServerStreamableHttp`, and `MCPServerSse` via the
`require_approval` constructor argument. When a tool needs approval, the run is
interrupted before tool execution. The SDK exposes `RunState.approve()` and
`RunState.reject()` for later resume.

## Decisions

1. **Use the SDK approval mechanism.** Jarvis will not invent a parallel
   proposal/execution protocol for MCP tools in v1.
2. **Apply approvals globally.** The same approval policy applies to Discord,
   schedules, and dashboard manual runs.
3. **Dashboard-first Action Inbox.** v1 approval decisions are made on a new
   `/actions` dashboard page. Discord approve/reject buttons can come later.
4. **Persist SDK run state.** Pending actions store the serialized SDK
   `RunState` and pending approval item so a restart does not lose the approval
   request.
5. **One pending approval per action row.** If a resumed run hits a second
   approval, Jarvis creates another pending action rather than trying to batch
   multiple decisions in v1.
6. **Rejected actions still resume.** Rejecting sends the rejection back to the
   model so Jarvis can explain what happened or choose a safer alternate path.
7. **Denied tools are not executable.** `policy_override=deny` is a hard block;
   the tool should be hidden from the agent surface when practical, otherwise
   rejected with a model-visible policy error.

## Policy Model

Jarvis keeps the operator-facing policy vocabulary already present on `/mcp`:

| UI value | Runtime behavior |
| --- | --- |
| auto-detect | Use the classifier from annotations and tool name. |
| allow | Tool executes without approval. |
| confirm | Tool pauses into the Action Inbox. |
| deny | Tool is not available for execution. |

The current `ToolPolicy` enum uses `AUTO` and `CONFIRM`. This feature extends
the policy layer to expose a runtime decision with three possible outcomes:

- `allow`
- `confirm`
- `deny`

Auto-detected read-only tools map to `allow`; auto-detected non-read tools map
to `confirm`.

## Data Model

Add an `actions` table:

- `id: UUID`
- `status: pending | running | completed | failed`
- `decision: approved | rejected | NULL`
- `conversation_id: UUID | NULL`
- `trigger_id: UUID | NULL`
- `channel_kind: string`
- `channel_ref: string`
- `server_name: string`
- `tool_name: string`
- `tool_call_id: string | NULL`
- `arguments_json: JSON`
- `run_state_json: JSON`
- `approval_item_json: JSON`
- `model: string`
- `created_at: datetime`
- `decided_at: datetime | NULL`
- `completed_at: datetime | NULL`
- `decision_reason: text | NULL`
- `error: text | NULL`

Indexes:

- `status, created_at` for the pending inbox.
- `conversation_id` for conversation-linked history.
- `trigger_id` for audit/debug lookup.

The table stores JSON, not pickles. The SDK `RunState` already serializes to a
JSON-friendly representation, and `ToolApprovalItem` can be serialized from the
interruption payload.

## Architecture

### MCP server construction

`MCPManager` will build every SDK MCP server with:

```python
require_approval=approval_policy.needs_approval
tool_filter=approval_policy.tool_filter
```

The policy helper depends on the database-backed MCP tool shadow rows. Its
responsibilities are:

- map a server/tool pair to `allow`, `confirm`, or `deny`;
- return `True` from `needs_approval(...)` for `confirm`;
- return `False` for `allow`;
- exclude or block `deny` tools.

Because `MCPManager` refreshes tool rows when servers connect or OAuth servers
are replaced, the policy helper must evaluate the latest persisted overrides at
tool-list and tool-call time rather than caching indefinitely.

### AgentRunner interruption handling

`AgentRunner.run()` currently assumes `Runner.run()` returns final output. This
feature adds an explicit interruption branch:

1. Build the SDK agent as today.
2. Call `Runner.run()`.
3. If the result contains tool approval interruptions:
   - persist the user message as today;
   - create one `actions` row for the first approval item;
   - persist a short assistant message such as
     `Action approval required: <server>.<tool>`;
   - return `AgentRunResult(..., final_output=<approval-required text>)`.
4. If no interruption exists, persist the assistant output as today.

This keeps existing channel behavior simple: Discord and dashboard manual runs
get a concise notice, and scheduled runs can route the same notice according to
their configured output mode.

### Action execution service

Add an `ActionService` that owns decision and resume behavior. It is separate
from FastAPI routes so approval can later be exposed through Discord without
duplicating logic.

`approve(action_id)`:

1. Load the pending action with row-level status validation.
2. Deserialize `RunState` and the pending approval item.
3. Call `run_state.approve(approval_item)`.
4. Mark the action `running` with `decision=approved`.
5. Resume with `Runner.run(agent, run_state, run_config=...)`.
6. Persist the final assistant output.
7. Route the final output through `OutputRouter`.
8. Mark the action `completed`, or `failed` if resume raises.

`reject(action_id, reason)`:

1. Load the pending action with row-level status validation.
2. Deserialize `RunState` and the pending approval item.
3. Call `run_state.reject(approval_item, rejection_message=reason_or_default)`.
4. Mark the action `running` with `decision=rejected`.
5. Resume so the model receives the rejection and can produce a final response.
6. Persist and route that final response.
7. Mark the action `completed` after successful resume. The row still records
   `decision=rejected` and `decision_reason`, so the UI can distinguish
   approved-completed from rejected-completed actions.

If the resumed run hits a second approval, `ActionService` creates a new pending
action and marks the current action `completed`. The final routed output for
that resume is the same concise approval-required message used by fresh runs.

### Agent reconstruction

Resuming a `RunState` needs an SDK agent equivalent to the original run. Jarvis
will centralize agent construction in a small helper used by both
`AgentRunner.run()` and `ActionService`.

The helper resolves:

- instructions;
- current MCP server objects from `MCPManager.agent_mcp_servers`;
- the model from the stored run context.

For v1, the action row stores the resolved model string at interruption time.
Resuming uses that model even if the interactive/default model selection has
changed since the action was created. That matches the paused-run mental model.

## Dashboard

Add a top-level nav link: **Actions**.

`GET /actions`:

- pending actions first, newest first;
- recently completed/rejected/failed actions below or on the same table with a
  status filter;
- columns: status, source, tool, created time, arguments preview, decision.

`GET /actions/{id}`:

- full tool name and server;
- source trigger and conversation link;
- formatted JSON arguments;
- decision metadata and error if present;
- approve/reject controls for pending actions.

`POST /actions/{id}/approve`:

- calls `ActionService.approve`;
- redirects back to the action detail page.

`POST /actions/{id}/reject`:

- accepts optional reason;
- calls `ActionService.reject`;
- redirects back to the action detail page.

The UI stays dense and operational, matching the existing dashboard style from
the operations backlog.

## Audit Events

Add audit event types:

- `action.created`
- `action.approved`
- `action.rejected`
- `action.completed`
- `action.failed`

Every event includes `action_id`, `server_name`, `tool_name`, and the related
`conversation_id`/`trigger_id` when available. Tool call/result traces from the
SDK still represent the actual execution after approval.

## Error Handling

- If an action is not `pending`, approve/reject returns a safe dashboard error
  and does not resume.
- If deserialization fails, mark the action `failed` with a clear error.
- If the MCP server is disconnected when resuming, mark `failed` and route a
  short failure message to the original channel.
- If the model is unavailable on resume, mark `failed`; this feature does not
  override the existing model-selection fallback policy.
- If a process crashes after marking an action `running` with a decision but
  before completing resume, the action remains non-terminal. A later repair
  command can be added; v1 surfaces it as `running` without `completed_at`.

## Testing

Unit tests:

- policy mapping from `allow`, `confirm`, `deny`, and auto-detect;
- action serialization/deserialization helpers;
- repository status transitions.

Integration tests:

- a confirm MCP tool creates a pending action and does not execute the tool;
- an allowed read tool executes without creating an action;
- a denied tool is blocked from execution;
- approving a pending action resumes the run and executes the MCP tool;
- rejecting a pending action resumes the run with a rejection message;
- a second approval during resume creates a second action.

Web tests:

- `/actions` lists pending actions;
- action detail renders arguments and source metadata;
- approve and reject POSTs call the service and redirect;
- non-pending action decisions are rejected safely.

Final verification:

- `uv run ruff check jarvis tests`
- `uv run pytest -q`

## Out of Scope

- Discord approve/reject buttons.
- Batch approval of multiple pending tool calls.
- Editing tool arguments before approval.
- A repair/retry queue for actions left in `running` after process crash.
- Hosted MCP conversion for OAuth providers.
