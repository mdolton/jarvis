import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.core.types import ChannelKind, MessageRole
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import (
    ConversationRepo,
    MemoryEntryRepo,
    MemoryRecallRepo,
    MessageRepo,
)
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
        memory_repo = MemoryEntryRepo(s)
        first_memory = await memory_repo.create(
            conversation_id=conv.id,
            source_channel_kind=ChannelKind.DISCORD.value,
            source_channel_ref="user-1",
            summary="User prefers TDD and concise updates.",
            topics=["testing"],
            entities=["Jarvis"],
            evidence=[],
        )
        second_memory = await memory_repo.create(
            conversation_id=conv.id,
            source_channel_kind=ChannelKind.DISCORD.value,
            source_channel_ref="user-1",
            summary="Use live verification before claiming success.",
            topics=["verification"],
            entities=["Codex"],
            evidence=[],
        )
        await MemoryRecallRepo(s).record_many(
            conversation_id=conv.id,
            trigger_id=None,
            recalled=[
                {
                    "memory_entry_id": second_memory.id,
                    "score": 0.97,
                    "rank": 1,
                },
                {
                    "memory_entry_id": first_memory.id,
                    "score": 0.81,
                    "rank": 2,
                },
            ],
        )
        conv_id = conv.id
        no_recall_conv = await conv_repo.find_or_create_open(
            channel_kind=ChannelKind.DASHBOARD,
            channel_ref="dashboard-1",
            idle_timeout_sec=900,
        )
        await msg_repo.append(
            conversation_id=no_recall_conv.id,
            role=MessageRole.USER,
            content="no recall here",
        )
        no_recall_conv_id = no_recall_conv.id
        unavailable_recall_conv = await conv_repo.find_or_create_open(
            channel_kind=ChannelKind.DASHBOARD,
            channel_ref="dashboard-archived",
            idle_timeout_sec=900,
        )
        await msg_repo.append(
            conversation_id=unavailable_recall_conv.id,
            role=MessageRole.USER,
            content="show recall history",
        )
        archived_memory = await memory_repo.create(
            conversation_id=unavailable_recall_conv.id,
            source_channel_kind=ChannelKind.DASHBOARD.value,
            source_channel_ref="dashboard-archived",
            summary="This memory was archived later.",
            topics=["history"],
            entities=["Jarvis"],
            evidence=[],
        )
        await memory_repo.archive(archived_memory.id)
        await MemoryRecallRepo(s).record_many(
            conversation_id=unavailable_recall_conv.id,
            trigger_id=None,
            recalled=[
                {
                    "memory_entry_id": archived_memory.id,
                    "score": 0.55,
                    "rank": 1,
                },
                {
                    "memory_entry_id": None,
                    "score": 0.40,
                    "rank": 2,
                },
            ],
        )
        unavailable_recall_conv_id = unavailable_recall_conv.id

    # Build a mock-ish context with real session_factory.
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.session_factory = factory

    app = create_app(app_context=ctx)
    client = TestClient(app)
    yield ctx, client, conv_id, no_recall_conv_id, unavailable_recall_conv_id, factory

    await engine.dispose()


def test_conversations_list(ctx_and_client):
    _, client, _, _, _, _ = ctx_and_client
    resp = client.get("/conversations")
    assert resp.status_code == 200
    assert "discord" in resp.text.lower()


def test_conversation_detail(ctx_and_client):
    _, client, conv_id, _, _, _ = ctx_and_client
    resp = client.get(f"/conversations/{conv_id}")
    assert resp.status_code == 200
    assert "hello" in resp.text
    assert "hi there" in resp.text
    assert "Recalled memories" in resp.text
    assert "Rank 1" in resp.text
    assert "0.97" in resp.text
    assert "Use live verification before claiming success." in resp.text
    assert "Rank 2" in resp.text
    assert "0.81" in resp.text
    assert "User prefers TDD and concise updates." in resp.text


def test_conversation_detail_without_recall_still_shows_empty_state(ctx_and_client):
    _, client, _, no_recall_conv_id, _, _ = ctx_and_client
    resp = client.get(f"/conversations/{no_recall_conv_id}")

    assert resp.status_code == 200
    assert "no recall here" in resp.text
    assert "Recalled memories" in resp.text
    assert "No memories were recalled for this conversation." in resp.text


def test_conversation_detail_with_archived_or_missing_recalled_memory_shows_rows(ctx_and_client):
    _, client, _, _, unavailable_recall_conv_id, _ = ctx_and_client
    resp = client.get(f"/conversations/{unavailable_recall_conv_id}")

    assert resp.status_code == 200
    assert "show recall history" in resp.text
    assert "Recalled memories" in resp.text
    assert "Rank 1" in resp.text
    assert "0.55" in resp.text
    assert "This memory was archived later." in resp.text
    assert "archived" in resp.text
    assert "Rank 2" in resp.text
    assert "0.40" in resp.text
    assert "Memory no longer available" in resp.text
    assert "No memories were recalled for this conversation." not in resp.text
