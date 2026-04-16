"""Tracer integration test: runs a real Agents SDK Runner with a scripted
mock LLM and asserts that the expected audit events land in the DB.
"""

import asyncio

import pytest
from agents import Agent, RunConfig, Runner, set_trace_processors

from jarvis.audit.logger import AuditLogger
from jarvis.audit.tracer import JarvisTraceProcessor
from jarvis.core.types import AuditEventType
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


async def test_tracer_emits_audit_events_for_agent_run(engine_and_factory):
    """A minimal Agent run should produce at least one audit event
    recording that the SDK trace was observed.
    """
    _, factory = engine_and_factory

    audit = AuditLogger(session_factory=factory, flush_interval_sec=0.02)
    await audit.start()
    try:
        tracer = JarvisTraceProcessor(audit)
        set_trace_processors([tracer])

        # Minimal response shape — exact class names depend on the SDK version.
        # We deliberately keep the fake model simple so we're testing the
        # tracer's handling of the SDK's trace stream, not the model response.
        from agents.models.interface import Model

        class _FakeModel(Model):
            async def get_response(self, *a, **kw):
                from agents.items import ModelResponse, Usage

                return ModelResponse(
                    output=[],
                    usage=Usage(),
                    response_id=None,
                )

            async def stream_response(self, *a, **kw):
                return
                yield  # make it an async generator

        agent = Agent(name="t", instructions="x", model=_FakeModel())
        await Runner.run(agent, "hi", run_config=RunConfig(workflow_name="test"))

        # Let the tracer's emits drain through AuditLogger.
        await asyncio.sleep(0.15)
    finally:
        await audit.stop()

    async with factory() as s:
        events = await AuditRepo(s).recent(limit=50)
    # At minimum: the tracer emits at least one audit event for this agent run.
    # Exactly which span types fire depends on SDK version; we just assert
    # that some LLM-related events were observed.
    types = {e.type for e in events}
    llm_types = {
        AuditEventType.LLM_REQUEST.value,
        AuditEventType.LLM_RESPONSE.value,
    }
    assert types & llm_types, f"no LLM audit events seen; got types {types}"
