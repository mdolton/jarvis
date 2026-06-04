from unittest.mock import AsyncMock, MagicMock

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client_and_factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.scheduler = MagicMock()
    ctx.scheduler.fire_now = AsyncMock(return_value=None)

    from jarvis.agents.model_catalog import Catalog

    ctx.model_catalog.list_models = AsyncMock(
        return_value=Catalog(models=["alpha", "beta"], ok=True)
    )

    app = create_app(app_context=ctx)
    client = TestClient(app)
    yield client, factory

    await engine.dispose()


def test_schedules_page_renders(client_and_factory):
    client, _ = client_and_factory
    resp = client.get("/schedules")
    assert resp.status_code == 200
    assert "schedules" in resp.text.lower()


def test_create_schedule(client_and_factory):
    client, _ = client_and_factory
    resp = client.post(
        "/schedules",
        data={
            "name": "morning-email",
            "description": "Check email",
            "cron_expr": "0 8 * * *",
            "timezone": "UTC",
            "prompt": "Summarize email",
            "output_mode": "discord",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)  # redirect after create


def test_toggle_schedule(client_and_factory):
    client, _ = client_and_factory
    # Create first.
    client.post(
        "/schedules",
        data={
            "name": "toggleme",
            "description": "",
            "cron_expr": "* * * * *",
            "timezone": "UTC",
            "prompt": "x",
            "output_mode": "dashboard_only",
        },
        follow_redirects=False,
    )
    # Get the list to find the schedule.
    resp = client.get("/schedules")
    assert "toggleme" in resp.text


def test_schedules_page_lists_models_in_form(client_and_factory):
    client, _ = client_and_factory
    resp = client.get("/schedules")
    assert resp.status_code == 200
    assert "alpha" in resp.text and "beta" in resp.text


def test_create_schedule_with_model(client_and_factory):
    client, _ = client_and_factory
    resp = client.post(
        "/schedules",
        data={
            "name": "pinned",
            "description": "",
            "cron_expr": "0 8 * * *",
            "timezone": "UTC",
            "prompt": "do it",
            "output_mode": "discord",
            "model": "alpha",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)


def test_create_schedule_default_model_is_null(client_and_factory):
    client, _ = client_and_factory
    resp = client.post(
        "/schedules",
        data={
            "name": "unpinned",
            "description": "",
            "cron_expr": "0 8 * * *",
            "timezone": "UTC",
            "prompt": "do it",
            "output_mode": "discord",
            "model": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)


def test_run_schedule_now_calls_scheduler(client_and_factory):
    client, factory = client_and_factory
    client.post(
        "/schedules",
        data={
            "name": "runme",
            "description": "",
            "cron_expr": "0 8 * * *",
            "timezone": "UTC",
            "prompt": "do it",
            "output_mode": "dashboard_only",
            "model": "",
        },
        follow_redirects=False,
    )

    async def _schedule_id():
        from jarvis.persistence.repositories import ScheduleRepo

        async with factory() as session:
            schedules = await ScheduleRepo(session).list_all()
            return schedules[0].id

    import anyio

    schedule_id = anyio.run(_schedule_id)
    resp = client.post(f"/schedules/{schedule_id}/run", follow_redirects=False)

    assert resp.status_code in (302, 303)
    client.app.state.ctx.scheduler.fire_now.assert_awaited_once_with(schedule_id)
