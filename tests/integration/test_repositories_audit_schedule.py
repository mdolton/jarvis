from datetime import UTC, datetime

import pytest

from jarvis.core.types import AuditEvent, AuditEventType
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import AuditRepo, ScheduleRepo


@pytest.fixture
async def session(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_audit_write_and_query_by_type(session):
    repo = AuditRepo(session)
    await repo.write_many(
        [
            AuditEvent(type=AuditEventType.TRIGGER_RECEIVED, payload={"x": 1}),
            AuditEvent(type=AuditEventType.LLM_REQUEST, payload={"y": 2}),
            AuditEvent(type=AuditEventType.TRIGGER_RECEIVED, payload={"x": 3}),
        ]
    )
    rows = await repo.recent(types=[AuditEventType.TRIGGER_RECEIVED], limit=10)
    assert len(rows) == 2


async def test_audit_recent_respects_limit_and_order(session):
    repo = AuditRepo(session)
    events = [AuditEvent(type=AuditEventType.LLM_REQUEST, payload={"i": i}) for i in range(5)]
    await repo.write_many(events)
    rows = await repo.recent(limit=3)
    assert len(rows) == 3
    # Newest first
    assert rows[0].created_at >= rows[-1].created_at


async def test_schedule_crud(session):
    repo = ScheduleRepo(session)
    created = await repo.create(
        name="morning",
        description="test",
        cron_expr="0 8 * * *",
        timezone="UTC",
        prompt="summarize",
        output_mode="discord",
        notify_on_error=True,
        enabled=True,
    )
    assert created.id is not None

    found = await repo.get(created.id)
    assert found is not None
    assert found.name == "morning"

    all_enabled = await repo.list_enabled()
    assert len(all_enabled) == 1

    await repo.set_enabled(created.id, False)
    all_enabled = await repo.list_enabled()
    assert len(all_enabled) == 0


async def test_schedule_record_run(session):
    repo = ScheduleRepo(session)
    sched = await repo.create(
        name="s",
        description="",
        cron_expr="* * * * *",
        timezone="UTC",
        prompt="go",
        output_mode="discord",
        notify_on_error=True,
        enabled=True,
    )
    ts = datetime.now(UTC)
    await repo.record_run(sched.id, at=ts, status="success")
    refreshed = await repo.get(sched.id)
    assert refreshed.last_run_status == "success"
    assert refreshed.last_run_at == ts


async def test_audit_recent_as_events_returns_pydantic(session):
    repo = AuditRepo(session)
    await repo.write_many(
        [
            AuditEvent(type=AuditEventType.TRIGGER_RECEIVED, payload={"x": 1}),
            AuditEvent(type=AuditEventType.LLM_REQUEST, payload={"y": 2}),
        ]
    )
    events = await repo.recent_as_events(limit=10)
    assert len(events) == 2
    # Returned as AuditEvent Pydantic instances with typed enums.
    assert all(isinstance(e, AuditEvent) for e in events)
    assert all(isinstance(e.type, AuditEventType) for e in events)
    # Newest first ordering preserved.
    assert events[0].created_at >= events[-1].created_at


async def test_schedule_list_all(session):
    repo = ScheduleRepo(session)
    await repo.create(
        name="a",
        description="",
        cron_expr="* * * * *",
        timezone="UTC",
        prompt="x",
        output_mode="discord",
        notify_on_error=True,
        enabled=True,
    )
    await repo.create(
        name="b",
        description="",
        cron_expr="* * * * *",
        timezone="UTC",
        prompt="y",
        output_mode="discord",
        notify_on_error=True,
        enabled=False,
    )
    all_schedules = await repo.list_all()
    assert len(all_schedules) == 2
    assert {s.name for s in all_schedules} == {"a", "b"}


async def test_schedule_update(session):
    repo = ScheduleRepo(session)
    sched = await repo.create(
        name="orig",
        description="d",
        cron_expr="0 8 * * *",
        timezone="UTC",
        prompt="go",
        output_mode="discord",
        notify_on_error=True,
        enabled=True,
    )
    await repo.update(
        sched.id,
        name="renamed",
        cron_expr="0 9 * * *",
        prompt="new prompt",
    )
    refreshed = await repo.get(sched.id)
    assert refreshed.name == "renamed"
    assert refreshed.cron_expr == "0 9 * * *"
    assert refreshed.prompt == "new prompt"
    assert refreshed.output_mode == "discord"


async def test_schedule_delete(session):
    repo = ScheduleRepo(session)
    sched = await repo.create(
        name="to-delete",
        description="",
        cron_expr="* * * * *",
        timezone="UTC",
        prompt="x",
        output_mode="discord",
        notify_on_error=True,
        enabled=True,
    )
    await repo.delete(sched.id)
    assert await repo.get(sched.id) is None


async def test_schedule_create_persists_model(tmp_path):
    from jarvis.persistence.db import Base, create_engine, session_factory
    from jarvis.persistence.repositories import ScheduleRepo

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    async with factory() as s:
        repo = ScheduleRepo(s)
        with_model = await repo.create(
            name="a",
            description="",
            cron_expr="* * * * *",
            timezone="UTC",
            prompt="p",
            output_mode="discord",
            notify_on_error=True,
            enabled=True,
            model="gpt-4o",
        )
        without_model = await repo.create(
            name="b",
            description="",
            cron_expr="* * * * *",
            timezone="UTC",
            prompt="p",
            output_mode="discord",
            notify_on_error=True,
            enabled=True,
        )

    async with factory() as s:
        repo = ScheduleRepo(s)
        a = await repo.get(with_model.id)
        b = await repo.get(without_model.id)
        assert a.model == "gpt-4o"
        assert b.model is None

    await engine.dispose()


async def test_schedule_create_persists_discord_user_id(session):
    repo = ScheduleRepo(session)
    with_user = await repo.create(
        name="discord",
        description="",
        cron_expr="0 8 * * *",
        timezone="UTC",
        prompt="send it",
        output_mode="discord",
        notify_on_error=True,
        enabled=True,
        discord_user_id="111",
    )
    without_user = await repo.create(
        name="dashboard",
        description="",
        cron_expr="0 9 * * *",
        timezone="UTC",
        prompt="keep it",
        output_mode="dashboard_only",
        notify_on_error=True,
        enabled=True,
    )

    found_with = await repo.get(with_user.id)
    found_without = await repo.get(without_user.id)

    assert found_with.discord_user_id == "111"
    assert found_without.discord_user_id is None
