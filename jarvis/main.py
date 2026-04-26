"""Application bootstrap — wires persistence, audit, config, MCP, agent, channels.

Returns an AppContext with every subsystem initialized. Later plans
(scheduler, dashboard) attach additional fields to this context.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx
from agents import set_trace_processors
from fastapi import FastAPI
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
from jarvis.core.types import ChannelKind
from jarvis.mcp.manager import MCPManager
from jarvis.oauth.flow import OAuthFlow
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.scheduler.scheduler import Scheduler
from jarvis.web.app import create_app as _create_web_app

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
    scheduler: Scheduler
    web_app: FastAPI
    oauth_flow: OAuthFlow
    oauth_http: httpx.AsyncClient

    async def shutdown(self) -> None:
        await self.scheduler.stop()
        # Stop adapters so no new triggers arrive while we tear down.
        for adapter in self.channel_adapters:
            try:
                await adapter.stop()
            except Exception:
                _log.exception("error stopping channel adapter")
        await self.mcp_manager.stop()
        await self.oauth_http.aclose()
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

    # OAuth.
    oauth_http = httpx.AsyncClient(timeout=30.0)
    oauth_flow = OAuthFlow(
        http_client=oauth_http,
        session_factory=factory,
        base_url=cfg.base_url,
        secrets_key=cfg.secrets_key,
    )

    # MCP.
    mcp_manager = MCPManager(
        config=cfg.mcp_servers,
        session_factory=factory,
        secrets_key=cfg.secrets_key,
    )
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

    # Find the discord adapter (if any) for scheduled output routing.
    discord_adapter = next(
        (a for a in channel_adapters if a.kind == ChannelKind.DISCORD.value),
        None,
    )

    # Scheduler.
    scheduler = Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=cfg.jarvis.llm,
        mcp_servers=mcp_manager.agent_mcp_servers(),
        discord_adapter=discord_adapter,
        idle_timeout_sec=cfg.jarvis.idle_timeout_sec,
        max_concurrent=cfg.jarvis.max_concurrent_agents,
        oauth_flow=oauth_flow,
        mcp_manager=mcp_manager,
    )
    await scheduler.start()

    # Web dashboard.
    web_app = _create_web_app(app_context=None)

    _log.info("jarvis bootstrap complete")
    ctx = AppContext(
        config=cfg,
        engine=engine,
        session_factory=factory,
        audit=audit,
        mcp_manager=mcp_manager,
        agent_runner=agent_runner,
        dispatcher=dispatcher,
        channel_adapters=channel_adapters,
        output_router=output_router,
        scheduler=scheduler,
        web_app=web_app,
        oauth_flow=oauth_flow,
        oauth_http=oauth_http,
    )
    # Wire the full context into the web app now that it exists.
    web_app.state.ctx = ctx
    return ctx
