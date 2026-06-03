import asyncio

import pytest_asyncio
from agents import set_trace_processors
from agents.models.interface import Model

from jarvis.agents.model_catalog import Catalog
from jarvis.audit.logger import AuditLogger
from jarvis.audit.tracer import JarvisTraceProcessor
from jarvis.config.schema import LLMConfig
from jarvis.core.types import AuditEventType
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import AuditRepo, ScheduleRepo
from jarvis.scheduler.scheduler import Scheduler


class _FakeModel(Model):
    async def get_response(self, *a, **kw):
        from agents.items import ModelResponse, Usage
        from openai.types.responses import ResponseOutputMessage, ResponseOutputText

        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id="m1",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[ResponseOutputText(type="output_text", text="ok", annotations=[])],
                )
            ],
            usage=Usage(),
            response_id=None,
        )

    async def stream_response(self, *a, **kw):
        if False:
            yield None


class _StubCatalog:
    def __init__(self, result: Catalog):
        self._result = result

    async def list_models(self) -> Catalog:
        return self._result


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


async def _make_scheduler(factory, audit, catalog):
    return Scheduler(
        session_factory=factory,
        audit=audit,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="cfg-model"),
        model_override=_FakeModel(),
        mcp_servers_provider=lambda: [],
        discord_adapter=None,
        model_catalog=catalog,
    )


async def test_pinned_model_absent_triggers_fallback_audit(infra):
    factory, audit = infra
    async with factory() as s:
        sched = await ScheduleRepo(s).create(
            name="x",
            description="",
            cron_expr="* * * * *",
            timezone="UTC",
            prompt="p",
            output_mode="dashboard_only",
            notify_on_error=True,
            enabled=True,
            model="ghost-model",
        )
    catalog = _StubCatalog(Catalog(models=["cfg-model", "other"], ok=True))
    scheduler = await _make_scheduler(factory, audit, catalog)
    await scheduler.fire_now(sched.id)
    await asyncio.sleep(0.15)

    async with factory() as s:
        events = await AuditRepo(s).recent(limit=50)
    fb = [e for e in events if e.type == AuditEventType.MODEL_FALLBACK.value]
    assert len(fb) == 1
    assert fb[0].payload["requested"] == "ghost-model"
    assert fb[0].payload["substituted"] == "cfg-model"


async def test_pinned_model_present_no_fallback(infra):
    factory, audit = infra
    async with factory() as s:
        sched = await ScheduleRepo(s).create(
            name="x",
            description="",
            cron_expr="* * * * *",
            timezone="UTC",
            prompt="p",
            output_mode="dashboard_only",
            notify_on_error=True,
            enabled=True,
            model="other",
        )
    catalog = _StubCatalog(Catalog(models=["cfg-model", "other"], ok=True))
    scheduler = await _make_scheduler(factory, audit, catalog)
    await scheduler.fire_now(sched.id)
    await asyncio.sleep(0.15)
    async with factory() as s:
        events = await AuditRepo(s).recent(limit=50)
    assert not [e for e in events if e.type == AuditEventType.MODEL_FALLBACK.value]


async def test_catalog_unavailable_no_fallback(infra):
    factory, audit = infra
    async with factory() as s:
        sched = await ScheduleRepo(s).create(
            name="x",
            description="",
            cron_expr="* * * * *",
            timezone="UTC",
            prompt="p",
            output_mode="dashboard_only",
            notify_on_error=True,
            enabled=True,
            model="ghost-model",
        )
    catalog = _StubCatalog(Catalog(models=[], ok=False))
    scheduler = await _make_scheduler(factory, audit, catalog)
    await scheduler.fire_now(sched.id)
    await asyncio.sleep(0.15)
    async with factory() as s:
        events = await AuditRepo(s).recent(limit=50)
    assert not [e for e in events if e.type == AuditEventType.MODEL_FALLBACK.value]
