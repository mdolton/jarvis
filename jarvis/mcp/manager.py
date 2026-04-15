"""MCPManager — owns lifecycle of all configured MCP servers.

- On start(): spin up each enabled MCP server (stdio, http, or sse),
  record status + discovered tools to the DB shadow tables.
- While running: the Agents SDK owns the actual connections and tool
  caching via `cache_tools_list=True`. We just keep the SDK server
  objects alive for the Agent to use.
- On stop(): async-close each SDK server cleanly.
"""

import logging
from contextlib import AsyncExitStack

from agents.mcp import MCPServerSse, MCPServerStdio, MCPServerStreamableHttp
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.config.schema import MCPServerConfig, MCPServersConfig
from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.persistence.repositories import MCPServerRepo, MCPToolRepo

_log = logging.getLogger(__name__)


class MCPManager:
    def __init__(
        self,
        *,
        config: MCPServersConfig,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._stack = AsyncExitStack()
        self._sdk_servers: list[object] = []  # opaque agents.mcp.MCPServer*

    async def start(self) -> None:
        """Connect to every enabled server. Failures are recorded, not raised."""
        for server_cfg in self._config.servers:
            if not server_cfg.enabled:
                continue
            try:
                await self._connect_one(server_cfg)
            except Exception as e:  # one bad server mustn't kill the rest
                _log.exception("failed to connect MCP server %r", server_cfg.name)
                await self._record_failure(server_cfg, e)

    async def stop(self) -> None:
        await self._stack.aclose()
        self._sdk_servers = []

    def agent_mcp_servers(self) -> list[object]:
        """Return the SDK server objects to pass into `Agent(mcp_servers=...)`."""
        return list(self._sdk_servers)

    async def _connect_one(self, cfg: MCPServerConfig) -> None:
        # Persist (or upsert) the server row up front so even a connection
        # failure later has something to attach the error to.
        async with self._session_factory() as session:
            row = await MCPServerRepo(session).upsert(name=cfg.name, transport=cfg.transport)
            server_id = row.id

        sdk_server = _build_sdk_server(cfg)
        await self._stack.enter_async_context(sdk_server)
        self._sdk_servers.append(sdk_server)

        # Enumerate tools — this confirms the connection actually works.
        tools = await _list_tools(sdk_server)

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
