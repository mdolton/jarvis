import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.core.types import ChannelKind, MessageRole
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import ConversationRepo, MessageRepo
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def ctx_and_client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    # Seed a conversation with messages.
    async with factory() as s:
        conv_repo = ConversationRepo(s)
        conv = await conv_repo.find_or_create_open(
            channel_kind=ChannelKind.DISCORD,
            channel_ref="user-1",
            idle_timeout_sec=900,
        )
        msg_repo = MessageRepo(s)
        await msg_repo.append(conversation_id=conv.id, role=MessageRole.USER, content="hello")
        await msg_repo.append(
            conversation_id=conv.id, role=MessageRole.ASSISTANT, content="hi there"
        )
        conv_id = conv.id

    # Build a mock-ish context with real session_factory.
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.session_factory = factory

    app = create_app(app_context=ctx)
    client = TestClient(app)
    yield ctx, client, conv_id, factory

    await engine.dispose()


def test_conversations_list(ctx_and_client):
    _, client, _, _ = ctx_and_client
    resp = client.get("/conversations")
    assert resp.status_code == 200
    assert "discord" in resp.text.lower()


def test_conversation_detail(ctx_and_client):
    _, client, conv_id, _ = ctx_and_client
    resp = client.get(f"/conversations/{conv_id}")
    assert resp.status_code == 200
    assert "hello" in resp.text
    assert "hi there" in resp.text
