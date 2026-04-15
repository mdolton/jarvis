from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.models import (
    AuditEventRow,
    ConversationRow,
    MessageRow,
    TriggerRow,
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


async def test_conversation_roundtrip(session):
    conv = ConversationRow(
        id=uuid4(),
        channel_kind="discord",
        channel_ref="user-1",
        started_at=datetime.now(UTC),
        last_activity_at=datetime.now(UTC),
        status="open",
    )
    session.add(conv)
    await session.commit()

    result = await session.execute(select(ConversationRow))
    found = result.scalar_one()
    assert found.channel_ref == "user-1"
    assert found.status == "open"


async def test_message_belongs_to_conversation(session):
    conv = ConversationRow(
        id=uuid4(),
        channel_kind="discord",
        channel_ref="user-1",
        started_at=datetime.now(UTC),
        last_activity_at=datetime.now(UTC),
        status="open",
    )
    session.add(conv)
    await session.flush()

    msg = MessageRow(
        id=uuid4(),
        conversation_id=conv.id,
        role="user",
        content="hi",
        created_at=datetime.now(UTC),
    )
    session.add(msg)
    await session.commit()

    result = await session.execute(select(MessageRow))
    found = result.scalar_one()
    assert found.content == "hi"
    assert found.conversation_id == conv.id


async def test_audit_event_allows_null_conversation(session):
    ev = AuditEventRow(
        id=uuid4(),
        conversation_id=None,
        trigger_id=None,
        type="config.reload_failed",
        payload={"error": "bad yaml"},
        created_at=datetime.now(UTC),
    )
    session.add(ev)
    await session.commit()

    result = await session.execute(select(AuditEventRow))
    assert result.scalar_one().type == "config.reload_failed"


async def test_trigger_roundtrip(session):
    trig = TriggerRow(
        id=uuid4(),
        kind="discord_message",
        source_ref="discord-msg-abc",
        created_at=datetime.now(UTC),
    )
    session.add(trig)
    await session.commit()

    result = await session.execute(select(TriggerRow))
    assert result.scalar_one().source_ref == "discord-msg-abc"


async def test_datetime_preserves_utc_timezone_through_roundtrip(session):
    """SQLite strips tzinfo by default — TZDateTime decorator must restore it."""
    original = datetime.now(UTC)
    conv = ConversationRow(
        id=uuid4(),
        channel_kind="discord",
        channel_ref="user-1",
        started_at=original,
        last_activity_at=original,
        status="open",
    )
    session.add(conv)
    await session.commit()

    # Reload from DB to force a fresh read.
    await session.refresh(conv)

    assert conv.started_at.tzinfo is UTC
    assert conv.last_activity_at.tzinfo is UTC
    # Values should be equal (UTC-equivalent) — not necessarily identical to the
    # microsecond since SQLite ISO string precision may differ by platform.
    assert (conv.started_at - original).total_seconds() < 0.001
