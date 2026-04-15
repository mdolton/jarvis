import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from jarvis.persistence.db import Base, create_engine, session_factory


async def test_create_engine_returns_async_engine(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    assert isinstance(engine, AsyncEngine)
    await engine.dispose()


async def test_session_factory_yields_async_session(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    factory = session_factory(engine)
    async with factory() as session:
        assert isinstance(session, AsyncSession)
        result = await session.execute(text("select 1"))
        assert result.scalar() == 1
    await engine.dispose()


async def test_base_has_metadata():
    assert Base.metadata is not None
