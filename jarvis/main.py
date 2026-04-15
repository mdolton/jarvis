"""Application bootstrap — wires persistence, audit, config.

Later plans extend this to start channels, MCP manager, scheduler, and
the web dashboard. For now, bootstrap() returns an AppContext with the
infrastructure pieces initialized and a .shutdown() coroutine for teardown.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jarvis.audit.logger import AuditLogger
from jarvis.config.loader import LoadedConfig, load_config
from jarvis.persistence.db import Base, create_engine, session_factory

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class AppContext:
    config: LoadedConfig
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    audit: AuditLogger

    async def shutdown(self) -> None:
        await self.audit.stop()
        await self.engine.dispose()


async def bootstrap(*, config_dir: Path | str, db_url: str) -> AppContext:
    cfg = load_config(config_dir)
    logging.basicConfig(level=cfg.jarvis.log_level)

    engine = create_engine(db_url)
    # In Plan 1 we use metadata.create_all for simplicity in tests. Plan 6
    # will wire this through Alembic for the real deployment.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = session_factory(engine)

    audit = AuditLogger(session_factory=factory)
    await audit.start()

    _log.info("jarvis bootstrap complete")
    return AppContext(
        config=cfg,
        engine=engine,
        session_factory=factory,
        audit=audit,
    )
