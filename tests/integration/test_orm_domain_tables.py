from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.models import (
    ActionRow,
    DigestTemplateRow,
    MCPServerRow,
    MCPToolRow,
    ScheduleRow,
    SettingRow,
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


async def test_schedule_roundtrip(session):
    sched = ScheduleRow(
        id=uuid4(),
        name="morning email",
        description="summarize overnight email",
        cron_expr="0 8 * * *",
        timezone="America/Los_Angeles",
        prompt="summarize my unread email",
        output_mode="discord",
        notify_on_error=True,
        enabled=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(sched)
    await session.commit()

    result = await session.execute(select(ScheduleRow))
    found = result.scalar_one()
    assert found.cron_expr == "0 8 * * *"
    assert found.last_run_at is None


async def test_mcp_server_and_tool(session):
    server = MCPServerRow(
        id=uuid4(),
        name="gcal",
        transport="stdio",
        status="disconnected",
    )
    session.add(server)
    await session.flush()

    tool = MCPToolRow(
        id=uuid4(),
        server_id=server.id,
        name="list_events",
        description="List calendar events",
        input_schema={"type": "object", "properties": {}},
        read_only_hint=True,
        destructive_hint=False,
        policy_override=None,
    )
    session.add(tool)
    await session.commit()

    result = await session.execute(select(MCPToolRow))
    found = result.scalar_one()
    assert found.name == "list_events"
    assert found.read_only_hint is True


async def test_setting_key_value(session):
    s = SettingRow(key="idle_timeout_sec", value=1800)
    session.add(s)
    await session.commit()

    result = await session.execute(select(SettingRow))
    assert result.scalar_one().value == 1800


async def test_action_row_roundtrip(session):
    row = ActionRow(
        status="pending",
        decision=None,
        conversation_id=None,
        trigger_id=None,
        channel_kind="dashboard",
        channel_ref="dashboard",
        server_name="gmail",
        tool_name="send_email",
        tool_call_id="call-1",
        arguments_json={"to": "me@example.com"},
        run_state_json={"state": "serialized"},
        approval_item_json={"tool": "send_email"},
        model="test-model",
        created_at=datetime.now(UTC),
    )
    session.add(row)
    await session.commit()

    result = await session.execute(select(ActionRow))
    got = result.scalar_one()
    assert got.status == "pending"
    assert got.decision is None
    assert got.arguments_json == {"to": "me@example.com"}
    assert got.model == "test-model"


async def test_digest_template_row_roundtrip(session):
    row = DigestTemplateRow(
        key="daily-brief",
        name="Daily Brief",
        description="Morning summary",
        category="brief",
        prompt="Summarize today.",
        default_cron_expr="0 8 * * *",
        default_timezone="UTC",
        default_output_mode="discord",
        default_model=None,
        default_discord_user_id=None,
        built_in=True,
        enabled=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(row)
    await session.commit()

    result = await session.execute(select(DigestTemplateRow))
    got = result.scalar_one()
    assert got.key == "daily-brief"
    assert got.name == "Daily Brief"
    assert got.default_cron_expr == "0 8 * * *"
    assert got.built_in is True
    assert got.enabled is True
