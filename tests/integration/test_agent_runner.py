"""AgentRunner integration tests using a fake model — we assert on the
event stream / DB effects, not on LLM output content.
"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest_asyncio
from agents import set_trace_processors
from agents.models.interface import Model

from jarvis.agents.runner import AgentRunner
from jarvis.audit.logger import AuditLogger
from jarvis.audit.tracer import JarvisTraceProcessor
from jarvis.config.schema import LLMConfig
from jarvis.core.types import (
    AuditEventType,
    ChannelKind,
    InvocationRequest,
    ManualTrigger,
    MessageRole,
    ScheduledTrigger,
)
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import (
    AuditRepo,
    MessageRepo,
)


class _FakeModel(Model):
    """A minimal Model that returns canned text with no tool calls."""

    def __init__(self, text: str = "hello from the fake") -> None:
        self._text = text

    async def get_response(self, *a, **kw):
        from agents.items import ModelResponse, Usage

        # Try the most common shape for a plain text assistant message.
        # ResponseOutputMessage is what the SDK uses for a final text reply.
        from openai.types.responses import ResponseOutputMessage, ResponseOutputText

        msg = ResponseOutputMessage(
            id="msg-1",
            type="message",
            role="assistant",
            status="completed",
            content=[
                ResponseOutputText(
                    type="output_text",
                    text=self._text,
                    annotations=[],
                ),
            ],
        )
        return ModelResponse(
            output=[msg],
            usage=Usage(),
            response_id=None,
        )

    async def stream_response(self, *a, **kw):
        # Required abstract method; not used by our tests.
        if False:
            yield None


@pytest_asyncio.fixture(loop_scope="function")
async def infra(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    audit = AuditLogger(session_factory=factory, flush_interval_sec=0.02)
    await audit.start()
    set_trace_processors([JarvisTraceProcessor(audit)])

    yield engine, factory, audit

    await audit.stop()
    await engine.dispose()


async def test_agent_runner_persists_user_and_assistant_messages(infra):
    _, factory, audit = infra
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        model=_FakeModel(text="response-under-test"),
    )

    req = InvocationRequest(
        trigger=ManualTrigger(user="mark", prompt="hello agent"),
    )
    result = await runner.run(req)

    assert "response-under-test" in result.final_output

    # DB state
    async with factory() as s:
        from sqlalchemy import select

        from jarvis.persistence.models import ConversationRow

        rows = (await s.execute(select(ConversationRow))).scalars().all()
        assert len(rows) == 1
        conv = rows[0]
        assert conv.channel_kind == ChannelKind.DASHBOARD.value

        msgs = await MessageRepo(s).history(conv.id)
        assert [m.role for m in msgs] == [
            MessageRole.USER.value,
            MessageRole.ASSISTANT.value,
        ]
        assert msgs[1].content == "response-under-test"

    # Routing fields are populated for the OutputRouter.
    assert result.channel_kind == ChannelKind.DASHBOARD
    assert result.channel_ref == "mark"


async def test_scheduled_trigger_prompt_includes_local_date_context(infra, monkeypatch):
    _, factory, audit = infra
    captured = {}

    async def fake_run(agent, prompt, run_config=None):
        captured["prompt"] = prompt
        return SimpleNamespace(final_output="ok")

    monkeypatch.setattr("jarvis.agents.runner.Runner.run", fake_run)

    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        model=_FakeModel(),
    )
    req = InvocationRequest(
        trigger=ScheduledTrigger(
            schedule_id="daily",
            prompt="Prepare my daily brief for today.",
            output_mode="dashboard_only",
            timezone="America/Los_Angeles",
            fired_at=datetime(2026, 6, 5, 1, 30, tzinfo=UTC),
        )
    )

    await runner.run(req)

    assert "Schedule context:" in captured["prompt"]
    assert "Timezone: America/Los_Angeles" in captured["prompt"]
    assert "Local date: 2026-06-04" in captured["prompt"]
    assert "Local time: 2026-06-04 18:30 PDT" in captured["prompt"]
    assert "Interpret relative dates like today" in captured["prompt"]
    assert captured["prompt"].endswith("Prepare my daily brief for today.")


async def test_agent_runner_writes_audit_events(infra):
    _, factory, audit = infra
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        model=_FakeModel(),
    )
    req = InvocationRequest(trigger=ManualTrigger(user="mark", prompt="hi"))
    await runner.run(req)

    await asyncio.sleep(0.15)  # let the audit logger drain

    async with factory() as s:
        events = await AuditRepo(s).recent(limit=50)
    types = {e.type for e in events}
    assert AuditEventType.TRIGGER_RECEIVED.value in types
