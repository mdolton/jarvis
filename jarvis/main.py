"""Application bootstrap — wires persistence, audit, config, MCP, agent, channels.

Returns an AppContext with every subsystem initialized. Later plans
(scheduler, dashboard) attach additional fields to this context.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from agents import set_trace_processors
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jarvis.agents.llm_client import build_llm_client, install_as_default
from jarvis.agents.runner import AgentRunner
from jarvis.audit.logger import AuditLogger
from jarvis.audit.tracer import JarvisTraceProcessor
from jarvis.channels.base import ChannelAdapter
from jarvis.channels.discord_adapter import DiscordAdapter
from jarvis.config.loader import LoadedConfig, load_config
from jarvis.core.dispatcher import TriggerDispatcher
from jarvis.core.output_router import OutputRouter
from jarvis.mcp.manager import MCPManager
from jarvis.persistence.db import Base, create_engine, session_factory

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class AppContext:
    config: LoadedConfig
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    audit: AuditLogger
    mcp_manager: MCPManager
    agent_runner: AgentRunner
    dispatcher: TriggerDispatcher
    channel_adapters: list[ChannelAdapter]
    output_router: OutputRouter

    async def shutdown(self) -> None:
        # Stop adapters first so no new triggers arrive while we tear down.
        for adapter in self.channel_adapters:
            try:
                await adapter.stop()
            except Exception:
                _log.exception("error stopping channel adapter")
        await self.mcp_manager.stop()
        await self.audit.stop()
        await self.engine.dispose()


async def bootstrap(*, config_dir: Path | str, db_url: str) -> AppContext:
    cfg = load_config(config_dir)
    logging.basicConfig(level=cfg.jarvis.log_level)

    # DB.
    engine = create_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    # Audit.
    audit = AuditLogger(session_factory=factory)
    await audit.start()

    # Install the tracer BEFORE any Runner.run.
    set_trace_processors([JarvisTraceProcessor(audit)])

    # LLM.
    llm_client = build_llm_client(cfg.jarvis.llm)
    install_as_default(llm_client)

    # MCP.
    mcp_manager = MCPManager(config=cfg.mcp_servers, session_factory=factory)
    await mcp_manager.start()

    # Agent runner.
    agent_runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers=mcp_manager.agent_mcp_servers(),
        llm_config=cfg.jarvis.llm,
        idle_timeout_sec=cfg.jarvis.idle_timeout_sec,
    )

    # Channel adapters (currently just Discord).
    channel_adapters: list[ChannelAdapter] = []
    if cfg.channels.discord is not None and cfg.channels.discord.enabled:
        discord_adapter = DiscordAdapter(
            token=cfg.channels.discord.token,
            allowed_user_ids=set(cfg.channels.discord.allowed_user_ids),
        )
        channel_adapters.append(discord_adapter)

    # Output router knows how to send replies through any of the adapters.
    output_router = OutputRouter(adapters=channel_adapters)

    # Dispatcher gets the router so channel-triggered runs auto-reply.
    dispatcher = TriggerDispatcher(
        runner=agent_runner,
        audit=audit,
        output_router=output_router,
        max_concurrent=cfg.jarvis.max_concurrent_agents,
    )

    # Now that the dispatcher exists, start each adapter.
    for adapter in channel_adapters:
        await adapter.start(dispatcher)

    _log.info("jarvis bootstrap complete")
    return AppContext(
        config=cfg,
        engine=engine,
        session_factory=factory,
        audit=audit,
        mcp_manager=mcp_manager,
        agent_runner=agent_runner,
        dispatcher=dispatcher,
        channel_adapters=channel_adapters,
        output_router=output_router,
    )
