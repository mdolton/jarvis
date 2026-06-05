from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from jarvis.agents.runner import AgentRunner
from jarvis.audit.logger import AuditLogger
from jarvis.config.schema import LLMConfig
from jarvis.core.types import AuditEventType, InvocationRequest, ManualTrigger
from jarvis.memory.types import MemoryContext
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import ActionRepo, AuditRepo, MessageRepo


class _RecordingMemoryService:
    def __init__(self) -> None:
        self.build_calls = []
        self.summarize_calls = []

    async def build_context(self, **kwargs):
        self.build_calls.append(kwargs)
        return MemoryContext(
            preferences=[],
            recalled=[],
            recall_available=False,
            error=None,
        )

    async def summarize_run(self, **kwargs):
        self.summarize_calls.append(kwargs)


class _FakeRunState:
    def to_json(self):
        return {"state": "serialized"}


class _FakeResult:
    def __init__(self) -> None:
        self.final_output = None
        self.interruptions = [
            SimpleNamespace(
                raw_item=SimpleNamespace(
                    name="send_email",
                    call_id="call-1",
                    arguments='{"to":"me@example.com"}',
                    server_label="gmail",
                )
            )
        ]

    def to_state(self):
        return _FakeRunState()


@pytest_asyncio.fixture(loop_scope="function")
async def infra(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    audit = AuditLogger(session_factory=factory, flush_interval_sec=0.02)
    await audit.start()
    yield factory, audit
    await audit.stop()
    await engine.dispose()


async def test_runner_creates_pending_action_on_tool_approval(monkeypatch, infra):
    factory, audit = infra
    run_mock = AsyncMock(return_value=_FakeResult())
    monkeypatch.setattr("jarvis.agents.runner.Runner.run", run_mock)
    memory_service = _RecordingMemoryService()

    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        model_provider=lambda: "m",
        memory_service=memory_service,
    )

    result = await runner.run(
        InvocationRequest(trigger=ManualTrigger(user="dashboard", prompt="send it"))
    )

    assert "Action approval required" in result.final_output

    async with factory() as s:
        actions = await ActionRepo(s).list_pending()
        assert len(actions) == 1
        assert actions[0].server_name == "gmail"
        assert actions[0].tool_name == "send_email"
        assert actions[0].arguments_json == {"to": "me@example.com"}
        assert actions[0].run_state_json == {"state": "serialized"}
        msgs = await MessageRepo(s).history(actions[0].conversation_id)
        assert msgs[-1].content == result.final_output

    assert memory_service.summarize_calls == []
    assert len(memory_service.build_calls) == 1

    await audit.stop()

    async with factory() as s:
        events = await AuditRepo(s).recent(types=[AuditEventType.ACTION_CREATED], limit=10)
        assert len(events) == 1
        assert events[0].payload["server_name"] == "gmail"
        assert events[0].payload["tool_name"] == "send_email"


async def test_runner_rolls_back_pending_action_when_assistant_message_fails(monkeypatch, infra):
    factory, audit = infra
    monkeypatch.setattr(
        "jarvis.agents.runner.Runner.run",
        AsyncMock(return_value=_FakeResult()),
    )

    async def fail_append_no_commit(self, **kwargs):
        raise RuntimeError("message append failed")

    monkeypatch.setattr(
        "jarvis.agents.runner.MessageRepo.append_no_commit",
        fail_append_no_commit,
        raising=False,
    )

    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        model_provider=lambda: "m",
    )

    with pytest.raises(RuntimeError, match="message append failed"):
        await runner.run(
            InvocationRequest(trigger=ManualTrigger(user="dashboard", prompt="send it"))
        )

    async with factory() as s:
        actions = await ActionRepo(s).list_pending()
        assert actions == []
