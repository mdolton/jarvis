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
