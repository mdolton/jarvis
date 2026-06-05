from uuid import uuid4

import pytest

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import DigestTemplateRepo


@pytest.fixture
async def session(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_digest_template_repo_create_get_and_list_enabled(session):
    repo = DigestTemplateRepo(session)
    created = await repo.create(
        key=None,
        name="Weekend Brief",
        description="Personal weekend summary",
        category="brief",
        prompt="Summarize the weekend.",
        default_cron_expr="0 9 * * 6",
        default_timezone="America/Los_Angeles",
        default_output_mode="dashboard_only",
        default_model=None,
        default_discord_user_id=None,
        built_in=False,
        enabled=True,
    )

    got = await repo.get(created.id)
    assert got is not None
    assert got.name == "Weekend Brief"
    assert got.default_timezone == "America/Los_Angeles"

    enabled = await repo.list_enabled()
    assert [t.id for t in enabled] == [created.id]


async def test_digest_template_repo_update_changes_editable_fields(session):
    repo = DigestTemplateRepo(session)
    created = await repo.create(
        key="daily-brief",
        name="Daily Brief",
        description="Morning summary",
        category="brief",
        prompt="Summarize today.",
        default_cron_expr="0 8 * * *",
        default_timezone="UTC",
        default_output_mode="discord",
        default_model=None,
        default_discord_user_id=None,
        built_in=True,
        enabled=True,
    )

    await repo.update(
        created.id,
        name="Daily Operations Brief",
        description="Updated summary",
        category="brief",
        prompt="Summarize calendar and mail.",
        default_cron_expr="30 8 * * *",
        default_timezone="America/Los_Angeles",
        default_output_mode="discord_if_noteworthy",
        default_model="alpha",
        default_discord_user_id="123",
    )

    got = await repo.get(created.id)
    assert got.name == "Daily Operations Brief"
    assert got.prompt == "Summarize calendar and mail."
    assert got.default_cron_expr == "30 8 * * *"
    assert got.default_model == "alpha"
    assert got.key == "daily-brief"
    assert got.built_in is True


async def test_digest_template_repo_clone_creates_user_owned_copy(session):
    repo = DigestTemplateRepo(session)
    original = await repo.create(
        key="email-digest",
        name="Email Digest",
        description="Mail summary",
        category="digest",
        prompt="Summarize important mail.",
        default_cron_expr="0 9 * * 1-5",
        default_timezone="UTC",
        default_output_mode="discord",
        default_model=None,
        default_discord_user_id=None,
        built_in=True,
        enabled=True,
    )

    clone = await repo.clone(original.id)

    assert clone.id != original.id
    assert clone.key is None
    assert clone.name == "Email Digest Copy"
    assert clone.prompt == original.prompt
    assert clone.built_in is False
    assert clone.enabled is True


async def test_digest_template_repo_disable_rejects_built_in(session):
    repo = DigestTemplateRepo(session)
    built_in = await repo.create(
        key="calendar-brief",
        name="Calendar Brief",
        description="Calendar summary",
        category="brief",
        prompt="Summarize calendar.",
        default_cron_expr="30 7 * * *",
        default_timezone="UTC",
        default_output_mode="discord",
        default_model=None,
        default_discord_user_id=None,
        built_in=True,
        enabled=True,
    )

    with pytest.raises(ValueError, match="built-in"):
        await repo.disable(built_in.id)


async def test_digest_template_repo_disable_hides_user_template(session):
    repo = DigestTemplateRepo(session)
    user_template = await repo.create(
        key=None,
        name="Temporary",
        description="Temporary template",
        category="custom",
        prompt="Run once.",
        default_cron_expr="0 12 * * *",
        default_timezone="UTC",
        default_output_mode="dashboard_only",
        default_model=None,
        default_discord_user_id=None,
        built_in=False,
        enabled=True,
    )

    await repo.disable(user_template.id)

    got = await repo.get(user_template.id)
    assert got.enabled is False
    assert await repo.list_enabled() == []


async def test_digest_template_repo_get_missing_returns_none(session):
    repo = DigestTemplateRepo(session)
    assert await repo.get(uuid4()) is None
