# stdio MCP tool permissions in the dashboard

**Date:** 2026-06-23
**Status:** Approved (pending spec review)

## Problem

When the agent calls a stdio MCP server's tool (e.g. `brave_web_search`), it
requests confirmation through the dashboard's actions tab every time. There is no
way to proactively grant a stdio tool standing permission so it runs without
prompting.

## Root cause

The capability already exists for OAuth/HTTP connections but is not exposed for
stdio servers — and the missing piece is purely the dashboard template:

- stdio tools **are** enumerated and persisted. On connect, `_do_connect_one`
  (`jarvis/mcp/manager.py:525`) calls `_list_tools` and writes every tool to the
  `mcp_tools` table via `MCPToolRepo.replace_for_server`, identical to the
  HTTP/OAuth path.
- The `mcp_tools.policy_override` column (`allow` / `confirm` / `deny`) already
  drives runtime approval through `MCPApprovalPolicy` / `tool_policy.runtime_decision`.
- `POST /mcp/tools/{tool_id}/policy` (`jarvis/web/routes/mcp.py:78`) already sets
  the override for any tool by id and clears the policy cache.
- The `/mcp` route already passes each stdio server's `tools` to the template
  (`jarvis/web/routes/mcp.py:62`).

The only gap: `mcp.html`'s "stdio servers" section (lines 89–103) renders name,
status, and enable/disable, but **never renders the tools/policy table** that the
connections section renders (lines 31–47).

## Goal

Expose per-tool policy controls for stdio servers in the dashboard, plus a
per-server "Allow all tools" convenience action.

## Design

### Data / runtime — no change

No schema change. Tools are already enumerated; `policy_override` already governs
runtime approval. Setting a tool to `allow` and clearing the policy cache makes the
next call skip the actions-tab prompt with no reconnect.

### Repository

Add to `MCPToolRepo` (`jarvis/persistence/repositories.py`):

```python
async def set_policy_override_for_server(
    self, server_id: UUID, policy_override: str | None
) -> None:
    """Bulk-set policy_override for every tool of a server."""
```

Single `UPDATE mcp_tools SET policy_override = :p WHERE server_id = :id`, committed.

### Routes

- **Per-tool (no change):** `POST /mcp/tools/{tool_id}/policy` already works for
  stdio tools — it is keyed by `tool_id`, source-agnostic.
- **Bulk (new):** `POST /mcp/stdio/{name}/tools/allow-all` in
  `jarvis/web/routes/mcp_admin.py`, mirroring `enable_stdio` / `disable_stdio`:
  1. Resolve the server row by name (`MCPServerRepo`).
  2. `MCPToolRepo.set_policy_override_for_server(server_id, "allow")`.
  3. Clear the policy cache: `ctx.mcp_manager.clear_policy_cache(name)`
     (guard with `getattr`, matching the existing per-tool route's defensive style).
  4. Emit a `stdio.tools.allow_all` audit event via `_emit`.
  5. `_redirect()` to `/mcp`.

### Template (`jarvis/web/templates/mcp.html`)

- Extract the existing policy-table markup (currently inline at lines 31–47) into a
  Jinja macro `{% macro tools_table(tools) %}…{% endmacro %}`.
- Replace the inline table in the connections section with a `tools_table(c.tools)`
  call.
- In the stdio section, render `tools_table(srv.tools)` when `srv.tools` is
  non-empty, plus an "Allow all tools" button posting to
  `/mcp/stdio/{{ srv.name }}/tools/allow-all`.

## Testing

Integration tests under `tests/integration/`:

1. Per-tool policy persists for a stdio server, and
   `MCPApprovalPolicy.needs_approval` flips True→False after setting `allow`
   (with a `clear_cache` between, as the runtime does via cache clear).
2. The bulk endpoint sets every tool of a server to `allow` and clears the cache.
3. The `/mcp` page renders the stdio tools table (tool name + policy select
   present in the HTML for a stdio server with tools).

## Out of scope (YAGNI)

- No bulk "deny all" / "reset to auto-detect".
- No bulk control for connection servers (per-tool already exists there).
- No new config keys; `policy_override` remains DB-only operator state.
