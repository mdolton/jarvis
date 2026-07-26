"""MCPManager — owns lifecycle of all configured MCP servers.

- On start(): launch a single long-lived "lifecycle" task, then ask it to
  spin up each enabled MCP server (stdio, http, or sse) and record status +
  discovered tools to the DB shadow tables.
- While running: the Agents SDK owns the actual connections and tool caching
  via `cache_tools_list=True`. We just keep the SDK server objects alive.
- On stop(): ask the lifecycle task to async-close each SDK server, then exit.

Why a single owner task? `MCPServerStreamableHttp` is built on anyio, whose
cancel scopes MUST be entered and exited on the same asyncio task. Connecting
on one task (e.g. an ephemeral OAuth-refresh job) and closing on another (a
fire-and-forget task or the shutdown task) corrupts anyio's cancel-scope state
and tears down the whole event loop. Routing every connect/replace/remove/close
through one task makes enter and exit always happen on the same task.
"""

import asyncio
import copy
import hashlib
import logging
import re
from collections.abc import Collection
from contextlib import AsyncExitStack
from dataclasses import dataclass

import httpx
from agents.exceptions import UserError
from agents.mcp import MCPServerSse, MCPServerStdio, MCPServerStreamableHttp
from mcp.shared._httpx_utils import create_mcp_http_client
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.config.schema import MCPServerConfig, MCPServersConfig
from jarvis.mcp.approval_policy import MCPApprovalPolicy
from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.oauth.catalog import ProviderCatalog, assert_no_yaml_collision
from jarvis.oauth.crypto import decrypt_blob
from jarvis.oauth.store import MCPConnectionRepo
from jarvis.persistence.repositories import MCPServerRepo, MCPToolRepo, SettingsRepo

_log = logging.getLogger(__name__)

_OAUTH_TOOL_CALL_TIMEOUT_SEC = 35.0

# Remote (http/sse) MCP servers need a wider per-call budget than the agents-SDK
# defaults (5s read timeout, 5s HTTP timeout, no retries), which are too tight
# for real remote servers: e.g. the self-hosted ynab MCP server's `initialize`
# regularly takes ~3-6s (cold start), which blew the 5s ClientSession read
# timeout and surfaced as "Connection timeout." at boot. These are the same
# widened values the OAuth streamable-HTTP path uses.
_REMOTE_HTTP_TIMEOUT_SEC = 30
_REMOTE_MAX_RETRY_ATTEMPTS = 2
_REMOTE_RETRY_BACKOFF_BASE = 1.0
_MAX_FUNCTION_TOOL_NAME_LEN = 64


def _identifier_segment(value: str) -> str:
    segment = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return segment or "default"


def _tool_namespace_for_runtime_name(runtime_name: str) -> str:
    if ":" not in runtime_name:
        return _identifier_segment(runtime_name)
    provider_key, label_slug = runtime_name.split(":", 1)
    return f"{_identifier_segment(label_slug)}.{_identifier_segment(provider_key)}"


def _shorten_tool_name(name: str, *, digest_source: str) -> str:
    if len(name) <= _MAX_FUNCTION_TOOL_NAME_LEN:
        return name
    digest = hashlib.sha1(digest_source.encode()).hexdigest()[:10]
    prefix_len = _MAX_FUNCTION_TOOL_NAME_LEN - len(digest) - 2
    prefix = name[:prefix_len].rstrip("_") or "tool"
    return f"{prefix}__{digest}"


def _tool_wire_name(namespace: str, tool_name: str, *, force_digest: bool = False) -> str:
    segments = [_identifier_segment(part) for part in namespace.split(".")]
    segments.append(_identifier_segment(tool_name))
    base = "__".join(segment for segment in segments if segment)
    if force_digest:
        digest = hashlib.sha1(f"{namespace}\0{tool_name}".encode()).hexdigest()[:10]
        base = f"{base}__{digest}"
    return _shorten_tool_name(base, digest_source=f"{namespace}\0{tool_name}")


def _tool_wire_names(namespace: str, raw_tool_names: list[str]) -> dict[str, str]:
    raw_to_wire: dict[str, str] = {}
    wire_to_raw: dict[str, str] = {}
    for raw_name in raw_tool_names:
        wire_name = _tool_wire_name(namespace, raw_name)
        if wire_name in wire_to_raw and wire_to_raw[wire_name] != raw_name:
            wire_name = _tool_wire_name(namespace, raw_name, force_digest=True)
        raw_to_wire[raw_name] = wire_name
        wire_to_raw[wire_name] = raw_name
    return raw_to_wire


def _copy_tool_with_name(tool, name: str):
    model_copy = getattr(tool, "model_copy", None)
    if callable(model_copy):
        return model_copy(update={"name": name})
    copied = copy.copy(tool)
    copied.name = name
    return copied


class _NamespacedMCPServer:
    """Expose one MCP server's tools under a connection-specific function prefix."""

    def __init__(self, inner: object, *, namespace: str) -> None:
        self._inner = inner
        self._namespace = namespace
        self._raw_by_wire: dict[str, str] = {}

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def __eq__(self, other: object) -> bool:
        return other is self or other is self._inner

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def cached_tools(self):
        tools = getattr(self._inner, "cached_tools", None)
        if not tools:
            return tools
        return self._namespaced_tools(tools)

    async def list_tools(self, run_context=None, agent=None):
        tools = await self._inner.list_tools(run_context, agent)
        return self._namespaced_tools(tools)

    async def call_tool(self, tool_name: str, arguments: dict | None, meta: dict | None = None):
        raw_tool_name = self._raw_by_wire.get(tool_name, tool_name)
        return await self._inner.call_tool(raw_tool_name, arguments, meta=meta)

    def _get_needs_approval_for_tool(self, tool, agent):
        raw_name = self._raw_by_wire.get(tool.name)
        raw_tool = _copy_tool_with_name(tool, raw_name) if raw_name is not None else tool
        return self._inner._get_needs_approval_for_tool(raw_tool, agent)

    def _get_failure_error_function(self, agent_failure_error_function):
        return self._inner._get_failure_error_function(agent_failure_error_function)

    def _namespaced_tools(self, tools):
        raw_names = [tool.name for tool in tools]
        raw_to_wire = _tool_wire_names(self._namespace, raw_names)
        self._raw_by_wire = {wire: raw for raw, wire in raw_to_wire.items()}
        return [_copy_tool_with_name(tool, raw_to_wire[tool.name]) for tool in tools]


class _TokenHolder:
    """Mutable box for the current OAuth access token.

    The live streamable-HTTP connection reads this on every request (via an
    httpx request hook), so refreshing a token is a single in-memory write —
    no reconnect, no list_tools, no SDK-server swap. This is what lets a
    refresh take effect on the *existing* connection instead of requiring the
    fragile rebuild-and-swap dance that previously left the live socket pinned
    to a dead token whenever the swap failed.
    """

    __slots__ = ("_token",)

    def __init__(self, token: str) -> None:
        self._token = token

    def get(self) -> str:
        return self._token

    def set(self, token: str) -> None:
        self._token = token


def _bearer_token(headers: dict[str, str]) -> str:
    """Extract the raw token from an ``{"Authorization": "Bearer <t>"}`` dict."""
    auth = headers.get("Authorization", "")
    return auth[7:] if auth.startswith("Bearer ") else auth


def _decrypt_headers(blob: bytes | None, key: bytes) -> dict[str, str]:
    if not blob:
        return {}
    import json

    return json.loads(decrypt_blob(blob, key).decode())


class MCPManager:
    def __init__(
        self,
        *,
        config: MCPServersConfig,
        session_factory: async_sessionmaker[AsyncSession],
        secrets_key: bytes | None = None,
        oauth_flow=None,
        catalog: "ProviderCatalog | None" = None,
        connect_timeout: float = 60.0,
        close_timeout: float = 10.0,
        audit=None,
        sensitivity_terms_provider=None,
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._secrets_key = secrets_key
        self._oauth_flow = oauth_flow
        self._catalog = catalog
        # Hard ceiling on a single connect+list_tools so a transiently
        # unresponsive remote can never wedge the serial lifecycle loop (and
        # thereby block Disconnect/remove, which run through the same task).
        self._connect_timeout = connect_timeout
        self._close_timeout = close_timeout
        self._approval_policy = MCPApprovalPolicy(
            session_factory=session_factory,
            audit=audit,
            sensitivity_terms_provider=sensitivity_terms_provider,
        )
        self._stacks: dict[str, AsyncExitStack] = {}
        self._sdk_servers: dict[str, object] = {}
        self._tool_names_by_server: dict[str, tuple[str, ...]] = {}
        self._tool_namespaces_by_server: dict[str, str] = {}
        # Collision-digested raw->wire tool-name map per server, captured at
        # discovery time so agent_mcp_context advertises names that exist.
        self._wire_names_by_server: dict[str, dict[str, str]] = {}
        # Live access token per OAuth provider. Refreshing updates the holder in
        # place; the running connection reads it on every request.
        self._token_holders: dict[str, _TokenHolder] = {}
        # All connection enter/exit happens on this single owner task.
        self._cmd_queue: asyncio.Queue[tuple[str, object, asyncio.Future]] | None = None
        self._loop_task: asyncio.Task | None = None

    async def _collision_keys(self) -> set[str]:
        from jarvis.oauth.catalog import SEED_PROVIDERS
        from jarvis.oauth.store import MCPConnectionRepo, MCPProviderRepo

        keys = set(SEED_PROVIDERS)
        if self._catalog is not None:
            async with self._session_factory() as s:
                keys |= {p.key for p in await MCPProviderRepo(s).list_all()}
                keys |= {c.runtime_name for c in await MCPConnectionRepo(s).list_all()}
        return keys

    async def start(self) -> None:
        """Connect to every enabled server. Failures are recorded, not raised."""
        assert_no_yaml_collision(
            (s.name for s in self._config.servers), await self._collision_keys()
        )
        self._cmd_queue = asyncio.Queue()
        self._loop_task = asyncio.create_task(self._lifecycle_loop(), name="mcp-lifecycle")

        # Reconcile config-backed server rows against the current yaml: drop any
        # stdio-source row no longer in the config. This prunes servers removed from
        # the yaml and the orphans the 0011 migration mislabeled as stdio (old
        # per-provider OAuth/HTTP rows, now superseded by provider/connection rows).
        async with self._session_factory() as session:
            pruned = await MCPServerRepo(session).delete_stdio_absent_from(
                s.name for s in self._config.servers
            )
        if pruned:
            _log.info("pruned %d orphaned config MCP server row(s) absent from yaml", pruned)

        async with self._session_factory() as session:
            disabled = set(await SettingsRepo(session).get("mcp.stdio_disabled") or [])
        for server_cfg in self._config.servers:
            if not server_cfg.enabled or server_cfg.name in disabled:
                continue
            # connect failures are recorded inside the owner task, never raised.
            await self._submit("connect_cfg", server_cfg)

        if self._secrets_key is not None and self._catalog is not None:
            await self._bootstrap_connections()

    async def stop(self) -> None:
        if self._loop_task is None:
            return  # never started, or already stopped — idempotent
        try:
            await self._submit("shutdown", None)
        finally:
            await self._loop_task
            self._loop_task = None
            self._cmd_queue = None

    def agent_mcp_servers(self, *, only: Collection[str] | None = None) -> list[object]:
        """Return the SDK server objects to pass into `Agent(mcp_servers=...)`.

        `only` narrows the result to the named servers; None means every
        connected server. Filtering happens here, against the `_sdk_servers`
        keys, because those keys are the authoritative runtime names — a
        server object's own `.name` is the upstream SDK's and does not always
        match the key we registered it under.

        Names in `only` that match nothing are ignored (a server can be
        disconnected or removed long after a schedule pinned it); the caller
        logs that, since silently running with fewer tools than asked for is
        worth noticing.
        """
        if only is None:
            return list(self._sdk_servers.values())
        wanted = set(only)
        return [server for name, server in self._sdk_servers.items() if name in wanted]

    def server_names(self) -> list[str]:
        """Names of every connected server, for scope pickers in the UI."""
        return sorted(self._sdk_servers)

    def agent_mcp_context(self) -> str:
        """Return a concise description of live MCP capabilities for the prompt."""
        if not self._sdk_servers:
            return ""
        lines = ["Current MCP servers:"]
        for name in sorted(self._sdk_servers):
            tools = self._tool_names_by_server.get(name, ())
            namespace = self._tool_namespaces_by_server.get(
                name, _tool_namespace_for_runtime_name(name)
            )
            if tools:
                wire_by_raw = self._wire_names_by_server.get(name, {})
                exposed = ", ".join(
                    f"{wire_by_raw.get(tool, _tool_wire_name(namespace, tool))} (raw: {tool})"
                    for tool in tools
                )
                lines.append(f"- {name} (namespace {namespace}): {exposed}")
            else:
                lines.append(f"- {name} (namespace {namespace})")
        return "\n".join(lines)

    def clear_policy_cache(self, server_name: str | None = None) -> None:
        if server_name is None:
            self._approval_policy.clear_cache()
        else:
            self._approval_policy.clear_server(server_name)

    async def replace_oauth_server(
        self,
        provider_key: str,
        *,
        url: str,
        headers: dict[str, str],
        oauth: bool = True,
        tool_namespace: str | None = None,
    ) -> None:
        """Build a new streamable-HTTP SDK server, verify list_tools, then atomically swap.

        If list_tools fails the new stack is closed and the old server remains active.
        Runs entirely on the owner task so enter/exit stay task-consistent.

        ``oauth`` selects bearer/oauth-retry wiring: True (default) for OAuth
        connections that inject a live access token and refresh on 401; False for
        http/sse connections, which carry only static headers and must NOT get a
        bearer token holder or an oauth ``unauthorized_retry``.

        The underlying command returns the new SDK server (used by the refresh-retry
        path); this wrapper discards it.
        """
        await self._submit(
            "replace",
            {
                "provider_key": provider_key,
                "url": url,
                "headers": headers,
                "oauth": oauth,
                "tool_namespace": tool_namespace or _tool_namespace_for_runtime_name(provider_key),
            },
        )

    async def remove_oauth_server(self, provider_key: str) -> None:
        """Pop and close the stack/sdk_server entries for an OAuth provider."""
        await self._submit("remove", provider_key)

    async def connect_connection(self, conn) -> None:
        """Attach one connection (dashboard enable/add). Resolves transport from its provider."""
        entry = await self._catalog.get(conn.provider_key)
        if entry.kind == "oauth":
            if not conn.access_token_enc:
                return
            token = decrypt_blob(conn.access_token_enc, self._secrets_key).decode()
            await self.replace_oauth_server(
                conn.runtime_name,
                url=conn.url_override or entry.mcp_url,
                headers={"Authorization": f"Bearer {token}"},
                oauth=True,
                tool_namespace=_tool_namespace_for_runtime_name(conn.runtime_name),
            )
        else:
            headers = _decrypt_headers(conn.headers_enc, self._secrets_key)
            await self.replace_oauth_server(
                conn.runtime_name,
                url=conn.url_override or entry.mcp_url,
                headers=headers,
                oauth=False,
                tool_namespace=_tool_namespace_for_runtime_name(conn.runtime_name),
            )

    async def connect_server(self, cfg) -> None:
        """Connect one stdio config server (dashboard enable). Thin wrapper over the owner task."""
        await self._submit("connect_cfg", cfg)

    async def disconnect(self, runtime_name: str) -> None:
        await self.remove_oauth_server(runtime_name)

    def update_oauth_token(self, provider_key: str, access_token: str) -> bool:
        """Point the live connection at a new access token, in place.

        Returns True if a live holder existed and was updated; False if the
        provider has no attached server (caller should fall back to a full
        attach). Synchronous and cannot fail, so the DB token state and the
        live connection's token never diverge.
        """
        holder = self._token_holders.get(provider_key)
        if holder is None:
            return False
        holder.set(access_token)
        return True

    async def refresh_oauth_server_for_retry(self, runtime_name: str) -> object:
        """Force-refresh a connection's token and keep the live connection.

        The refreshed token is applied to the existing SDK server in place; we
        return that same server so the caller retries on the connection that is
        already open. Only if the connection isn't currently attached (no live
        holder) do we fall back to a full attach via the lifecycle owner task.
        """
        if self._oauth_flow is None:
            raise RuntimeError("MCPManager was not configured with an OAuthFlow")
        async with self._session_factory() as session:
            conn = await MCPConnectionRepo(session).get_by_runtime_name(runtime_name)
        if conn is None:
            raise LookupError(f"no MCP connection for runtime_name {runtime_name!r}")
        headers = await self._oauth_flow.refresh(conn.id)
        if self.update_oauth_token(runtime_name, _bearer_token(headers)):
            return self._sdk_servers.get(runtime_name)
        entry = await self._catalog.get(conn.provider_key)
        return await self._submit(
            "replace",
            {
                "provider_key": runtime_name,
                "url": conn.url_override or entry.mcp_url,
                "headers": headers,
                "tool_namespace": _tool_namespace_for_runtime_name(runtime_name),
            },
        )

    # --- Owner task: the only place stacks are entered and exited ----------

    async def _submit(self, kind: str, payload: object):
        """Hand a command to the owner task and await its result/exception."""
        if self._cmd_queue is None:
            raise RuntimeError("MCPManager not started")
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._cmd_queue.put((kind, payload, fut))
        return await fut

    async def _lifecycle_loop(self) -> None:
        assert self._cmd_queue is not None
        while True:
            kind, payload, fut = await self._cmd_queue.get()
            try:
                if kind == "connect_cfg":
                    await self._do_connect_one(payload)  # type: ignore[arg-type]
                    result = None
                elif kind == "replace":
                    result = await self._do_replace_oauth(**payload)  # type: ignore[arg-type]
                elif kind == "remove":
                    result = await self._do_remove_oauth(payload)  # type: ignore[arg-type]
                elif kind == "shutdown":
                    await self._do_stop_all()
                    result = None
                else:  # pragma: no cover - guards programmer error
                    raise RuntimeError(f"unknown MCP command {kind!r}")
                if not fut.done():
                    fut.set_result(result)
            except asyncio.CancelledError:
                # A stray cancellation bled out of an MCP connection teardown.
                # The owner task is never externally cancelled (stop() uses a
                # command, not task cancellation), so this is always spurious —
                # log and keep serving rather than let the owner task die.
                _log.warning("ignoring spurious cancellation in MCP lifecycle loop")
                if not fut.done():
                    fut.set_result(None)
            except Exception as e:
                if not fut.done():
                    fut.set_exception(e)
                else:
                    # The caller stopped waiting (e.g. the OAuth callback's
                    # bounded wait timed out) — without this, a late failure
                    # would vanish: no log, no dashboard status, while the UI
                    # says "check the MCP page".
                    _log.exception("MCP %r command failed after caller stopped waiting", kind)
                    if kind == "replace":
                        await self._record_replace_failure(payload, e)
            if kind == "shutdown":
                return

    async def _bootstrap_connections(self) -> None:
        """Attach every enabled connection at startup, keyed by runtime_name."""
        async with self._session_factory() as session:
            conns = await MCPConnectionRepo(session).list_enabled()
        for conn in conns:
            entry = await self._catalog.get(conn.provider_key)
            try:
                if entry.kind == "oauth":
                    if conn.status != "connected" or not conn.access_token_enc:
                        continue  # not authorized yet / needs reauth
                    token = decrypt_blob(conn.access_token_enc, self._secrets_key).decode()
                    await self.replace_oauth_server(
                        conn.runtime_name,
                        url=conn.url_override or entry.mcp_url,
                        headers={"Authorization": f"Bearer {token}"},
                        oauth=True,
                        tool_namespace=_tool_namespace_for_runtime_name(conn.runtime_name),
                    )
                else:  # http / sse
                    headers = _decrypt_headers(conn.headers_enc, self._secrets_key)
                    url = conn.url_override or entry.mcp_url
                    await self.replace_oauth_server(
                        conn.runtime_name,
                        url=url,
                        headers=headers,
                        oauth=False,
                        tool_namespace=_tool_namespace_for_runtime_name(conn.runtime_name),
                    )
            except Exception as e:
                _log.exception("failed to attach connection %r at boot", conn.runtime_name)
                async with self._session_factory() as session:
                    await MCPConnectionRepo(session).set_status(
                        conn.id, status="needs_reauth", last_error=f"boot attach failed: {e}"
                    )

    async def _do_connect_one(self, cfg: MCPServerConfig) -> None:
        """Connect one configured server. Records failure instead of raising."""
        try:
            async with self._session_factory() as session:
                row = await MCPServerRepo(session).upsert(name=cfg.name, transport=cfg.transport)
                server_id = row.id

            stack = AsyncExitStack()
            sdk_server = _build_sdk_server(cfg, approval_policy=self._approval_policy)

            try:
                async with asyncio.timeout(self._connect_timeout):
                    await stack.enter_async_context(sdk_server)
                    tools = await _list_tools(sdk_server)
            except BaseException:
                # Eagerly close on connect/list_tools failure or timeout so we
                # never keep a half-broken (or hung) server around.
                await _aclose_silently(stack, close_timeout=self._close_timeout)
                raise

            if cfg.read_only:
                tools = [_force_read_only(t) for t in tools]

            self._stacks[cfg.name] = stack
            self._tool_namespaces_by_server[cfg.name] = _tool_namespace_for_runtime_name(cfg.name)
            self._sdk_servers[cfg.name] = _NamespacedMCPServer(
                sdk_server,
                namespace=self._tool_namespaces_by_server[cfg.name],
            )
            self._tool_names_by_server[cfg.name] = tuple(t.name for t in tools)
            self._wire_names_by_server[cfg.name] = _tool_wire_names(
                self._tool_namespaces_by_server[cfg.name], [t.name for t in tools]
            )

            async with self._session_factory() as session:
                srepo = MCPServerRepo(session)
                trepo = MCPToolRepo(session)
                await srepo.set_status(server_id, status="connected", last_error=None)
                await trepo.replace_for_server(server_id, tools=tools)
            self.clear_policy_cache(cfg.name)
        except Exception as e:  # one bad server mustn't kill the rest
            _log.exception("failed to connect MCP server %r", cfg.name)
            await self._record_failure(cfg, e)

    async def _record_failure(self, cfg: MCPServerConfig, exc: Exception) -> None:
        async with self._session_factory() as session:
            repo = MCPServerRepo(session)
            row = await repo.upsert(name=cfg.name, transport=cfg.transport)
            await repo.set_status(row.id, status="error", last_error=f"{type(exc).__name__}: {exc}")

    async def _record_replace_failure(self, payload: object, exc: Exception) -> None:
        """Best-effort dashboard status for a replace that failed after its
        caller stopped waiting. Mirrors the status write in _do_replace_oauth,
        but with an error status; never raises."""
        try:
            provider_key = payload["provider_key"]  # type: ignore[index]
            async with self._session_factory() as session:
                repo = MCPServerRepo(session)
                conn = await MCPConnectionRepo(session).get_by_runtime_name(provider_key)
                row = await repo.upsert(
                    name=provider_key,
                    transport="http",
                    source="connection",
                    connection_id=conn.id if conn else None,
                )
                await repo.set_status(
                    row.id, status="error", last_error=f"{type(exc).__name__}: {exc}"
                )
        except Exception:
            _log.exception("failed to record late replace failure")

    async def _do_replace_oauth(
        self,
        *,
        provider_key: str,
        url: str,
        headers: dict[str, str],
        oauth: bool = True,
        tool_namespace: str | None = None,
    ) -> object:
        """Build and swap a new MCP server, returning it for retry logic.

        Returns the new SDK server object; refresh_oauth_server_for_retry consumes
        it through _submit's result.
        """
        tool_namespace = tool_namespace or _tool_namespace_for_runtime_name(provider_key)
        new_stack = AsyncExitStack()

        async def unauthorized_retry():
            return await self.refresh_oauth_server_for_retry(provider_key)

        build_kwargs = {
            "name": provider_key,
            "approval_policy": self._approval_policy,
        }
        if oauth:
            # Fresh holder for the new connection; only promoted into
            # self._token_holders once this server is committed as live, so a
            # failed build never disturbs the currently-attached connection's
            # token. http/sse connections get no holder (static headers only).
            holder = _TokenHolder(_bearer_token(headers))
            build_kwargs["token_holder"] = holder
            if self._oauth_flow is not None:
                build_kwargs["unauthorized_retry"] = unauthorized_retry
        else:
            holder = None
        new_sdk = _build_streamable_http(url, headers, **build_kwargs)

        try:
            async with asyncio.timeout(self._connect_timeout):
                await new_stack.enter_async_context(new_sdk)
                tools = await _list_tools(new_sdk)
        except BaseException:
            # Connect/list_tools failed or timed out. Drop the half-open stack so
            # a hung connection can't linger, then surface the error to the caller
            # (the old server, if any, stays active).
            await _aclose_silently(new_stack, close_timeout=self._close_timeout)
            raise

        old_stack = self._stacks.get(provider_key)
        self._tool_namespaces_by_server[provider_key] = tool_namespace
        self._sdk_servers[provider_key] = _NamespacedMCPServer(
            new_sdk,
            namespace=tool_namespace,
        )
        self._stacks[provider_key] = new_stack
        if holder is not None:
            self._token_holders[provider_key] = holder
        else:
            self._token_holders.pop(provider_key, None)
        self._tool_names_by_server[provider_key] = tuple(t.name for t in tools)
        self._wire_names_by_server[provider_key] = _tool_wire_names(
            tool_namespace, [t.name for t in tools]
        )

        # Persist status/tools BEFORE closing the old connection. Closing an
        # anyio-based streamable-HTTP connection can emit a stray cancellation
        # out of its task-group teardown; keeping our DB write ahead of the close
        # means that write is never disturbed (which previously invalidated and
        # terminated the connection — "Exception terminating connection").
        async with self._session_factory() as session:
            srepo = MCPServerRepo(session)
            trepo = MCPToolRepo(session)
            conn = await MCPConnectionRepo(session).get_by_runtime_name(provider_key)
            row = await srepo.upsert(
                name=provider_key,
                transport="http",
                source="connection",
                connection_id=conn.id if conn else None,
            )
            await srepo.set_status(row.id, status="connected", last_error=None)
            await trepo.replace_for_server(row.id, tools=tools)
        self.clear_policy_cache(provider_key)

        # Close the old connection LAST, on THIS (owner) task — the same task it
        # was opened on. _aclose_silently swallows any stray teardown error.
        if old_stack is not None:
            await _aclose_silently(old_stack, close_timeout=self._close_timeout)
        return new_sdk

    async def _do_remove_oauth(self, provider_key: str) -> None:
        self._sdk_servers.pop(provider_key, None)
        self._tool_names_by_server.pop(provider_key, None)
        self._tool_namespaces_by_server.pop(provider_key, None)
        self._wire_names_by_server.pop(provider_key, None)
        self._token_holders.pop(provider_key, None)
        stack = self._stacks.pop(provider_key, None)
        self.clear_policy_cache(provider_key)
        if stack is not None:
            await _aclose_silently(stack, close_timeout=self._close_timeout)

    async def _do_stop_all(self) -> None:
        for name in list(self._stacks):
            await _aclose_silently(self._stacks[name], close_timeout=self._close_timeout)
        self._stacks.clear()
        self._sdk_servers.clear()
        self._token_holders.clear()
        self._tool_names_by_server.clear()
        self._tool_namespaces_by_server.clear()
        self._wire_names_by_server.clear()


def _build_streamable_http(
    url: str,
    headers: dict[str, str],
    *,
    name: str,
    approval_policy: MCPApprovalPolicy | None = None,
    unauthorized_retry=None,
    token_holder: _TokenHolder | None = None,
) -> object:
    """Module-level builder so tests can patch this single symbol.

    The SDK defaults (5s read timeout, 5s HTTP timeout, no retries) are too tight
    for OAuth-backed remote servers like Google's early-access Gmail MCP endpoint,
    which is slow and intermittently returns 502s. Widen the per-call budget to 30s
    and retry transient failures a couple of times so a single hiccup surfaces to the
    model as a retry rather than a hard "technical error".
    """
    kwargs = {}
    unauthorized_tracker = _UnauthorizedTracker()
    if approval_policy is not None:
        kwargs["require_approval"] = lambda ctx, agent, tool: approval_policy.needs_approval(
            name, tool
        )
        kwargs["tool_filter"] = lambda filter_context, tool: approval_policy.filter_tool(name, tool)
    params = {"url": url, "headers": headers, "timeout": _REMOTE_HTTP_TIMEOUT_SEC}
    if unauthorized_retry is not None or token_holder is not None:
        params["httpx_client_factory"] = _tracking_httpx_client_factory(
            unauthorized_tracker, token_holder
        )

    sdk_server = MCPServerStreamableHttp(
        name=name,
        params=params,
        cache_tools_list=True,
        client_session_timeout_seconds=_REMOTE_HTTP_TIMEOUT_SEC,
        max_retry_attempts=_REMOTE_MAX_RETRY_ATTEMPTS,
        retry_backoff_seconds_base=_REMOTE_RETRY_BACKOFF_BASE,
        **kwargs,
    )
    if approval_policy is not None:
        _apply_runtime_policy_guard(
            sdk_server,
            name,
            approval_policy,
            unauthorized_retry=unauthorized_retry,
            unauthorized_detector=unauthorized_tracker.is_unauthorized_error,
            tool_call_timeout=_OAUTH_TOOL_CALL_TIMEOUT_SEC,
            unauthorized_retry_timeout=_OAUTH_TOOL_CALL_TIMEOUT_SEC,
        )
    return sdk_server


async def _aclose_silently(stack: AsyncExitStack, *, close_timeout: float = 10.0) -> None:
    """Close an AsyncExitStack, logging any error instead of propagating it.

    Catches BaseException because closing an anyio-based streamable-HTTP MCP
    connection can raise a stray CancelledError out of its task-group teardown.
    The lifecycle task is never externally cancelled, so such a cancellation is
    spurious here and must not be allowed to kill the owner task.
    """
    try:
        async with asyncio.timeout(close_timeout):
            await stack.aclose()
    except TimeoutError:
        _log.warning("timed out closing MCP exit stack after %.1fs", close_timeout)
    except BaseException:  # best-effort close; see docstring
        _log.exception("error closing exit stack")


def _build_sdk_server(
    cfg: MCPServerConfig,
    approval_policy: MCPApprovalPolicy | None = None,
) -> object:
    """Instantiate the right `agents.mcp` server class for `cfg`."""
    kwargs = {}
    if approval_policy is not None:
        name = cfg.name
        kwargs["require_approval"] = lambda ctx, agent, tool: approval_policy.needs_approval(
            name, tool
        )
        kwargs["tool_filter"] = lambda filter_context, tool: approval_policy.filter_tool(name, tool)

    if cfg.transport == "stdio":
        params: dict = {
            "command": cfg.command[0],
            "args": cfg.command[1:],
        }
        if cfg.env is not None:
            params["env"] = cfg.env
        sdk_server = MCPServerStdio(
            name=cfg.name,
            params=params,
            cache_tools_list=True,
            **kwargs,
        )
        if approval_policy is not None:
            _apply_runtime_policy_guard(sdk_server, cfg.name, approval_policy)
        return sdk_server
    if cfg.transport == "http":
        sdk_server = MCPServerStreamableHttp(
            name=cfg.name,
            params={
                "url": cfg.url,
                "headers": cfg.headers or {},
                "timeout": _REMOTE_HTTP_TIMEOUT_SEC,
            },
            cache_tools_list=True,
            client_session_timeout_seconds=_REMOTE_HTTP_TIMEOUT_SEC,
            max_retry_attempts=_REMOTE_MAX_RETRY_ATTEMPTS,
            retry_backoff_seconds_base=_REMOTE_RETRY_BACKOFF_BASE,
            **kwargs,
        )
        if approval_policy is not None:
            _apply_runtime_policy_guard(sdk_server, cfg.name, approval_policy)
        return sdk_server
    if cfg.transport == "sse":
        sdk_server = MCPServerSse(
            name=cfg.name,
            params={
                "url": cfg.url,
                "headers": cfg.headers or {},
                "timeout": _REMOTE_HTTP_TIMEOUT_SEC,
            },
            cache_tools_list=True,
            client_session_timeout_seconds=_REMOTE_HTTP_TIMEOUT_SEC,
            max_retry_attempts=_REMOTE_MAX_RETRY_ATTEMPTS,
            retry_backoff_seconds_base=_REMOTE_RETRY_BACKOFF_BASE,
            **kwargs,
        )
        if approval_policy is not None:
            _apply_runtime_policy_guard(sdk_server, cfg.name, approval_policy)
        return sdk_server
    raise ValueError(f"unsupported transport: {cfg.transport}")


def _apply_runtime_policy_guard(
    sdk_server: object,
    name: str,
    approval_policy: MCPApprovalPolicy,
    *,
    unauthorized_retry=None,
    unauthorized_detector=None,
    tool_call_timeout: float | None = None,
    unauthorized_retry_timeout: float | None = None,
) -> None:
    original_call_tool = sdk_server.call_tool  # type: ignore[attr-defined]
    sdk_server._jarvis_original_call_tool = original_call_tool  # type: ignore[attr-defined]

    async def guarded_call_tool(tool_name: str, arguments: dict | None, meta: dict | None = None):
        if await approval_policy.is_denied(name, tool_name):
            raise UserError(f"MCP tool '{tool_name}' on server '{name}' is denied by policy.")
        try:
            return await _call_tool_with_timeout(
                original_call_tool,
                tool_name,
                arguments,
                meta=meta,
                call_timeout=tool_call_timeout,
            )
        except BaseException as exc:
            detector = unauthorized_detector or _is_unauthorized_mcp_error
            if unauthorized_retry is None or not detector(exc):
                raise
            refreshed_server = await _unauthorized_retry_with_timeout(
                unauthorized_retry,
                retry_timeout=unauthorized_retry_timeout,
            )
            retry_call_tool = getattr(
                refreshed_server,
                "_jarvis_original_call_tool",
                refreshed_server.call_tool,  # type: ignore[attr-defined]
            )
            return await _call_tool_with_timeout(
                retry_call_tool,
                tool_name,
                arguments,
                meta=meta,
                call_timeout=tool_call_timeout,
            )

    sdk_server.call_tool = guarded_call_tool  # type: ignore[attr-defined]

    def get_needs_approval_for_tool(tool, agent):
        # Replace the SDK's normalization (which drops call arguments) with a
        # per-call gate that sees them, so sensitivity escalation and the
        # policy-decision audit event have argument-level context. The
        # `require_approval` kwarg stays wired as a fallback should a future
        # SDK stop consulting this instance attribute.
        async def _needs_approval(run_context, args, call_id):
            return await approval_policy.needs_approval(name, tool, arguments=args, call_id=call_id)

        return _needs_approval

    sdk_server._get_needs_approval_for_tool = get_needs_approval_for_tool  # type: ignore[attr-defined]


async def _call_tool_with_timeout(
    call_tool,
    tool_name: str,
    arguments: dict | None,
    *,
    meta: dict | None,
    call_timeout: float | None,
):
    if call_timeout is None:
        return await call_tool(tool_name, arguments, meta=meta)
    async with asyncio.timeout(call_timeout):
        return await call_tool(tool_name, arguments, meta=meta)


async def _unauthorized_retry_with_timeout(unauthorized_retry, *, retry_timeout: float | None):
    if retry_timeout is None:
        return await unauthorized_retry()
    async with asyncio.timeout(retry_timeout):
        return await unauthorized_retry()


def _is_unauthorized_mcp_error(exc: BaseException) -> bool:
    return "401" in str(exc) or "Unauthorized" in str(exc) or "invalid_token" in str(exc)


@dataclass
class _UnauthorizedTracker:
    seen_unauthorized: bool = False

    async def on_response(self, response: httpx.Response) -> None:
        if response.status_code == 401:
            self.seen_unauthorized = True

    def is_unauthorized_error(self, exc: BaseException) -> bool:
        if self.seen_unauthorized:
            self.seen_unauthorized = False
            return True
        return _is_unauthorized_mcp_error(exc)


def _tracking_httpx_client_factory(
    tracker: _UnauthorizedTracker, token_holder: _TokenHolder | None = None
):
    def factory(
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        client = create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)
        client.event_hooks["response"].append(tracker.on_response)
        if token_holder is not None:
            # Inject the *current* token on every request, so a refresh that
            # mutates the holder takes effect on this already-open connection
            # without any reconnect/swap.
            async def _inject_bearer(request: httpx.Request) -> None:
                request.headers["Authorization"] = f"Bearer {token_holder.get()}"

            client.event_hooks["request"].append(_inject_bearer)
        return client

    return factory


def _force_read_only(descriptor: MCPToolDescriptor) -> MCPToolDescriptor:
    """Apply a config-level `read_only: true` assertion to one tool descriptor.

    Only fills in a *missing* readOnlyHint; a server-provided hint (either
    value) and a destructive_hint=True both win over the operator assertion.
    """
    if descriptor.read_only_hint is not None or descriptor.destructive_hint is True:
        return descriptor
    return descriptor.model_copy(update={"read_only_hint": True})


async def _list_tools(sdk_server: object) -> list[MCPToolDescriptor]:
    """Ask the SDK server for its tools and map to our typed descriptors.

    Any temporary `tool_filter` mutation is deliberately scoped and restored in
    `finally`; the SDK server object is retained for later Agent use.
    """
    # This manager call catalogs the raw server tool set before an Agent run
    # context exists. The Agents SDK applies dynamic tool filters from
    # list_tools(), but those filters require run_context+agent, so temporarily
    # bypass filtering only for shadow-table discovery.
    tool_filter = getattr(sdk_server, "tool_filter", None)
    if tool_filter is not None:
        sdk_server.tool_filter = None  # type: ignore[attr-defined]
    try:
        raw_tools = await sdk_server.list_tools()  # type: ignore[attr-defined]
    finally:
        if tool_filter is not None:
            sdk_server.tool_filter = tool_filter  # type: ignore[attr-defined]
    descriptors: list[MCPToolDescriptor] = []
    for t in raw_tools:
        ann = getattr(t, "annotations", None)
        descriptors.append(
            MCPToolDescriptor(
                name=t.name,
                description=t.description or "",
                input_schema=dict(t.inputSchema) if t.inputSchema else {},
                read_only_hint=getattr(ann, "readOnlyHint", None) if ann else None,
                destructive_hint=getattr(ann, "destructiveHint", None) if ann else None,
            )
        )
    return descriptors
