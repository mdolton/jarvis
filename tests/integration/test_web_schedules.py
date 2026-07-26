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
    ctx.scheduler.on_created = AsyncMock(return_value=None)
    ctx.scheduler.on_toggled = AsyncMock(return_value=None)
    ctx.scheduler.on_updated = AsyncMock(return_value=None)
    ctx.scheduler.on_deleted = AsyncMock(return_value=None)

    from jarvis.agents.model_catalog import Catalog

    ctx.model_catalog.list_models = AsyncMock(
        return_value=Catalog(models=["alpha", "beta"], ok=True)
    )

    app = create_app(app_context=ctx)
    client = TestClient(app, headers={"origin": "http://testserver"})
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


def test_schedules_page_defaults_timezone_to_config(client_and_factory):
    client, _ = client_and_factory
    client.app.state.ctx.config.jarvis.timezone = "America/Los_Angeles"

    resp = client.get("/schedules")

    assert resp.status_code == 200
    assert 'name="timezone" placeholder="Timezone" value="America/Los_Angeles"' in resp.text


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


def test_schedules_page_links_error_status_to_error_log(client_and_factory):
    client, factory = client_and_factory

    async def _create_failed_schedule():
        from datetime import UTC, datetime

        from jarvis.core.types import AuditEvent, AuditEventType
        from jarvis.persistence.repositories import AuditRepo, ScheduleRepo

        async with factory() as session:
            schedule = await ScheduleRepo(session).create(
                name="failing-brief",
                description="",
                cron_expr="0 8 * * *",
                timezone="UTC",
                prompt="do it",
                output_mode="dashboard_only",
                notify_on_error=True,
                enabled=True,
            )
            await ScheduleRepo(session).record_run(
                schedule.id,
                at=datetime.now(UTC),
                status="error",
            )
            error = AuditEvent(
                type=AuditEventType.SCHEDULE_ERROR,
                payload={
                    "schedule_id": str(schedule.id),
                    "schedule_name": schedule.name,
                    "error": "Calendar timed out",
                },
            )
            await AuditRepo(session).write_many([error])
            return error.id

    import anyio

    error_id = anyio.run(_create_failed_schedule)
    resp = client.get("/schedules")

    assert resp.status_code == 200
    assert "failing-brief" in resp.text
    assert f'href="/errors#event-{error_id}"' in resp.text


def test_schedules_page_renders_last_run_in_configured_timezone(client_and_factory, monkeypatch):
    import os
    import time

    client, factory = client_and_factory

    async def _create_schedule():
        from datetime import UTC, datetime

        from jarvis.persistence.repositories import ScheduleRepo

        async with factory() as session:
            schedule = await ScheduleRepo(session).create(
                name="timezone-brief",
                description="",
                cron_expr="0 8 * * *",
                timezone="UTC",
                prompt="do it",
                output_mode="dashboard_only",
                notify_on_error=True,
                enabled=True,
            )
            await ScheduleRepo(session).record_run(
                schedule.id,
                at=datetime(2026, 1, 2, 15, 4, 5, tzinfo=UTC),
                status="success",
            )

    import anyio

    anyio.run(_create_schedule)
    old_tz = os.environ.get("TZ")
    client.app.state.ctx.config.jarvis.timezone = "America/Los_Angeles"
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):
        time.tzset()

    try:
        resp = client.get("/schedules")
    finally:
        if old_tz is None:
            monkeypatch.delenv("TZ", raising=False)
        else:
            monkeypatch.setenv("TZ", old_tz)
        if hasattr(time, "tzset"):
            time.tzset()

    assert resp.status_code == 200
    assert "timezone-brief" in resp.text
    assert "2026-01-02 07:04" in resp.text
    assert "2026-01-02 15:04" not in resp.text


def test_schedules_page_can_prefill_from_template(client_and_factory):
    client, factory = client_and_factory

    async def _create_template():
        from jarvis.persistence.repositories import DigestTemplateRepo

        async with factory() as session:
            row = await DigestTemplateRepo(session).create(
                key=None,
                name="Morning Ops",
                description="Ops summary",
                category="brief",
                prompt="Summarize calendar and mail.",
                default_cron_expr="15 8 * * *",
                default_timezone="America/Los_Angeles",
                default_output_mode="discord_if_noteworthy",
                default_model="alpha",
                default_discord_user_id="456",
                built_in=False,
                enabled=True,
            )
            return row.id

    import anyio

    template_id = anyio.run(_create_template)
    resp = client.get(f"/schedules?template_id={template_id}")

    assert resp.status_code == 200
    assert "Morning Ops" in resp.text
    assert "15 8 * * *" in resp.text
    assert "America/Los_Angeles" in resp.text
    assert "Summarize calendar and mail." in resp.text
    assert "456" in resp.text
    assert '<option value="alpha" selected>' in resp.text


def test_schedules_page_lists_templates_in_selector(client_and_factory):
    client, factory = client_and_factory

    async def _create_template():
        from jarvis.persistence.repositories import DigestTemplateRepo

        async with factory() as session:
            await DigestTemplateRepo(session).create(
                key=None,
                name="Template Choice",
                description="Choice",
                category="brief",
                prompt="Use this template.",
                default_cron_expr="0 10 * * *",
                default_timezone="UTC",
                default_output_mode="discord",
                default_model=None,
                default_discord_user_id=None,
                built_in=False,
                enabled=True,
            )

    import anyio

    anyio.run(_create_template)
    resp = client.get("/schedules")

    assert resp.status_code == 200
    assert "Template Choice" in resp.text
    assert "/schedules?template_id=" in resp.text


def test_create_schedule_from_template_snapshot_persists_normal_schedule(
    client_and_factory,
):
    client, factory = client_and_factory

    async def _create_template():
        from jarvis.persistence.repositories import DigestTemplateRepo

        async with factory() as session:
            row = await DigestTemplateRepo(session).create(
                key=None,
                name="Snapshot Source",
                description="Source template",
                category="brief",
                prompt="Original template prompt.",
                default_cron_expr="0 8 * * *",
                default_timezone="UTC",
                default_output_mode="discord",
                default_model="beta",
                default_discord_user_id="789",
                built_in=False,
                enabled=True,
            )
            return row.id

    import anyio

    template_id = anyio.run(_create_template)
    page = client.get(f"/schedules?template_id={template_id}")
    assert "Original template prompt." in page.text

    resp = client.post(
        "/schedules",
        data={
            "name": "Snapshot Schedule",
            "description": "Copied and edited",
            "cron_expr": "30 8 * * *",
            "timezone": "America/Los_Angeles",
            "prompt": "Edited schedule prompt.",
            "output_mode": "dashboard_only",
            "model": "",
            "discord_user_id": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    async def _schedule():
        from jarvis.persistence.repositories import ScheduleRepo

        async with factory() as session:
            rows = await ScheduleRepo(session).list_all()
            return rows[0]

    schedule = anyio.run(_schedule)
    assert schedule.name == "Snapshot Schedule"
    assert schedule.prompt == "Edited schedule prompt."
    assert schedule.cron_expr == "30 8 * * *"
    assert schedule.model is None


def test_create_schedule_from_template_preserves_unavailable_model_snapshot(
    client_and_factory,
):
    client, factory = client_and_factory

    async def _create_template():
        from jarvis.persistence.repositories import DigestTemplateRepo

        async with factory() as session:
            row = await DigestTemplateRepo(session).create(
                key=None,
                name="Stale Model Source",
                description="Source template",
                category="brief",
                prompt="Prompt with unavailable model.",
                default_cron_expr="0 7 * * *",
                default_timezone="UTC",
                default_output_mode="discord",
                default_model="ghost-model",
                default_discord_user_id=None,
                built_in=False,
                enabled=True,
            )
            return row.id

    import anyio

    template_id = anyio.run(_create_template)
    page = client.get(f"/schedules?template_id={template_id}")

    assert page.status_code == 200
    assert '<option value="ghost-model" selected>' in page.text
    assert "ghost-model (not available)" in page.text

    resp = client.post(
        "/schedules",
        data={
            "name": "Stale Model Snapshot",
            "description": "Copied",
            "cron_expr": "0 7 * * *",
            "timezone": "UTC",
            "prompt": "Prompt with unavailable model.",
            "output_mode": "discord",
            "model": "ghost-model",
            "discord_user_id": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    async def _schedule():
        from jarvis.persistence.repositories import ScheduleRepo

        async with factory() as session:
            rows = await ScheduleRepo(session).list_all()
            return rows[0]

    schedule = anyio.run(_schedule)
    assert schedule.name == "Stale Model Snapshot"
    assert schedule.model == "ghost-model"


def test_template_model_is_not_marked_unavailable_when_catalog_fails(
    client_and_factory,
):
    client, factory = client_and_factory

    from jarvis.agents.model_catalog import Catalog

    client.app.state.ctx.model_catalog.list_models = AsyncMock(
        return_value=Catalog(models=[], ok=False)
    )

    async def _create_template():
        from jarvis.persistence.repositories import DigestTemplateRepo

        async with factory() as session:
            row = await DigestTemplateRepo(session).create(
                key=None,
                name="Unknown Catalog Source",
                description="Source template",
                category="brief",
                prompt="Prompt when catalog fails.",
                default_cron_expr="0 6 * * *",
                default_timezone="UTC",
                default_output_mode="discord",
                default_model="catalog-fallback-model",
                default_discord_user_id=None,
                built_in=False,
                enabled=True,
            )
            return row.id

    import anyio

    template_id = anyio.run(_create_template)
    page = client.get(f"/schedules?template_id={template_id}")

    assert page.status_code == 200
    assert (
        '<option value="catalog-fallback-model" selected>catalog-fallback-model</option>'
    ) in page.text
    assert "catalog-fallback-model (not available)" not in page.text


def test_schedules_page_warns_for_malformed_template_id(client_and_factory):
    client, _ = client_and_factory

    resp = client.get("/schedules?template_id=not-a-uuid")

    assert resp.status_code == 200
    assert "Create Schedule" in resp.text
    assert "Template not found or disabled." in resp.text


def test_schedules_page_warns_for_missing_template_id(client_and_factory):
    client, _ = client_and_factory

    from uuid import uuid4

    resp = client.get(f"/schedules?template_id={uuid4()}")

    assert resp.status_code == 200
    assert "Create Schedule" in resp.text
    assert "Template not found or disabled." in resp.text


def test_schedules_page_warns_for_disabled_template_id(client_and_factory):
    client, factory = client_and_factory

    async def _create_template():
        from jarvis.persistence.repositories import DigestTemplateRepo

        async with factory() as session:
            row = await DigestTemplateRepo(session).create(
                key=None,
                name="Disabled Source",
                description="Source template",
                category="brief",
                prompt="Disabled template prompt.",
                default_cron_expr="0 9 * * *",
                default_timezone="UTC",
                default_output_mode="discord",
                default_model="alpha",
                default_discord_user_id=None,
                built_in=False,
                enabled=False,
            )
            return row.id

    import anyio

    template_id = anyio.run(_create_template)
    resp = client.get(f"/schedules?template_id={template_id}")

    assert resp.status_code == 200
    assert "Template not found or disabled." in resp.text
    assert "Disabled template prompt." not in resp.text


def _create_form(name="lifecycle-sched", cron="0 8 * * *", timezone="UTC"):
    return {
        "name": name,
        "description": "",
        "cron_expr": cron,
        "timezone": timezone,
        "prompt": "x",
        "output_mode": "dashboard_only",
    }


def test_create_schedule_registers_with_live_scheduler(client_and_factory):
    client, _ = client_and_factory
    resp = client.post("/schedules", data=_create_form(), follow_redirects=False)
    assert resp.status_code in (302, 303)

    on_created = client.app.state.ctx.scheduler.on_created
    on_created.assert_awaited_once()
    row = on_created.await_args.args[0]
    assert row.name == "lifecycle-sched"


def test_create_schedule_rejects_invalid_cron(client_and_factory):
    client, _ = client_and_factory
    resp = client.post(
        "/schedules",
        data=_create_form(name="bad-cron-sched", cron="not a cron"),
        follow_redirects=False,
    )
    assert resp.status_code == 400
    client.app.state.ctx.scheduler.on_created.assert_not_awaited()
    # No row was written.
    assert "bad-cron-sched" not in client.get("/schedules").text


def test_create_schedule_rejects_invalid_timezone(client_and_factory):
    client, _ = client_and_factory
    resp = client.post(
        "/schedules",
        data=_create_form(name="bad-tz-sched", timezone="Mars/Olympus_Mons"),
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_toggle_schedule_syncs_scheduler(client_and_factory):
    client, _ = client_and_factory
    client.post("/schedules", data=_create_form(name="toggle-sync"), follow_redirects=False)
    on_created = client.app.state.ctx.scheduler.on_created
    schedule_id = on_created.await_args.args[0].id

    resp = client.post(f"/schedules/{schedule_id}/toggle", follow_redirects=False)
    assert resp.status_code in (302, 303)

    on_toggled = client.app.state.ctx.scheduler.on_toggled
    on_toggled.assert_awaited_once()
    row = on_toggled.await_args.args[0]
    assert row.id == schedule_id
    assert row.enabled is False  # was created enabled; toggle flips it


def test_delete_schedule_unregisters(client_and_factory):
    client, _ = client_and_factory
    client.post("/schedules", data=_create_form(name="delete-sync"), follow_redirects=False)
    schedule_id = client.app.state.ctx.scheduler.on_created.await_args.args[0].id

    resp = client.post(f"/schedules/{schedule_id}/delete", follow_redirects=False)
    assert resp.status_code in (302, 303)
    client.app.state.ctx.scheduler.on_deleted.assert_awaited_once_with(schedule_id)


def test_toggle_missing_schedule_is_noop(client_and_factory):
    client, _ = client_and_factory
    resp = client.post(
        "/schedules/00000000-0000-0000-0000-000000000000/toggle",
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    client.app.state.ctx.scheduler.on_toggled.assert_not_awaited()


def _create(client, name, **extra):
    return client.post(
        "/schedules",
        data={
            "name": name,
            "description": "",
            "cron_expr": "0 8 * * *",
            "timezone": "UTC",
            "prompt": "brief me",
            "output_mode": "dashboard_only",
            **extra,
        },
        follow_redirects=False,
    )


def test_scope_picker_lists_connected_servers(client_and_factory):
    client, _ = client_and_factory
    client.app.state.ctx.mcp_manager.server_names = lambda: ["gmail", "weather"]

    text = client.get("/schedules").text

    assert 'name="mcp_servers" value="gmail"' in text
    assert 'name="mcp_servers" value="weather"' in text


async def test_create_with_scope_persists_the_allow_list(client_and_factory):
    client, factory = client_and_factory
    client.app.state.ctx.mcp_manager.server_names = lambda: ["gmail", "weather"]

    resp = _create(client, "scoped-brief", mcp_servers=["weather", "gmail"])
    assert resp.status_code in (302, 303)

    from jarvis.persistence.repositories import ScheduleRepo

    async with factory() as s:
        row = next(r for r in await ScheduleRepo(s).list_all() if r.name == "scoped-brief")
    assert row.mcp_servers == ["weather", "gmail"]


async def test_create_without_scope_stores_null_not_empty_list(client_and_factory):
    """NULL is the single representation of "all servers", so an unscoped
    schedule created today reads identically to a pre-migration row."""
    client, factory = client_and_factory

    assert _create(client, "unscoped-brief").status_code in (302, 303)

    from jarvis.persistence.repositories import ScheduleRepo

    async with factory() as s:
        row = next(r for r in await ScheduleRepo(s).list_all() if r.name == "unscoped-brief")
    assert row.mcp_servers is None


async def test_scope_is_shown_on_existing_schedules(client_and_factory):
    client, _ = client_and_factory
    client.app.state.ctx.mcp_manager.server_names = lambda: ["gmail", "weather"]
    _create(client, "scoped-brief", mcp_servers=["weather"])
    _create(client, "unscoped-brief")

    text = client.get("/schedules").text

    assert "weather" in text
    assert ">all<" in text.replace(" ", "").replace("\n", "")


async def test_scope_can_be_changed_on_an_existing_schedule(client_and_factory):
    """The motivating case: an already-running Daily Brief needs narrowing
    without being deleted and recreated."""
    client, factory = client_and_factory
    client.app.state.ctx.mcp_manager.server_names = lambda: ["gmail", "weather"]
    _create(client, "daily-brief")

    from jarvis.persistence.repositories import ScheduleRepo

    async with factory() as s:
        row = next(r for r in await ScheduleRepo(s).list_all() if r.name == "daily-brief")
    assert row.mcp_servers is None

    resp = client.post(
        f"/schedules/{row.id}/scope",
        data={"mcp_servers": ["weather"]},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    async with factory() as s:
        refreshed = await ScheduleRepo(s).get(row.id)
    assert refreshed.mcp_servers == ["weather"]


async def test_clearing_the_scope_restores_all_servers(client_and_factory):
    client, factory = client_and_factory
    client.app.state.ctx.mcp_manager.server_names = lambda: ["gmail", "weather"]
    _create(client, "briefly", mcp_servers=["weather"])

    from jarvis.persistence.repositories import ScheduleRepo

    async with factory() as s:
        row = next(r for r in await ScheduleRepo(s).list_all() if r.name == "briefly")

    client.post(f"/schedules/{row.id}/scope", data={}, follow_redirects=False)

    async with factory() as s:
        refreshed = await ScheduleRepo(s).get(row.id)
    assert refreshed.mcp_servers is None  # NULL, not [] — "all servers"


def test_scope_change_on_missing_schedule_404s(client_and_factory):
    client, _ = client_and_factory
    from uuid import uuid4

    resp = client.post(
        f"/schedules/{uuid4()}/scope", data={"mcp_servers": ["weather"]}, follow_redirects=False
    )
    assert resp.status_code == 404


async def _row_named(factory, name):
    from jarvis.persistence.repositories import ScheduleRepo

    async with factory() as session:
        return next(r for r in await ScheduleRepo(session).list_all() if r.name == name)


def _edit_form(**overrides):
    return {
        "name": "edited-brief",
        "description": "now with feeling",
        "cron_expr": "30 9 * * *",
        "timezone": "America/Los_Angeles",
        "prompt": "brief me differently",
        "output_mode": "discord_if_noteworthy",
        "model": "beta",
        "discord_user_id": "999",
        **overrides,
    }


async def test_edit_page_prefills_the_current_schedule(client_and_factory):
    client, factory = client_and_factory
    client.app.state.ctx.mcp_manager.server_names = lambda: ["gmail", "weather"]
    _create(client, "prefill-me", model="alpha", mcp_servers=["weather"], discord_user_id="123")
    row = await _row_named(factory, "prefill-me")

    resp = client.get(f"/schedules/{row.id}/edit")

    assert resp.status_code == 200
    assert 'value="prefill-me"' in resp.text
    assert 'value="0 8 * * *"' in resp.text
    assert "brief me" in resp.text
    assert 'value="123"' in resp.text
    assert '<option value="alpha" selected>' in resp.text
    # The scope picker reflects the stored allow-list: weather ticked, gmail not.
    boxes = " ".join(resp.text.split())
    assert 'name="mcp_servers" value="weather" checked' in boxes
    assert 'name="mcp_servers" value="gmail" checked' not in boxes


def test_edit_page_404s_for_a_missing_schedule(client_and_factory):
    client, _ = client_and_factory
    from uuid import uuid4

    assert client.get(f"/schedules/{uuid4()}/edit").status_code == 404


async def test_edit_persists_every_field(client_and_factory):
    """The motivating case: change the prompt, cron, output and model in place
    instead of deleting and recreating the schedule."""
    client, factory = client_and_factory
    client.app.state.ctx.mcp_manager.server_names = lambda: ["gmail", "weather"]
    _create(client, "editable")
    row = await _row_named(factory, "editable")

    resp = client.post(
        f"/schedules/{row.id}",
        data=_edit_form(mcp_servers=["gmail"], notify_on_error="true"),
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    updated = await _row_named(factory, "edited-brief")
    assert updated.id == row.id
    assert updated.description == "now with feeling"
    assert updated.cron_expr == "30 9 * * *"
    assert updated.timezone == "America/Los_Angeles"
    assert updated.prompt == "brief me differently"
    assert updated.output_mode == "discord_if_noteworthy"
    assert updated.model == "beta"
    assert updated.discord_user_id == "999"
    assert updated.mcp_servers == ["gmail"]
    assert updated.notify_on_error is True


async def test_edit_resyncs_the_live_scheduler(client_and_factory):
    client, factory = client_and_factory
    _create(client, "resync-me")
    row = await _row_named(factory, "resync-me")

    client.post(f"/schedules/{row.id}", data=_edit_form(), follow_redirects=False)

    on_updated = client.app.state.ctx.scheduler.on_updated
    on_updated.assert_awaited_once()
    passed = on_updated.await_args.args[0]
    assert passed.id == row.id
    assert passed.cron_expr == "30 9 * * *"  # the post-update row, not the stale one


async def test_edit_clears_optional_fields_when_blanked(client_and_factory):
    """Blank on edit means "clear it" — the form arrived prefilled, so an empty
    box is a deliberate act rather than an unstated preference."""
    client, factory = client_and_factory
    client.app.state.ctx.mcp_manager.server_names = lambda: ["gmail"]
    _create(client, "clear-me", model="alpha", discord_user_id="123", mcp_servers=["gmail"])
    row = await _row_named(factory, "clear-me")

    client.post(
        f"/schedules/{row.id}",
        data=_edit_form(name="clear-me", model="", discord_user_id=""),
        follow_redirects=False,
    )

    updated = await _row_named(factory, "clear-me")
    assert updated.model is None
    assert updated.discord_user_id is None
    assert updated.mcp_servers is None  # NULL, not [] — "all servers"
    assert updated.notify_on_error is False  # unchecked box sends nothing


async def test_edit_rejects_an_invalid_cron_without_writing(client_and_factory):
    client, factory = client_and_factory
    _create(client, "keep-me")
    row = await _row_named(factory, "keep-me")

    resp = client.post(
        f"/schedules/{row.id}",
        data=_edit_form(cron_expr="not a cron"),
        follow_redirects=False,
    )

    assert resp.status_code == 400
    client.app.state.ctx.scheduler.on_updated.assert_not_awaited()
    assert (await _row_named(factory, "keep-me")).cron_expr == "0 8 * * *"


async def test_edit_rejects_an_invalid_timezone(client_and_factory):
    client, factory = client_and_factory
    _create(client, "tz-guard")
    row = await _row_named(factory, "tz-guard")

    resp = client.post(
        f"/schedules/{row.id}",
        data=_edit_form(timezone="Mars/Olympus_Mons"),
        follow_redirects=False,
    )
    assert resp.status_code == 400


async def test_edit_rejects_an_unknown_output_mode(client_and_factory):
    client, factory = client_and_factory
    _create(client, "mode-guard")
    row = await _row_named(factory, "mode-guard")

    resp = client.post(
        f"/schedules/{row.id}",
        data=_edit_form(output_mode="carrier_pigeon"),
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_edit_of_a_missing_schedule_404s(client_and_factory):
    client, _ = client_and_factory
    from uuid import uuid4

    resp = client.post(f"/schedules/{uuid4()}", data=_edit_form(), follow_redirects=False)
    assert resp.status_code == 404


async def test_schedule_list_links_to_the_edit_page(client_and_factory):
    client, factory = client_and_factory
    _create(client, "linked")
    row = await _row_named(factory, "linked")

    assert f'href="/schedules/{row.id}/edit"' in client.get("/schedules").text


def test_create_rejects_an_unknown_output_mode(client_and_factory):
    client, _ = client_and_factory

    resp = _create(client, "bad-mode", output_mode="carrier_pigeon")

    assert resp.status_code == 400
    client.app.state.ctx.scheduler.on_created.assert_not_awaited()
