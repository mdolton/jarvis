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
import logging
from contextlib import AsyncExitStack

from agents.exceptions import UserError
from agents.mcp import MCPServerSse, MCPServerStdio, MCPServerStreamableHttp
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.config.schema import MCPServerConfig, MCPServersConfig
from jarvis.mcp.approval_policy import MCPApprovalPolicy
from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.oauth.catalog import OAUTH_CATALOG, assert_no_yaml_collision
from jarvis.oauth.crypto import decrypt_blob
from jarvis.oauth.store import OAuthCredentialsRepo
from jarvis.persistence.repositories import MCPServerRepo, MCPToolRepo

_log = logging.getLogger(__name__)


class MCPManager:
    def __init__(
        self,
        *,
        config: MCPServersConfig,
        session_factory: async_sessionmaker[AsyncSession],
        secrets_key: bytes | None = None,
        connect_timeout: float = 60.0,
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._secrets_key = secrets_key
        # Hard ceiling on a single connect+list_tools so a transiently
        # unresponsive remote can never wedge the serial lifecycle loop (and
        # thereby block Disconnect/remove, which run through the same task).
        self._connect_timeout = connect_timeout
        self._approval_policy = MCPApprovalPolicy(session_factory=session_factory)
        self._stacks: dict[str, AsyncExitStack] = {}
        self._sdk_servers: dict[str, object] = {}
        # All connection enter/exit happens on this single owner task.
        self._cmd_queue: asyncio.Queue[tuple[str, object, asyncio.Future]] | None = None
        self._loop_task: asyncio.Task | None = None

    async def start(self) -> None:
        """Connect to every enabled server. Failures are recorded, not raised."""
        assert_no_yaml_collision(s.name for s in self._config.servers)
        self._cmd_queue = asyncio.Queue()
        self._loop_task = asyncio.create_task(self._lifecycle_loop(), name="mcp-lifecycle")

        for server_cfg in self._config.servers:
            if not server_cfg.enabled:
                continue
            # connect failures are recorded inside the owner task, never raised.
            await self._submit("connect_cfg", server_cfg)

        if self._secrets_key is not None:
            await self._bootstrap_oauth_catalog()

    async def stop(self) -> None:
        if self._loop_task is None:
            return  # never started, or already stopped — idempotent
        try:
            await self._submit("shutdown", None)
        finally:
            await self._loop_task
            self._loop_task = None
            self._cmd_queue = None

    def agent_mcp_servers(self) -> list[object]:
        """Return the SDK server objects to pass into `Agent(mcp_servers=...)`."""
        return list(self._sdk_servers.values())

    def clear_policy_cache(self, server_name: str | None = None) -> None:
        if server_name is None:
            self._approval_policy.clear_cache()
        else:
            self._approval_policy.clear_server(server_name)

    async def replace_oauth_server(
        self, provider_key: str, *, url: str, headers: dict[str, str]
    ) -> None:
        """Build a new streamable-HTTP SDK server, verify list_tools, then atomically swap.

        If list_tools fails the new stack is closed and the old server remains active.
        Runs entirely on the owner task so enter/exit stay task-consistent.
        """
        await self._submit(
            "replace", {"provider_key": provider_key, "url": url, "headers": headers}
        )

    async def remove_oauth_server(self, provider_key: str) -> None:
        """Pop and close the stack/sdk_server entries for an OAuth provider."""
        await self._submit("remove", provider_key)

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
            if kind == "shutdown":
                return

    async def _bootstrap_oauth_catalog(self) -> None:
        """Attach any already-connected OAuth providers at startup."""
        async with self._session_factory() as session:
            rows = await OAuthCredentialsRepo(session).list_all()
        rows_by_key = {r.provider_key: r for r in rows}
        for key, entry in OAUTH_CATALOG.items():
            cred = rows_by_key.get(key)
            if cred is None or cred.status != "connected" or not cred.access_token_enc:
                continue
            access_token = decrypt_blob(cred.access_token_enc, self._secrets_key).decode()  # type: ignore[arg-type]
            try:
                await self.replace_oauth_server(
                    key,
                    url=entry.mcp_url,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            except Exception as e:
                _log.exception("failed to attach OAuth MCP %r at boot", key)
                async with self._session_factory() as session:
                    await OAuthCredentialsRepo(session).set_status(
                        key, status="needs_reauth", last_error=f"boot attach failed: {e}"
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
                await _aclose_silently(stack)
                raise

            self._stacks[cfg.name] = stack
            self._sdk_servers[cfg.name] = sdk_server

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

    async def _do_replace_oauth(
        self, *, provider_key: str, url: str, headers: dict[str, str]
    ) -> None:
        new_stack = AsyncExitStack()
        new_sdk = _build_streamable_http(
            url,
            headers,
            name=provider_key,
            approval_policy=self._approval_policy,
        )

        try:
            async with asyncio.timeout(self._connect_timeout):
                await new_stack.enter_async_context(new_sdk)
                tools = await _list_tools(new_sdk)
        except BaseException:
            # Connect/list_tools failed or timed out. Drop the half-open stack so
            # a hung connection can't linger, then surface the error to the caller
            # (the old server, if any, stays active).
            await _aclose_silently(new_stack)
            raise

        old_stack = self._stacks.get(provider_key)
        self._sdk_servers[provider_key] = new_sdk
        self._stacks[provider_key] = new_stack

        # Persist status/tools BEFORE closing the old connection. Closing an
        # anyio-based streamable-HTTP connection can emit a stray cancellation
        # out of its task-group teardown; keeping our DB write ahead of the close
        # means that write is never disturbed (which previously invalidated and
        # terminated the connection — "Exception terminating connection").
        async with self._session_factory() as session:
            srepo = MCPServerRepo(session)
            trepo = MCPToolRepo(session)
            row = await srepo.upsert(name=provider_key, transport="http")
            await srepo.set_status(row.id, status="connected", last_error=None)
            await trepo.replace_for_server(row.id, tools=tools)
        self.clear_policy_cache(provider_key)

        # Close the old connection LAST, on THIS (owner) task — the same task it
        # was opened on. _aclose_silently swallows any stray teardown error.
        if old_stack is not None:
            await _aclose_silently(old_stack)

    async def _do_remove_oauth(self, provider_key: str) -> None:
        self._sdk_servers.pop(provider_key, None)
        stack = self._stacks.pop(provider_key, None)
        self.clear_policy_cache(provider_key)
        if stack is not None:
            await _aclose_silently(stack)

    async def _do_stop_all(self) -> None:
        for name in list(self._stacks):
            await _aclose_silently(self._stacks[name])
        self._stacks.clear()
        self._sdk_servers.clear()


def _build_streamable_http(
    url: str,
    headers: dict[str, str],
    *,
    name: str,
    approval_policy: MCPApprovalPolicy | None = None,
) -> object:
    """Module-level builder so tests can patch this single symbol.

    The SDK defaults (5s read timeout, 5s HTTP timeout, no retries) are too tight
    for OAuth-backed remote servers like Google's early-access Gmail MCP endpoint,
    which is slow and intermittently returns 502s. Widen the per-call budget to 30s
    and retry transient failures a couple of times so a single hiccup surfaces to the
    model as a retry rather than a hard "technical error".
    """
    kwargs = {}
    if approval_policy is not None:
        kwargs["require_approval"] = lambda ctx, agent, tool: approval_policy.needs_approval(
            name, tool
        )
        kwargs["tool_filter"] = lambda filter_context, tool: approval_policy.filter_tool(name, tool)

    sdk_server = MCPServerStreamableHttp(
        name=name,
        params={"url": url, "headers": headers, "timeout": 30},
        cache_tools_list=True,
        client_session_timeout_seconds=30,
        max_retry_attempts=2,
        retry_backoff_seconds_base=1.0,
        **kwargs,
    )
    if approval_policy is not None:
        _apply_runtime_policy_guard(sdk_server, name, approval_policy)
    return sdk_server


async def _aclose_silently(stack: AsyncExitStack) -> None:
    """Close an AsyncExitStack, logging any error instead of propagating it.

    Catches BaseException because closing an anyio-based streamable-HTTP MCP
    connection can raise a stray CancelledError out of its task-group teardown.
    The lifecycle task is never externally cancelled, so such a cancellation is
    spurious here and must not be allowed to kill the owner task.
    """
    try:
        await stack.aclose()
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
            params={"url": cfg.url, "headers": cfg.headers or {}},
            cache_tools_list=True,
            **kwargs,
        )
        if approval_policy is not None:
            _apply_runtime_policy_guard(sdk_server, cfg.name, approval_policy)
        return sdk_server
    if cfg.transport == "sse":
        sdk_server = MCPServerSse(
            name=cfg.name,
            params={"url": cfg.url, "headers": cfg.headers or {}},
            cache_tools_list=True,
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
) -> None:
    original_call_tool = sdk_server.call_tool  # type: ignore[attr-defined]

    async def guarded_call_tool(tool_name: str, arguments: dict | None, meta: dict | None = None):
        if await approval_policy.is_denied(name, tool_name):
            raise UserError(f"MCP tool '{tool_name}' on server '{name}' is denied by policy.")
        return await original_call_tool(tool_name, arguments, meta=meta)

    sdk_server.call_tool = guarded_call_tool  # type: ignore[attr-defined]


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
