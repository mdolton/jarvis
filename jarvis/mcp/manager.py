"""MCPManager — owns lifecycle of all configured MCP servers.

- On start(): spin up each enabled MCP server (stdio, http, or sse),
  record status + discovered tools to the DB shadow tables.
- While running: the Agents SDK owns the actual connections and tool
  caching via `cache_tools_list=True`. We just keep the SDK server
  objects alive for the Agent to use.
- On stop(): async-close each SDK server cleanly.
"""

import asyncio
import logging
from contextlib import AsyncExitStack

from agents.mcp import MCPServerSse, MCPServerStdio, MCPServerStreamableHttp
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.config.schema import MCPServerConfig, MCPServersConfig
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
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._secrets_key = secrets_key
        self._stacks: dict[str, AsyncExitStack] = {}
        self._sdk_servers: dict[str, object] = {}

    async def start(self) -> None:
        """Connect to every enabled server. Failures are recorded, not raised."""
        assert_no_yaml_collision(s.name for s in self._config.servers)
        for server_cfg in self._config.servers:
            if not server_cfg.enabled:
                continue
            try:
                await self._connect_one(server_cfg)
            except Exception as e:  # one bad server mustn't kill the rest
                _log.exception("failed to connect MCP server %r", server_cfg.name)
                await self._record_failure(server_cfg, e)

        if self._secrets_key is not None:
            await self._bootstrap_oauth_catalog()

    async def stop(self) -> None:
        for name in list(self._stacks):
            try:
                await self._stacks[name].aclose()
            except Exception:
                _log.exception("error closing MCP server stack %r", name)
        self._stacks.clear()
        self._sdk_servers.clear()

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

    def agent_mcp_servers(self) -> list[object]:
        """Return the SDK server objects to pass into `Agent(mcp_servers=...)`."""
        return list(self._sdk_servers.values())

    async def _connect_one(self, cfg: MCPServerConfig) -> None:
        async with self._session_factory() as session:
            row = await MCPServerRepo(session).upsert(name=cfg.name, transport=cfg.transport)
            server_id = row.id

        stack = AsyncExitStack()
        sdk_server = _build_sdk_server(cfg)
        await stack.enter_async_context(sdk_server)

        try:
            tools = await _list_tools(sdk_server)
        except Exception:
            # Eagerly close on list_tools failure so we don't keep a half-broken server.
            await stack.aclose()
            raise

        self._stacks[cfg.name] = stack
        self._sdk_servers[cfg.name] = sdk_server

        async with self._session_factory() as session:
            srepo = MCPServerRepo(session)
            trepo = MCPToolRepo(session)
            await srepo.set_status(server_id, status="connected", last_error=None)
            await trepo.replace_for_server(server_id, tools=tools)

    async def _record_failure(self, cfg: MCPServerConfig, exc: Exception) -> None:
        async with self._session_factory() as session:
            repo = MCPServerRepo(session)
            row = await repo.upsert(name=cfg.name, transport=cfg.transport)
            await repo.set_status(row.id, status="error", last_error=f"{type(exc).__name__}: {exc}")

    async def replace_oauth_server(
        self, provider_key: str, *, url: str, headers: dict[str, str]
    ) -> None:
        """Build a new streamable-HTTP SDK server, verify list_tools, then atomically swap.

        If list_tools fails the new stack is closed and the old server remains active.
        The old stack (if any) is closed on the next event-loop tick via asyncio.create_task.
        """
        new_stack = AsyncExitStack()
        new_sdk = _build_streamable_http(url, headers, name=provider_key)
        await new_stack.enter_async_context(new_sdk)

        try:
            tools = await _list_tools(new_sdk)
        except Exception:
            await new_stack.aclose()
            raise

        old_stack = self._stacks.get(provider_key)
        self._sdk_servers[provider_key] = new_sdk
        self._stacks[provider_key] = new_stack

        if old_stack is not None:
            asyncio.create_task(_aclose_silently(old_stack))  # noqa: RUF006

        async with self._session_factory() as session:
            srepo = MCPServerRepo(session)
            trepo = MCPToolRepo(session)
            row = await srepo.upsert(name=provider_key, transport="http")
            await srepo.set_status(row.id, status="connected", last_error=None)
            await trepo.replace_for_server(row.id, tools=tools)

    async def remove_oauth_server(self, provider_key: str) -> None:
        """Pop and close the stack/sdk_server entries for an OAuth provider."""
        self._sdk_servers.pop(provider_key, None)
        stack = self._stacks.pop(provider_key, None)
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                _log.exception("error closing oauth server stack %r", provider_key)


def _build_streamable_http(url: str, headers: dict[str, str], *, name: str) -> object:
    """Module-level builder so tests can patch this single symbol.

    The SDK defaults (5s read timeout, 5s HTTP timeout, no retries) are too tight
    for OAuth-backed remote servers like Google's early-access Gmail MCP endpoint,
    which is slow and intermittently returns 502s. Widen the per-call budget to 30s
    and retry transient failures a couple of times so a single hiccup surfaces to the
    model as a retry rather than a hard "technical error".
    """
    return MCPServerStreamableHttp(
        name=name,
        params={"url": url, "headers": headers, "timeout": 30},
        cache_tools_list=True,
        client_session_timeout_seconds=30,
        max_retry_attempts=2,
        retry_backoff_seconds_base=1.0,
    )


async def _aclose_silently(stack: AsyncExitStack) -> None:
    """Close an AsyncExitStack, logging any error instead of propagating it."""
    try:
        await stack.aclose()
    except Exception:
        _log.exception("error closing exit stack")


def _build_sdk_server(cfg: MCPServerConfig) -> object:
    """Instantiate the right `agents.mcp` server class for `cfg`."""
    if cfg.transport == "stdio":
        params: dict = {
            "command": cfg.command[0],
            "args": cfg.command[1:],
        }
        if cfg.env is not None:
            params["env"] = cfg.env
        return MCPServerStdio(
            name=cfg.name,
            params=params,
            cache_tools_list=True,
        )
    if cfg.transport == "http":
        return MCPServerStreamableHttp(
            name=cfg.name,
            params={"url": cfg.url, "headers": cfg.headers or {}},
            cache_tools_list=True,
        )
    if cfg.transport == "sse":
        return MCPServerSse(
            name=cfg.name,
            params={"url": cfg.url, "headers": cfg.headers or {}},
            cache_tools_list=True,
        )
    raise ValueError(f"unsupported transport: {cfg.transport}")


async def _list_tools(sdk_server: object) -> list[MCPToolDescriptor]:
    """Ask the SDK server for its tools and map to our typed descriptors."""
    raw_tools = await sdk_server.list_tools()  # type: ignore[attr-defined]
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
