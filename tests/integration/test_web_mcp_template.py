from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.oauth.catalog import ProviderCatalog, seed_built_in_providers
from jarvis.oauth.crypto import generate_key
from jarvis.oauth.store import MCPConnectionRepo
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MCPServerRepo
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        await seed_built_in_providers(s)
        await MCPConnectionRepo(s).create(
            provider_key="gmail", label="Personal", runtime_name="gmail:personal"
        )
        await MCPServerRepo(s).upsert(name="fs", transport="stdio")
    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.catalog = ProviderCatalog(factory)
    ctx.config = MagicMock()
    ctx.config.secrets_key = generate_key().encode()
    ctx.audit = MagicMock()
    ctx.audit.emit = AsyncMock()
    ctx.mcp_manager = MagicMock()
    app = create_app(app_context=ctx)
    yield TestClient(app)
    await engine.dispose()


def test_page_renders_management_forms(client):
    page = client.get("/mcp").text
    assert 'action="/mcp/providers/add"' in page
    assert 'action="/mcp/connections/add"' in page
    assert "Add connection" in page
    assert "Add provider" in page
    # existing connection + stdio toggle still present
    assert "Personal" in page
    assert 'action="/mcp/stdio/fs/disable"' in page


def test_provider_connections_are_collapsed_by_default(client):
    page = client.get("/mcp").text
    assert '<details class="conn-row">' in page
    assert "<summary" in page
    assert "Personal" in page

    details_start = page.index('<details class="conn-row">')
    summary_start = page.index("<summary", details_start)
    summary_end = page.index("</summary>", summary_start)
    assert "Personal" in page[summary_start:summary_end]
    assert "open" not in page[details_start:summary_start]
