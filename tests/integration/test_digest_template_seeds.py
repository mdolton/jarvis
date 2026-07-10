import pytest

from jarvis.digests.seeds import BUILT_IN_DIGEST_TEMPLATES, seed_built_in_digest_templates
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import DigestTemplateRepo, ScheduleRepo

_DAILY_BRIEF = next(s for s in BUILT_IN_DIGEST_TEMPLATES if s.key == "daily-brief")


@pytest.fixture
async def session(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_seed_built_in_digest_templates_creates_expected_records(session):
    await seed_built_in_digest_templates(session)

    rows = await DigestTemplateRepo(session).list_enabled()
    assert [row.key for row in rows] == [
        "calendar-brief",
        "daily-brief",
        "email-digest",
        "action-inbox-review",
    ]
    assert {row.name for row in rows} == {
        "Action Inbox Review",
        "Calendar Brief",
        "Daily Brief",
        "Email Digest",
    }
    assert all(row.built_in for row in rows)
    assert all(row.enabled for row in rows)


async def test_seed_built_in_digest_templates_is_idempotent(session):
    await seed_built_in_digest_templates(session)
    await seed_built_in_digest_templates(session)

    rows = await DigestTemplateRepo(session).list_enabled()
    assert len(rows) == len(BUILT_IN_DIGEST_TEMPLATES)
    assert len({row.key for row in rows}) == len(BUILT_IN_DIGEST_TEMPLATES)


async def test_seed_built_in_digest_templates_preserves_local_edits(session):
    repo = DigestTemplateRepo(session)
    await seed_built_in_digest_templates(session)
    daily = await repo.get_by_key("daily-brief")
    assert daily is not None

    await repo.update(
        daily.id,
        name="My Morning Brief",
        description=daily.description,
        category=daily.category,
        prompt="Use my local wording.",
        default_cron_expr=daily.default_cron_expr,
        default_timezone=daily.default_timezone,
        default_output_mode=daily.default_output_mode,
        default_model=daily.default_model,
        default_discord_user_id=daily.default_discord_user_id,
    )

    await seed_built_in_digest_templates(session)
    edited = await repo.get_by_key("daily-brief")
    assert edited.name == "My Morning Brief"
    assert edited.prompt == "Use my local wording."


async def test_seed_rolls_forward_template_prompt_from_previous_seed_version(session):
    repo = DigestTemplateRepo(session)
    await seed_built_in_digest_templates(session)
    daily = await repo.get_by_key("daily-brief")

    # Simulate a DB seeded by an older release: the row carries the previous
    # seed prompt verbatim.
    old_prompt = _DAILY_BRIEF.previous_prompts[0]
    await repo.refresh_prompt(daily.id, prompt=old_prompt)

    await seed_built_in_digest_templates(session)

    refreshed = await repo.get_by_key("daily-brief")
    assert refreshed.prompt == _DAILY_BRIEF.prompt


async def test_seed_rolls_forward_uncustomized_schedule_prompts(session):
    await seed_built_in_digest_templates(session)
    schedule_repo = ScheduleRepo(session)

    old_prompt = _DAILY_BRIEF.previous_prompts[0]
    untouched = await schedule_repo.create(
        name="Daily Brief",
        description="",
        cron_expr="0 8 * * *",
        timezone="UTC",
        prompt=old_prompt,
        output_mode="discord",
        notify_on_error=True,
        enabled=True,
        model=None,
        discord_user_id=None,
    )
    customized = await schedule_repo.create(
        name="My Brief",
        description="",
        cron_expr="0 8 * * *",
        timezone="UTC",
        prompt=old_prompt + " Also include a haiku.",
        output_mode="discord",
        notify_on_error=True,
        enabled=True,
        model=None,
        discord_user_id=None,
    )

    await seed_built_in_digest_templates(session)

    assert (await schedule_repo.get(untouched.id)).prompt == _DAILY_BRIEF.prompt
    assert (await schedule_repo.get(customized.id)).prompt == old_prompt + " Also include a haiku."
