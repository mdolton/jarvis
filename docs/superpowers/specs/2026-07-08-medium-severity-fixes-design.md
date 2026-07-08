# Medium-severity review fixes: OAuth attach/refresh hardening + action resume timeout

Date: 2026-07-08
Status: approved

Five medium-severity findings from the 2026-07-07 full-codebase review, fixed together
on one branch. The branch also carries the pending CLAUDE.md documentation edit as its
first commit (no code impact).

## Fix A — `url_override` honored on every OAuth attach path

**Problem.** OAuth connections attach at `entry.mcp_url` in `MCPManager.connect_connection`,
`MCPManager._bootstrap_connections`, and the OAuth callback route, but at
`conn.url_override or entry.mcp_url` in `refresh_oauth_server_for_retry` and the
background refresh job. A connection with a URL override silently switches endpoints
depending on which path last attached it.

**Design.**
- `connect_connection` and `_bootstrap_connections` (oauth branch) use
  `conn.url_override or entry.mcp_url`.
- The callback route (`jarvis/web/routes/oauth.py`) stops hand-rolling its attach
  (current_headers + catalog + `replace_oauth_server` with `entry.mcp_url`). Instead it
  loads the connection row and calls `mcp_manager.connect_connection(conn)` — one place
  computes an OAuth connection's URL, and the route's duplicated token-decrypt/attach
  logic is deleted. The route still uses `catalog.get` for the provider display name.

## Fix B — Callback attach: timeout is not failure

**Problem.** The callback wraps the attach in `asyncio.wait_for(..., 10.0)`, but the
attach command is already queued on the MCP lifecycle task (60s connect budget) and a
`wait_for` timeout cancels only the waiting, not the queued work. On timeout the route
marks the connection `needs_reauth` while the attach often completes seconds later —
DB status contradicts a live, working server.

**Design.** In the callback route:
- `except TimeoutError:` renders a new `pending` outcome on `oauth_callback.html`
  ("Connected. MCP attach is still in progress — check the MCP page.") and does NOT
  write connection status. The attach continues on the lifecycle task; the dashboard's
  runtime status (MCPServerRow) reflects the eventual result.
- Any other exception keeps today's behavior: mark `needs_reauth` with the error,
  render the error page.
- `POST_CALLBACK_ATTACH_TIMEOUT` stays 10.0s.
- The `TimeoutError` handler must precede the generic handler (`asyncio.wait_for`
  raises the builtin `TimeoutError` on 3.12).

## Fix C — Per-connection refresh lock with freshness short-circuit

**Problem.** Two concurrent 401s both invoke `OAuthFlow.refresh`, both read the same
stored refresh token; with single-use rotation the second exchange gets
`invalid_grant` → `OAuthRefreshPermanentError` → the connection is torn down and marked
`needs_reauth` even though the first refresh succeeded.

**Design.** In `jarvis/oauth/flow.py`:
- `OAuthFlow` gains `self._refresh_locks: dict[UUID, asyncio.Lock]`, entries created on
  demand (`setdefault`). Bounded by the number of connections.
- `refresh(connection_id)` runs its whole body under the connection's lock, with a
  **recency-window short-circuit**: `OAuthFlow` records
  `(monotonic timestamp, headers)` per connection on each successful refresh; a caller
  that acquires the lock within `refresh_coalesce_window_sec` (default 30.0, a
  constructor parameter so tests can inject 0.0) of the last successful refresh gets
  those cached headers back without touching the token endpoint. Failed refreshes
  never populate the cache.
- Why a recency window rather than a `token_expires_at` freshness check: a token
  revoked server-side while still unexpired must NOT short-circuit — the refresh has
  to reach the token endpoint so `invalid_grant` marks the connection `needs_reauth`.
  The recency window coalesces only genuinely concurrent bursts (two 401s within
  milliseconds), which is the actual race.
- `_UnauthorizedTracker` is deliberately unchanged: with the lock, a misclassified 401
  costs one short-circuited refresh, not a torn-down connection.

## Fix D — Action resume timeout

**Problem.** `ActionService._decide` calls `Runner.run` with no timeout, and the
approve route shields it — a wedged resume holds the HTTP request forever and leaves
the action stuck in `running` (unrecoverable: `mark_running` requires `pending`).

**Design.**
- `ActionService.__init__` gains `run_timeout_sec: float | None = None`.
- `_decide` wraps its `Runner.run` call in `async with asyncio.timeout(...)` when set
  (same pattern as `AgentRunner.run`). `TimeoutError` flows into the existing
  `except Exception → _fail_action` path: action marked `failed`, error recorded,
  failure notice routed, exception re-raised.
- `main.py` passes `run_timeout_sec=cfg.jarvis.idle_timeout_sec` (default 900s),
  mirroring how the Scheduler bounds its runner.

## Fix E — Pending OAuth state TTL enforced at use

**Problem.** `mcp_pending` rows are swept daily at 3am with a 600s TTL, but
`handle_callback` never checks age — an authorization `state` is actually valid for up
to ~24h.

**Design.**
- `PENDING_STATE_TTL_SEC = 600` defined once in `jarvis/oauth/store.py`;
  `MCPPendingRepo.sweep_expired` and `oauth_jobs.oauth_pending_sweep` use it as their
  default instead of a literal.
- `handle_callback` rejects a pending row whose `created_at` is older than the TTL:
  delete the row, raise `OAuthCallbackError` with the existing "unknown or expired
  state" wording. The daily sweep remains as garbage collection.

## Fix F — CLAUDE.md documentation edit

The pending working-tree edit (prod deploy/proxy notes, `${VAR}` expansion and header
gotchas, post-merge cleanup ritual) is committed verbatim as the branch's first commit.
No code impact.

## Testing

- **A:** manager tests (stubbed `_build_streamable_http`) assert the URL passed is
  `url_override` when set — for `connect_connection` and for boot attach. A callback
  route test asserts the attach goes through `connect_connection` with the override URL.
- **B:** route test where the manager attach never completes within the wait → 200
  `pending` page and connection status untouched; attach raising → `needs_reauth`
  (existing behavior preserved).
- **C:** two concurrent `refresh()` calls against an httpx MockTransport that counts
  refresh-grant hits → exactly one hit, both callers receive the same fresh token;
  with `refresh_coalesce_window_sec=0.0`, two sequential `refresh()` calls hit the
  endpoint twice (window respected, not permanent).
- **D:** `Runner.run` stubbed to outlast a short timeout → action marked `failed` with
  the timeout recorded, exception propagates to the caller.
- **E:** pending row backdated beyond the TTL → `OAuthCallbackError` and the row is
  deleted; a fresh pending row still completes the callback.
