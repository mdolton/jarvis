import pytest_asyncio

from jarvis.oauth.catalog import seed_built_in_providers, unique_runtime_name
from jarvis.oauth.store import MCPConnectionRepo, MCPProviderRepo
from jarvis.persistence.db import Base, create_engine, session_factory


@pytest_asyncio.fixture(loop_scope="function")
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = session_factory(engine)
    async with f() as s:
        await seed_built_in_providers(s)
    yield f
    await engine.dispose()


async def test_unique_runtime_name_dedupes(factory):
    async with factory() as s:
        existing = {c.runtime_name for c in await MCPConnectionRepo(s).list_all()}
    assert unique_runtime_name(existing, "calendar", "Work") == "calendar:work"
    existing.add("calendar:work")
    assert unique_runtime_name(existing, "calendar", "Work") == "calendar:work-2"


async def test_has_connections(factory):
    async with factory() as s:
        assert await MCPProviderRepo(s).has_connections("calendar") is False
        await MCPConnectionRepo(s).create(provider_key="calendar", label="W",
                                          runtime_name="calendar:w")
    async with factory() as s:
        assert await MCPProviderRepo(s).has_connections("calendar") is True
