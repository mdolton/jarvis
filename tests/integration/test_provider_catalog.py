import pytest_asyncio

from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.persistence.db import Base, create_engine, session_factory


@pytest_asyncio.fixture(loop_scope="function")
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield session_factory(engine)
    await engine.dispose()


async def test_seed_then_catalog_reconstructs_provider_entry(factory):
    async with factory() as s:
        await seed_built_in_providers(s)
    catalog = ProviderCatalog(factory)
    cal = await catalog.get("calendar")
    assert cal.kind == "oauth"
    assert cal.auth_mode.value == "manual"
    assert cal.mcp_url.endswith("/mcp/v1")
    keys = {e.key for e in await catalog.list()}
    assert {"gmail", "calendar", "fastmail"} <= keys


async def test_seed_is_idempotent(factory):
    async with factory() as s:
        await seed_built_in_providers(s)
    async with factory() as s:
        await seed_built_in_providers(s)  # second run must not duplicate or raise
    catalog = ProviderCatalog(factory)
    assert len([e for e in await catalog.list() if e.key == "gmail"]) == 1
