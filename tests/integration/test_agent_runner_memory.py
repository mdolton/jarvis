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
from jarvis.core.types import InvocationRequest, ManualTrigger, MessageRole, ScheduledTrigger
from jarvis.memory.types import MemoryContext, RecalledMemory
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MessageRepo


class _FakeModel(Model):
    def __init__(self, text: str = "hello from the fake") -> None:
        self._text = text

    async def get_response(self, *a, **kw):
        from agents.items import ModelResponse, Usage
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

    yield factory, audit

    await audit.stop()
    await engine.dispose()


class _FakeMemoryService:
    def __init__(self, session_factory):
        self._session_factory = session_factory
        self.build_calls = []
        self.summarize_calls = []

    async def build_context(self, *, conversation_id, trigger_id, prompt):
        self.build_calls.append(
            {
                "conversation_id": conversation_id,
                "trigger_id": trigger_id,
                "prompt": prompt,
            }
        )
        return MemoryContext(
            preferences=["Prefer concise answers."],
            recalled=[
                RecalledMemory(
                    memory_entry_id=conversation_id,
                    summary="The user is working on the memory rollout.",
                    topics=["jarvis"],
                    entities=["Task 6"],
                    evidence=[{"kind": "note", "label": "Context", "content": "Task 6 scope"}],
                    score=0.8,
                    rank=1,
                )
            ],
            recall_available=True,
            error=None,
        )

    async def summarize_run(self, **kwargs):
        async with self._session_factory() as session:
            history = await MessageRepo(session).history(kwargs["conversation_id"])
        self.summarize_calls.append(
            {
                **kwargs,
                "assistant_persisted": history[-1].role == MessageRole.ASSISTANT.value
                and history[-1].content == kwargs["assistant_output"],
            }
        )


async def test_agent_runner_injects_memory_context_and_summarizes(infra, monkeypatch):
    factory, audit = infra
    captured = {}
    summary_tasks = []
    real_create_task = asyncio.create_task

    async def fake_run(agent, prompt, run_config=None):
        captured["prompt"] = prompt
        return SimpleNamespace(final_output="done")

    def fake_create_task(coro, *, name=None):
        task = real_create_task(coro, name=name)
        summary_tasks.append(task)
        return task

    monkeypatch.setattr("jarvis.agents.runner.Runner.run", fake_run)
    monkeypatch.setattr("jarvis.agents.runner.asyncio.create_task", fake_create_task)
    memory_service = _FakeMemoryService(factory)
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        model=_FakeModel(),
        memory_service=memory_service,
    )

    await runner.run(InvocationRequest(trigger=ManualTrigger(user="mark", prompt="hello")))
    await asyncio.gather(*summary_tasks)

    assert "Standing preferences:" in captured["prompt"]
    assert "Prefer concise answers." in captured["prompt"]
    assert "Relevant prior context:" in captured["prompt"]
    assert captured["prompt"].endswith("hello")
    assert len(memory_service.build_calls) == 1
    assert memory_service.build_calls[0]["prompt"] == "hello"
    assert len(memory_service.summarize_calls) == 1
    assert memory_service.summarize_calls[0]["assistant_output"] == "done"
    assert memory_service.summarize_calls[0]["assistant_persisted"] is True


async def test_agent_runner_uses_only_user_prompt_for_memory_recall_on_scheduled_runs(
    infra, monkeypatch
):
    factory, audit = infra
    captured = {}

    async def fake_run(agent, prompt, run_config=None):
        captured["prompt"] = prompt
        return SimpleNamespace(final_output="done")

    monkeypatch.setattr("jarvis.agents.runner.Runner.run", fake_run)
    memory_service = _FakeMemoryService(factory)
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        model=_FakeModel(),
        memory_service=memory_service,
    )

    await runner.run(
        InvocationRequest(
            trigger=ScheduledTrigger(
                schedule_id="daily",
                prompt="Prepare my daily brief for today.",
                output_mode="dashboard_only",
                timezone="America/Los_Angeles",
                fired_at=datetime(2026, 6, 5, 1, 30, tzinfo=UTC),
            )
        )
    )

    assert memory_service.build_calls[0]["prompt"] == "Prepare my daily brief for today."
    assert "Schedule context:" in captured["prompt"]
    assert "Local date: 2026-06-04" in captured["prompt"]
    assert captured["prompt"].endswith("Prepare my daily brief for today.")


async def test_agent_runner_continues_when_memory_recall_or_summary_fails(infra, monkeypatch):
    factory, audit = infra
    captured = {}
    summary_tasks = []
    real_create_task = asyncio.create_task

    class BrokenMemoryService:
        async def build_context(self, **kwargs):
            raise RuntimeError("memory down")

        async def summarize_run(self, **kwargs):
            raise RuntimeError("summary down")

    async def fake_run(agent, prompt, run_config=None):
        captured["prompt"] = prompt
        return SimpleNamespace(final_output="done")

    def fake_create_task(coro, *, name=None):
        task = real_create_task(coro, name=name)
        summary_tasks.append(task)
        return task

    monkeypatch.setattr("jarvis.agents.runner.Runner.run", fake_run)
    monkeypatch.setattr("jarvis.agents.runner.asyncio.create_task", fake_create_task)
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        model=_FakeModel(),
        memory_service=BrokenMemoryService(),
    )

    result = await runner.run(
        InvocationRequest(
            trigger=ScheduledTrigger(
                schedule_id="daily",
                prompt="Prepare my daily brief for today.",
                output_mode="dashboard_only",
                timezone="America/Los_Angeles",
                fired_at=datetime(2026, 6, 5, 1, 30, tzinfo=UTC),
            )
        )
    )
    await asyncio.gather(*summary_tasks)

    assert result.final_output == "done"
    assert "Standing preferences:" not in captured["prompt"]
    assert "Schedule context:" in captured["prompt"]
    assert captured["prompt"].endswith("Prepare my daily brief for today.")


async def test_agent_runner_drain_waits_for_inflight_summary(infra, monkeypatch):
    factory, audit = infra

    class BlockingMemoryService:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def build_context(self, **kwargs):
            return MemoryContext(
                preferences=[],
                recalled=[],
                recall_available=False,
                error=None,
            )

        async def summarize_run(self, **kwargs):
            self.started.set()
            await self.release.wait()

    async def fake_run(agent, prompt, run_config=None):
        return SimpleNamespace(final_output="done")

    monkeypatch.setattr("jarvis.agents.runner.Runner.run", fake_run)
    memory_service = BlockingMemoryService()
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers_provider=lambda: [],
        llm_config=LLMConfig(base_url="http://x/v1", api_key="k", model="m"),
        model=_FakeModel(),
        memory_service=memory_service,
    )

    await runner.run(InvocationRequest(trigger=ManualTrigger(user="mark", prompt="hello")))
    await memory_service.started.wait()

    drain_task = asyncio.create_task(runner.drain_memory_tasks())
    await asyncio.sleep(0)
    assert not drain_task.done()

    memory_service.release.set()
    await drain_task
