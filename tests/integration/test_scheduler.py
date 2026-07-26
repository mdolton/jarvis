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


def test_validate_schedule_timing_accepts_valid_input():
    from jarvis.scheduler.scheduler import validate_schedule_timing

    validate_schedule_timing("0 8 * * *", "America/Los_Angeles")  # must not raise


def test_validate_schedule_timing_rejects_bad_cron():
    import pytest

    from jarvis.scheduler.scheduler import validate_schedule_timing

    with pytest.raises(ValueError):
        validate_schedule_timing("not a cron", "UTC")


def test_validate_schedule_timing_rejects_bad_timezone():
    import pytest

    from jarvis.scheduler.scheduler import validate_schedule_timing

    with pytest.raises(ValueError):
        validate_schedule_timing("0 8 * * *", "Mars/Olympus_Mons")


async def test_start_survives_bad_schedule_row_and_registers_good_ones(infra):
    _, factory, audit = infra

    async with factory() as s:
        repo = ScheduleRepo(s)
        await repo.create(
            name="bad-cron",
            description="",
            cron_expr="not a cron",
            timezone="UTC",
            prompt="x",
            output_mode="dashboard_only",
            notify_on_error=False,
            enabled=True,
        )
        good = await repo.create(
            name="good",
            description="",
            cron_expr=_far_future_cron_expr(),
            timezone="UTC",
            prompt="x",
            output_mode="dashboard_only",
            notify_on_error=False,
            enabled=True,
        )

    scheduler = Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model_override=_FakeModel(),
        mcp_servers_provider=lambda: [],
        discord_adapter=None,
    )

    await scheduler.start()  # must not raise
    try:
        assert scheduler.active_job_count() == 1
        assert good.id in scheduler._jobs
    finally:
        await scheduler.stop()

    # SCHEDULE_ERROR audit event names the bad schedule.
    await asyncio.sleep(0.1)  # let the audit logger flush
    from jarvis.core.types import AuditEventType
    from jarvis.persistence.repositories import AuditRepo

    async with factory() as s:
        events = await AuditRepo(s).recent(types=[AuditEventType.SCHEDULE_ERROR], limit=10)
    assert any(e.payload.get("schedule_name") == "bad-cron" for e in events)


async def test_lifecycle_methods_register_and_unregister(infra):
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

        async with factory() as s:
            row = await ScheduleRepo(s).create(
                name="post-boot",
                description="",
                cron_expr=_far_future_cron_expr(),
                timezone="UTC",
                prompt="x",
                output_mode="dashboard_only",
                notify_on_error=False,
                enabled=True,
            )

        # Created after boot -> registers live.
        await scheduler.on_created(row)
        assert scheduler.active_job_count() == 1

        # Disable -> unregisters.
        async with factory() as s:
            repo = ScheduleRepo(s)
            await repo.set_enabled(row.id, False)
        async with factory() as s:
            row = await ScheduleRepo(s).get(row.id)
        await scheduler.on_toggled(row)
        assert scheduler.active_job_count() == 0

        # Re-enable -> registers again.
        async with factory() as s:
            await ScheduleRepo(s).set_enabled(row.id, True)
        async with factory() as s:
            row = await ScheduleRepo(s).get(row.id)
        await scheduler.on_toggled(row)
        assert scheduler.active_job_count() == 1

        # Delete -> unregisters.
        await scheduler.on_deleted(row.id)
        assert scheduler.active_job_count() == 0

        # Idempotent: removing an unknown id is a no-op.
        await scheduler.on_deleted(row.id)
        assert scheduler.active_job_count() == 0
    finally:
        await scheduler.stop()


async def test_on_created_ignores_disabled_row(infra):
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
        async with factory() as s:
            row = await ScheduleRepo(s).create(
                name="starts-disabled",
                description="",
                cron_expr=_far_future_cron_expr(),
                timezone="UTC",
                prompt="x",
                output_mode="dashboard_only",
                notify_on_error=False,
                enabled=False,
            )
        await scheduler.on_created(row)
        assert scheduler.active_job_count() == 0
    finally:
        await scheduler.stop()


async def test_schedule_mcp_scope_reaches_the_server_provider(infra):
    """End-to-end plumbing: the row's allow-list must survive into the call
    that selects MCP servers for the agent."""
    _, factory, audit = infra
    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        return []

    scheduler = Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model_override=_FakeModel(),
        mcp_servers_provider=provider,
        discord_adapter=None,
    )

    async with factory() as s:
        sched = await ScheduleRepo(s).create(
            name="scoped",
            description="",
            cron_expr=_far_future_cron_expr(),
            timezone="America/Los_Angeles",
            prompt="brief me",
            output_mode="dashboard_only",
            notify_on_error=True,
            enabled=True,
            mcp_servers=["weather", "calendar"],
        )
        sched_id = sched.id

    await scheduler.start()
    try:
        await scheduler.fire_now(sched_id)
    finally:
        await scheduler.stop()

    assert calls == [{"only": ("weather", "calendar")}]


async def test_schedule_without_scope_calls_the_provider_unfiltered(infra):
    _, factory, audit = infra
    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        return []

    scheduler = Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model_override=_FakeModel(),
        mcp_servers_provider=provider,
        discord_adapter=None,
    )

    async with factory() as s:
        sched = await ScheduleRepo(s).create(
            name="unscoped",
            description="",
            cron_expr=_far_future_cron_expr(),
            timezone="America/Los_Angeles",
            prompt="brief me",
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

    assert calls == [{}]
