# Low-severity review fixes + PR #52 follow-ups

Date: 2026-07-08
Status: approved

Eleven small fixes: the seven low-severity findings from the 2026-07-07 full-codebase
review plus the four follow-ups filed by PR #52's final review. One branch.

## Group 1 — OAuth flow robustness (`jarvis/oauth/flow.py`)

### 1. Refresh response validation

**Problem.** `_refresh_locked` does `data = resp.json()` then `data["access_token"]` on
a 200 response. A non-JSON body raises a JSON decode error; a JSON body without
`access_token` raises bare `KeyError`. Both escape the transient/permanent error
taxonomy the callers (background refresh job, 401-retry path) rely on.

**Design.** After the 4xx/5xx handling, parse defensively: JSON decode failure or a
missing/empty `access_token` raises
`OAuthRefreshTransientError("token endpoint returned malformed response: <detail>")`.
Transient, not permanent — a malformed provider response must not mark the connection
`needs_reauth`; the next refresh cycle retries.

### 2. RFC 8707 `resource` indicator honors `url_override`

**Problem.** `start_authorization`, `handle_callback`, and `_refresh_locked` send
`resource = entry.mcp_url` even when the connection is attached at `conn.url_override`
— tokens are minted for the catalog resource while requests go to the override.

**Design.** All three sites send `resource = conn.url_override or entry.mcp_url`.
Each site already has the connection row loaded. Guarded by `entry.send_resource_indicator`
as today.

### 3. Refresh-cache eviction on revoke

**Problem.** `_last_refresh` (headers containing a live bearer token) and
`_refresh_locks` entries are never removed. Within 30s of a successful refresh, a
disconnect + immediate refresh would serve the revoked token from cache; the dicts
also grow monotonically.

**Design.** `revoke(connection_id)` pops both dict entries at method entry — revoke is
the token-invalidation choke point (the disconnect and remove routes both call it).
A refresh in flight while revoke runs can at worst trigger one extra token exchange
(the popped lock is simply replaced on next use); a comment documents this benign race.

## Group 2 — Route fixes

### 4. `oauth_connect` catches `DCRUnsupportedError` (`jarvis/web/routes/oauth.py`)

**Problem.** `start_authorization` → `register_client` can raise `DCRUnsupportedError`,
which is not an `OAuthDiscoveryError` subclass — the route's except misses it and the
user gets a raw 500.

**Design.** Catch `(OAuthDiscoveryError, DCRUnsupportedError)` and render the existing
error template at 502, same as discovery failures.

### 5. `edit_provider_credentials` rejects providers with no connections (`jarvis/web/routes/mcp_admin.py`)

**Problem.** Posting credentials for a provider with zero connections encrypts them and
silently discards them (loop over empty list); the natural flow "add provider → set
credentials → add connection" loses the credentials with no error.

**Design.** If `list_for_provider` returns no rows, raise
`HTTPException(400, "provider has no connections — add a connection first, then set credentials")`
before any encryption/DB work. The audit event is only emitted on success.

### 6. Vanished-connection callback branch gets a test (`tests/integration/test_web_oauth.py`)

**Problem.** The `conn is None` branch added in PR #52 (error page instead of false
success) has no test — the interleaving isn't reachable from the HTTP surface.

**Design.** Monkeypatch `jarvis.web.routes.oauth.MCPConnectionRepo` with a stub whose
`get` returns `None` (only for the route module; `handle_callback` uses its own import
and still works). Assert HTTP 500 and the "removed before the MCP attach" message.

## Group 3 — MCP manager hygiene (`jarvis/mcp/manager.py`)

### 7. Collision-correct wire names in `agent_mcp_context`

**Problem.** `agent_mcp_context` recomputes wire names per tool via `_tool_wire_name`,
without the collision digest that `_tool_wire_names` applies. When two raw tool names
sanitize to the same wire name, the prompt advertises the undigested name for both —
one of which does not exist at call time.

**Design.** The manager stores the collision-aware mapping at discovery time:
`self._wire_names_by_server: dict[str, dict[str, str]]` (raw → wire), populated in
`_do_connect_one` and `_do_replace_oauth` from `_tool_wire_names(namespace, raw_names)`,
removed in `_do_remove_oauth`, cleared in `_do_stop_all`. `agent_mcp_context` reads the
stored mapping, falling back to `_tool_wire_name` computation only when a server has no
stored mapping. (`_NamespacedMCPServer` keeps its own per-call mapping; this stored copy
is only for prompt context.)

### 8. Annotation/docstring truth

`_do_replace_oauth` is annotated `-> None` but returns the new SDK server, and
`refresh_oauth_server_for_retry` depends on the value flowing back through `_submit`.
Change the annotation to `-> object` and note the return value in both
`_do_replace_oauth`'s and `replace_oauth_server`'s docstrings. No behavior change.

## Group 4 — Memory repo footgun (`jarvis/persistence/repositories.py`)

### 9. `create_pending_many` internal-duplicate safety

**Problem.** A batch containing two items with the same normalized content raises
`IntegrityError` on the first insert; the `_create_missing_pending` retry filters
against the DB (neither exists), re-inserts both, collides again, and returns `[]` —
the entire batch is silently dropped. Currently masked by the only caller's pre-dedup.

**Design.** Both `create_pending_many` and `_create_missing_pending` dedupe the incoming
items by `_normalize_preference_content` (first occurrence wins) before building rows.
The final `IntegrityError → return []` fallback in `_create_missing_pending` remains,
now reachable only via true concurrent races.

## Group 5 — Docstring rot (`jarvis/audit/tracer.py`)

### 10. Correct the tracer module docstring

The docstring claims the processor is "paired with `set_tracing_disabled(True)` in
llm_client.install_as_default" — `install_as_default` deliberately does not call that
(its own docstring explains why). Rewrite the paragraph: `set_trace_processors([...])`
replaces the SDK's default OpenAI exporter, which alone prevents traces leaking to
OpenAI. Comment-only change.

## Group 6 — `AppContext.shutdown()` aiosqlite connection leak (`jarvis/main.py` / `jarvis/persistence/db.py`)

### 11. Diagnose and fix the un-stopped pooled connection

**Problem (root-caused during PR #52 verification; see the instrumentation writeup in
`.superpowers/sdd/task-6-report.md` and the `appcontext-shutdown-connection-leak`
memory).** `test_main_smoke.py::test_bootstrap_disables_memory_for_non_sqlite_db_url`'s
`ctx.shutdown()` explicitly stops only one of its two pooled aiosqlite connections; the
second is never stopped by `await engine.dispose()` and is reaped later by CPython GC
(`aiosqlite.Connection.__del__` → `.stop()`) on whatever event loop is then running —
the source of the intermittent
`PytestUnhandledThreadExceptionWarning: RuntimeError: Event loop is closed` that lands
on unrelated tests.

**Design.** This task carries diagnostic latitude:

1. Reproduce with the cheap instrumentation recipe from the writeup (venv-level
   aiosqlite event log + current-test tagging; heavier instrumentation hides the race).
2. Determine why the second connection survives `dispose()` — leading hypotheses: a
   connection still checked out at dispose time (dispose only closes checked-in
   connections), or the async pool's terminate path not stopping the aiosqlite worker.
3. Fix in production code (shutdown ordering in `AppContext.shutdown()`, or engine/pool
   configuration for SQLite in `jarvis/persistence/db.py` — e.g. `NullPool` if the pool
   itself is the problem). Tests must not change to mask the leak.
4. If the true fix requires upstream (SQLAlchemy/aiosqlite) changes, STOP and report
   with evidence instead of working around it.

**Acceptance.** Instrumented verification that every aiosqlite connection opened during
the smoke test reaches an explicit `stop` before test end (no GC-reaped `del`), and 5
consecutive full-suite runs showing only the pre-existing audioop deprecation warning.
All instrumentation reverted before commit.

## Testing

- **1:** MockTransport returns 200 with (a) non-JSON body, (b) JSON without
  `access_token` → `OAuthRefreshTransientError` both times; connection NOT marked
  `needs_reauth`.
- **2:** MockTransport captures request bodies/URLs; with `url_override` set, the
  `resource` param equals the override in the authorize URL, the code exchange, and the
  refresh exchange.
- **3:** refresh once (cache populated), call `revoke`, refresh again immediately →
  the refresh grant hits the token endpoint twice total (without eviction the second
  call would be served from the 30s cache: one hit).
- **4:** provider metadata without `registration_endpoint`, DCR connection → GET
  `/oauth/connect/{id}` renders the error page with 502, not a raw 500.
- **5:** POST `/mcp/providers/{key}/edit-credentials` for a connection-less provider →
  400; with a connection → credentials set (existing behavior).
- **6:** monkeypatched repo returning `None` → 500 + "removed before the MCP attach".
- **7:** manager test with two colliding raw tool names (e.g. `do thing` and `do-thing`,
  both sanitizing to `do_thing`) → `agent_mcp_context` output contains the digested wire
  name exactly as `_tool_wire_names` produced it.
- **9:** `create_pending_many` with two same-normalized items → exactly one row
  persisted, returned list has one element.
- **8, 10:** non-behavioral; lint + existing suite.
- **11:** acceptance criteria above.
