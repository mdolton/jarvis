import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from jarvis.core.types import ChannelKind, MessageRole
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import (
    ConversationRepo,
    MessageRepo,
    TriggerRepo,
)


@pytest.fixture
async def session(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_conversation_find_or_create_creates_new(session):
    repo = ConversationRepo(session)
    conv = await repo.find_or_create_open(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        idle_timeout_sec=900,
    )
    assert conv.status == "open"


async def test_conversation_find_or_create_returns_existing_if_active(session):
    repo = ConversationRepo(session)
    c1 = await repo.find_or_create_open(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        idle_timeout_sec=900,
    )
    c2 = await repo.find_or_create_open(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        idle_timeout_sec=900,
    )
    assert c1.id == c2.id


async def test_conversation_find_or_create_opens_new_after_idle_timeout(session):
    repo = ConversationRepo(session)
    c1 = await repo.find_or_create_open(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        idle_timeout_sec=900,
    )
    # Force c1 to look stale.
    c1.last_activity_at = datetime.now(UTC) - timedelta(seconds=1000)
    await session.commit()

    c2 = await repo.find_or_create_open(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        idle_timeout_sec=900,
    )
    assert c1.id != c2.id
    await session.refresh(c1)
    assert c1.status == "closed"


async def test_scheduled_trigger_always_fresh_conversation(session):
    repo = ConversationRepo(session)
    c1 = await repo.find_or_create_open(
        channel_kind=ChannelKind.SCHEDULED,
        channel_ref="schedule-abc",
        idle_timeout_sec=0,  # 0 means: always fresh
    )
    c2 = await repo.find_or_create_open(
        channel_kind=ChannelKind.SCHEDULED,
        channel_ref="schedule-abc",
        idle_timeout_sec=0,
    )
    assert c1.id != c2.id


async def test_message_repo_appends(session):
    conv_repo = ConversationRepo(session)
    conv = await conv_repo.find_or_create_open(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        idle_timeout_sec=900,
    )

    msg_repo = MessageRepo(session)
    await msg_repo.append(conversation_id=conv.id, role=MessageRole.USER, content="hello")
    await msg_repo.append(conversation_id=conv.id, role=MessageRole.ASSISTANT, content="hi there")

    history = await msg_repo.history(conv.id)
    assert [m.role for m in history] == ["user", "assistant"]
    assert [m.content for m in history] == ["hello", "hi there"]


async def test_trigger_repo_records(session):
    repo = TriggerRepo(session)
    trig = await repo.record(kind="discord_message", source_ref="discord-msg-abc")
    assert trig.kind == "discord_message"
    assert trig.source_ref == "discord-msg-abc"


async def test_message_append_touches_conversation(session):
    conv_repo = ConversationRepo(session)
    conv = await conv_repo.find_or_create_open(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        idle_timeout_sec=900,
    )
    original_activity = conv.last_activity_at

    # Simulate the conversation aging.
    await asyncio.sleep(0.02)

    msg_repo = MessageRepo(session)
    await msg_repo.append(
        conversation_id=conv.id,
        role=MessageRole.USER,
        content="hello",
    )

    await session.refresh(conv)
    assert conv.last_activity_at > original_activity
