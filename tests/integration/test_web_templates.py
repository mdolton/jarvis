from unittest.mock import MagicMock

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.digests.seeds import seed_built_in_digest_templates
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import DigestTemplateRepo
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client_and_factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as session:
        await seed_built_in_digest_templates(session)

    ctx = MagicMock()
    ctx.session_factory = factory
    app = create_app(app_context=ctx)
    client = TestClient(app, headers={"origin": "http://testserver"})
    yield client, factory

    await engine.dispose()


def test_templates_page_lists_built_in_templates(client_and_factory):
    client, _ = client_and_factory
    resp = client.get("/templates")
    assert resp.status_code == 200
    assert "Daily Brief" in resp.text
    assert "Email Digest" in resp.text
    assert "Calendar Brief" in resp.text
    assert "Action Inbox Review" in resp.text


def test_templates_new_page_renders_create_form(client_and_factory):
    client, _ = client_and_factory
    resp = client.get("/templates/new")
    assert resp.status_code == 200
    assert "Create Template" in resp.text
    assert "Template prompt" in resp.text


def test_create_user_template(client_and_factory):
    client, _ = client_and_factory
    resp = client.post(
        "/templates",
        data={
            "name": "Weekend Brief",
            "description": "Weekend summary",
            "category": "brief",
            "prompt": "Summarize the weekend.",
            "default_cron_expr": "0 9 * * 6",
            "default_timezone": "America/Los_Angeles",
            "default_output_mode": "dashboard_only",
            "default_model": "",
            "default_discord_user_id": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)
    assert resp.headers["location"] == "/templates"

    resp = client.get("/templates")
    assert "Weekend Brief" in resp.text


def test_edit_template_updates_prompt(client_and_factory):
    client, factory = client_and_factory

    async def _daily_id():
        async with factory() as session:
            row = await DigestTemplateRepo(session).get_by_key("daily-brief")
            return row.id

    import anyio

    template_id = anyio.run(_daily_id)
    resp = client.post(
        f"/templates/{template_id}",
        data={
            "name": "My Daily Brief",
            "description": "Edited",
            "category": "brief",
            "prompt": "Use my edited wording.",
            "default_cron_expr": "15 8 * * *",
            "default_timezone": "America/Los_Angeles",
            "default_output_mode": "discord",
            "default_model": "",
            "default_discord_user_id": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303)

    detail = client.get(f"/templates/{template_id}")
    assert "My Daily Brief" in detail.text
    assert "Use my edited wording." in detail.text


def test_clone_template_creates_copy(client_and_factory):
    client, factory = client_and_factory

    async def _daily_id():
        async with factory() as session:
            row = await DigestTemplateRepo(session).get_by_key("daily-brief")
            return row.id

    import anyio

    template_id = anyio.run(_daily_id)
    resp = client.post(f"/templates/{template_id}/clone", follow_redirects=False)
    assert resp.status_code in (302, 303)

    listing = client.get("/templates")
    assert "Daily Brief Copy" in listing.text


def test_disable_user_template_removes_it_from_list(client_and_factory):
    client, _ = client_and_factory
    client.post(
        "/templates",
        data={
            "name": "Temporary",
            "description": "Temporary",
            "category": "custom",
            "prompt": "Temporary prompt.",
            "default_cron_expr": "0 12 * * *",
            "default_timezone": "UTC",
            "default_output_mode": "dashboard_only",
            "default_model": "",
            "default_discord_user_id": "",
        },
        follow_redirects=False,
    )
    listing = client.get("/templates")
    assert "Temporary" in listing.text

    import re

    match = re.search(r'/templates/([^"]+)/disable', listing.text)
    assert match is not None

    resp = client.post(match.group(0), follow_redirects=False)
    assert resp.status_code in (302, 303)
    listing = client.get("/templates")
    assert "Temporary" not in listing.text


def test_disable_built_in_template_is_rejected(client_and_factory):
    client, factory = client_and_factory

    async def _daily_id():
        async with factory() as session:
            row = await DigestTemplateRepo(session).get_by_key("daily-brief")
            return row.id

    import anyio

    template_id = anyio.run(_daily_id)
    resp = client.post(f"/templates/{template_id}/disable")
    assert resp.status_code == 400
    assert "built-in" in resp.text
