import asyncio

import pytest

from jarvis.audit.logger import AuditLogger
from jarvis.core.types import AuditEvent, AuditEventType
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import AuditRepo


@pytest.fixture
async def engine_and_factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    yield engine, factory
    await engine.dispose()


async def test_emit_then_stop_persists_events(engine_and_factory):
    _, factory = engine_and_factory
    logger = AuditLogger(session_factory=factory, flush_interval_sec=0.02)
    await logger.start()
    try:
        for i in range(3):
            await logger.emit(AuditEvent(type=AuditEventType.LLM_REQUEST, payload={"i": i}))
    finally:
        await logger.stop()

    async with factory() as s:
        rows = await AuditRepo(s).recent(limit=10)
    assert len(rows) == 3


async def test_flush_interval_triggers_writes(engine_and_factory):
    _, factory = engine_and_factory
    logger = AuditLogger(session_factory=factory, flush_interval_sec=0.02)
    await logger.start()
    try:
        await logger.emit(AuditEvent(type=AuditEventType.LLM_REQUEST))
        await asyncio.sleep(0.1)  # longer than flush_interval
        async with factory() as s:
            rows = await AuditRepo(s).recent(limit=10)
        assert len(rows) == 1
    finally:
        await logger.stop()


async def test_batch_size_flushes_full_buffer(engine_and_factory):
    _, factory = engine_and_factory
    logger = AuditLogger(
        session_factory=factory,
        flush_interval_sec=0.02,
        batch_size=3,  # small batch; drain cap per flush
    )
    await logger.start()
    try:
        for i in range(10):
            await logger.emit(AuditEvent(type=AuditEventType.LLM_REQUEST, payload={"i": i}))
        # A few flush cycles at flush_interval 0.02 should drain all 10.
        await asyncio.sleep(0.2)
        async with factory() as s:
            rows = await AuditRepo(s).recent(limit=20)
        assert len(rows) == 10
    finally:
        await logger.stop()
