"""Scheduler integration tests. Use fire_now to trigger immediately."""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from agents import set_trace_processors
from agents.models.interface import Model

from jarvis.audit.logger import AuditLogger
from jarvis.audit.tracer import JarvisTraceProcessor
from jarvis.config.schema import LLMConfig
from jarvis.core.types import ChannelKind, MessageRole
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MessageRepo, ScheduleRepo
from jarvis.scheduler.scheduler import Scheduler


class _FakeModel(Model):
    def __init__(self) -> None:
        self.call_count = 0

    async def get_response(self, *a, **kw):
        from agents.items import ModelResponse, Usage
        from openai.types.responses import ResponseOutputMessage, ResponseOutputText

        self.call_count += 1
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="m1",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        ResponseOutputText(
                            type="output_text",
                            text=f"scheduled-reply-{self.call_count}",
                            annotations=[],
                        )
                    ],
                )
            ],
            usage=Usage(),
            response_id=None,
        )

    async def stream_response(self, *a, **kw):
        if False:
            yield None


class _FailingModel(Model):
    async def get_response(self, *a, **kw):
        raise RuntimeError("model unavailable")

    async def stream_response(self, *a, **kw):
        if False:
            yield None


class _RecordingDiscordAdapter:
    kind = "discord"

    def __init__(self) -> None:
        self.sent = []

    async def start(self, dispatcher) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg) -> None:
        self.sent.append(msg)


def _far_future_cron_expr() -> str:
    """Keep fire_now tests from racing with APScheduler's real cron trigger."""
    future = datetime.now(UTC) + timedelta(days=180)
    return f"{future.minute} {future.hour} {future.day} {future.month} *"


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


async def test_scheduler_fires_and_records_run(infra):
    """Create a schedule, trigger via fire_now, verify messages + status."""
    _, factory, audit = infra
    model = _FakeModel()

    scheduler = Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model_override=model,
        mcp_servers_provider=lambda: [],
        discord_adapter=None,
    )

    async with factory() as s:
        sched = await ScheduleRepo(s).create(
            name="test-sched",
            description="test",
            cron_expr=_far_future_cron_expr(),
            timezone="America/Los_Angeles",
            prompt="give me a summary",
            output_mode="dashboard_only",
            notify_on_error=True,
            enabled=True,
        )
        sched_id = sched.id

    await scheduler.start()
    try:
        await scheduler.fire_now(sched_id)
    finally:
        await scheduler.stop()

    async with factory() as s:
        refreshed = await ScheduleRepo(s).get(sched_id)
        assert refreshed.last_run_status == "success"
        assert refreshed.last_run_at is not None

    async with factory() as s:
        from sqlalchemy import select

        from jarvis.persistence.models import ConversationRow

        convs = (await s.execute(select(ConversationRow))).scalars().all()
        assert len(convs) == 1
        assert convs[0].channel_kind == ChannelKind.SCHEDULED.value

        msgs = await MessageRepo(s).history(convs[0].id)
        assert len(msgs) == 2
        assert msgs[0].role == MessageRole.USER.value
        assert "Timezone: America/Los_Angeles" in msgs[0].content
        assert msgs[0].content.endswith("give me a summary")
        assert msgs[1].role == MessageRole.ASSISTANT.value


async def test_scheduler_handles_disabled_schedule(infra):
    _, factory, audit = infra

    scheduler = Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model_override=_FakeModel(),
        mcp_servers_provider=lambda: [],
        discord_adapter=None,
    )

    async with factory() as s:
        await ScheduleRepo(s).create(
            name="disabled",
            description="",
            cron_expr="* * * * *",
            timezone="UTC",
            prompt="x",
            output_mode="dashboard_only",
            notify_on_error=True,
            enabled=False,
        )

    await scheduler.start()
    try:
        assert scheduler.active_job_count() == 0
    finally:
        await scheduler.stop()


async def test_scheduler_empty_db_starts_cleanly(infra):
    _, factory, audit = infra

    scheduler = Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model_override=_FakeModel(),
        mcp_servers_provider=lambda: [],
        discord_adapter=None,
    )

    await scheduler.start()
    try:
        assert scheduler.active_job_count() == 0
    finally:
        await scheduler.stop()


async def test_scheduler_routes_discord_output_to_schedule_recipient(infra):
    _, factory, audit = infra
    discord = _RecordingDiscordAdapter()
    scheduler = Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model_override=_FakeModel(),
        mcp_servers_provider=lambda: [],
        discord_adapter=discord,
    )

    async with factory() as s:
        sched = await ScheduleRepo(s).create(
            name="discord",
            description="",
            cron_expr=_far_future_cron_expr(),
            timezone="UTC",
            prompt="x",
            output_mode="discord",
            notify_on_error=True,
            enabled=True,
            discord_user_id="111",
        )

    await scheduler.start()
    try:
        await scheduler.fire_now(sched.id)
    finally:
        await scheduler.stop()

    assert len(discord.sent) == 1
    assert discord.sent[0].channel_ref == "111"
    assert discord.sent[0].text == "scheduled-reply-1"


async def test_scheduler_notifies_discord_recipient_on_failure_when_enabled(infra):
    _, factory, audit = infra
    discord = _RecordingDiscordAdapter()
    scheduler = Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model_override=_FailingModel(),
        mcp_servers_provider=lambda: [],
        discord_adapter=discord,
    )

    async with factory() as s:
        sched = await ScheduleRepo(s).create(
            name="breaks",
            description="",
            cron_expr=_far_future_cron_expr(),
            timezone="UTC",
            prompt="x",
            output_mode="discord",
            notify_on_error=True,
            enabled=True,
            discord_user_id="111",
        )

    await scheduler.start()
    try:
        await scheduler.fire_now(sched.id)
    finally:
        await scheduler.stop()

    async with factory() as s:
        refreshed = await ScheduleRepo(s).get(sched.id)
        assert refreshed.last_run_status == "error"

    assert len(discord.sent) == 1
    assert discord.sent[0].channel_ref == "111"
    assert "Scheduled task `breaks` failed" in discord.sent[0].text


async def test_scheduler_times_out_wedged_agent_run(infra, monkeypatch):
    _, factory, audit = infra
    discord = _RecordingDiscordAdapter()

    async def never_returns(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("jarvis.agents.runner.Runner.run", never_returns)

    scheduler = Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model_override=_FakeModel(),
        mcp_servers_provider=lambda: [],
        discord_adapter=discord,
        run_timeout_sec=0.01,
    )

    async with factory() as s:
        sched = await ScheduleRepo(s).create(
            name="wedged",
            description="",
            cron_expr=_far_future_cron_expr(),
            timezone="UTC",
            prompt="x",
            output_mode="discord",
            notify_on_error=True,
            enabled=True,
            discord_user_id="111",
        )

    await scheduler.start()
    try:
        await asyncio.wait_for(scheduler.fire_now(sched.id), timeout=1)
    finally:
        await scheduler.stop()

    async with factory() as s:
        refreshed = await ScheduleRepo(s).get(sched.id)
        assert refreshed.last_run_status == "error"

    assert len(discord.sent) == 1
    assert discord.sent[0].channel_ref == "111"
    assert "Scheduled task `wedged` failed" in discord.sent[0].text
