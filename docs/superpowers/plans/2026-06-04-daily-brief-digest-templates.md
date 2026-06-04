# Daily Brief / Digest Templates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build reusable Daily Brief / Digest templates with built-in seeds and snapshot-based schedule creation.

**Architecture:** Add a first-class `digest_templates` persistence model and repository, seed four built-in templates idempotently at bootstrap, expose template management through FastAPI/Jinja2 dashboard routes, and let `/schedules?template_id=<id>` prefill the existing schedule create form. Schedule execution stays unchanged because templates are copied into normal `ScheduleRow` fields at creation time.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, SQLAlchemy async, Alembic, pytest, ruff.

---

## File Structure

- Create `alembic/versions/0007_digest_templates.py` — creates/drops `digest_templates` and indexes.
- Modify `jarvis/persistence/models.py` — add `DigestTemplateRow`.
- Modify `jarvis/persistence/repositories.py` — add `DigestTemplateRepo`.
- Create `jarvis/digests/__init__.py` — package marker for digest template helpers.
- Create `jarvis/digests/seeds.py` — built-in template definitions and idempotent seed function.
- Modify `jarvis/main.py` — seed built-ins after DB initialization and before routes can list templates.
- Create `jarvis/web/routes/templates.py` — template list, create, edit, clone, and disable routes.
- Create `jarvis/web/templates/templates.html` — template list and create form.
- Create `jarvis/web/templates/template_detail.html` — edit form for a single template.
- Modify `jarvis/web/app.py` — include the templates router.
- Modify `jarvis/web/templates/base.html` — add Templates nav link.
- Modify `jarvis/web/routes/schedules.py` — load template defaults for `GET /schedules?template_id=<id>`.
- Modify `jarvis/web/templates/schedules.html` — render template selector and default form values.
- Add tests:
  - `tests/integration/test_digest_template_migration.py`
  - `tests/integration/test_digest_templates.py`
  - `tests/integration/test_digest_template_seeds.py`
  - `tests/integration/test_web_templates.py`
  - update `tests/integration/test_orm_domain_tables.py`
  - update `tests/integration/test_web_schedules.py`

## Task 1: Persistence and Migration

**Files:**
- Create: `alembic/versions/0007_digest_templates.py`
- Modify: `jarvis/persistence/models.py`
- Modify: `jarvis/persistence/repositories.py`
- Test: `tests/integration/test_digest_template_migration.py`
- Test: `tests/integration/test_digest_templates.py`
- Test: `tests/integration/test_orm_domain_tables.py`

- [ ] **Step 1: Add the failing ORM row test**

Append this test to `tests/integration/test_orm_domain_tables.py`:

```python
from jarvis.persistence.models import DigestTemplateRow


async def test_digest_template_row_roundtrip(session):
    row = DigestTemplateRow(
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
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(row)
    await session.commit()

    result = await session.execute(select(DigestTemplateRow))
    got = result.scalar_one()
    assert got.key == "daily-brief"
    assert got.name == "Daily Brief"
    assert got.default_cron_expr == "0 8 * * *"
    assert got.built_in is True
    assert got.enabled is True
```

- [ ] **Step 2: Add failing repository tests**

Create `tests/integration/test_digest_templates.py`:

```python
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
```

- [ ] **Step 3: Add the failing migration test**

Create `tests/integration/test_digest_template_migration.py`:

```python
import os
import sqlite3
import subprocess
from pathlib import Path


def _run_alembic(db_path: Path, cmd: str) -> subprocess.CompletedProcess:
    cwd = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [
            "uv",
            "run",
            "alembic",
            "-x",
            f"db_url=sqlite+aiosqlite:///{db_path}",
            *cmd.split(),
        ],
        capture_output=True,
        text=True,
        cwd=cwd,
        env={**os.environ},
    )


def test_digest_templates_migration_roundtrip(tmp_path):
    db_path = tmp_path / "test.db"
    up = _run_alembic(db_path, "upgrade head")
    assert up.returncode == 0, up.stderr

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info('digest_templates')").fetchall()
        }
    assert {
        "id",
        "key",
        "name",
        "description",
        "category",
        "prompt",
        "default_cron_expr",
        "default_timezone",
        "default_output_mode",
        "default_model",
        "default_discord_user_id",
        "built_in",
        "enabled",
        "created_at",
        "updated_at",
    }.issubset(columns)

    down = _run_alembic(db_path, "downgrade 0006")
    assert down.returncode == 0, down.stderr

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "digest_templates" not in tables
```

- [ ] **Step 4: Run focused tests and verify red**

Run:

```bash
uv run pytest -q \
  tests/integration/test_orm_domain_tables.py::test_digest_template_row_roundtrip \
  tests/integration/test_digest_templates.py \
  tests/integration/test_digest_template_migration.py
```

Expected: failures because `DigestTemplateRow`, `DigestTemplateRepo`, and migration `0007` do not exist.

- [ ] **Step 5: Add migration `0007_digest_templates.py`**

Create `alembic/versions/0007_digest_templates.py`:

```python
"""add digest templates

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "digest_templates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("default_cron_expr", sa.String(length=64), nullable=False),
        sa.Column("default_timezone", sa.String(length=64), nullable=False),
        sa.Column("default_output_mode", sa.String(length=32), nullable=False),
        sa.Column("default_model", sa.String(length=128), nullable=True),
        sa.Column("default_discord_user_id", sa.String(length=128), nullable=True),
        sa.Column("built_in", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key", name="uq_digest_templates_key"),
    )
    op.create_index(
        "ix_digest_templates_enabled_category_name",
        "digest_templates",
        ["enabled", "category", "name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_digest_templates_enabled_category_name", table_name="digest_templates")
    op.drop_table("digest_templates")
```

- [ ] **Step 6: Add `DigestTemplateRow`**

In `jarvis/persistence/models.py`, add this class after `ScheduleRow`:

```python
class DigestTemplateRow(Base):
    __tablename__ = "digest_templates"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    key: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64))
    prompt: Mapped[str] = mapped_column(Text)
    default_cron_expr: Mapped[str] = mapped_column(String(64))
    default_timezone: Mapped[str] = mapped_column(String(64))
    default_output_mode: Mapped[str] = mapped_column(String(32))
    default_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    default_discord_user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    built_in: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime())
    updated_at: Mapped[datetime] = mapped_column(TZDateTime())

    __table_args__ = (
        Index(
            "ix_digest_templates_enabled_category_name",
            "enabled",
            "category",
            "name",
        ),
    )
```

Also add `DigestTemplateRow` to the import tuple in `jarvis/persistence/repositories.py`.

- [ ] **Step 7: Add `DigestTemplateRepo`**

In `jarvis/persistence/repositories.py`, insert this repository after `ScheduleRepo`:

```python
class DigestTemplateRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        key: str | None,
        name: str,
        description: str,
        category: str,
        prompt: str,
        default_cron_expr: str,
        default_timezone: str,
        default_output_mode: str,
        default_model: str | None,
        default_discord_user_id: str | None,
        built_in: bool,
        enabled: bool,
    ) -> DigestTemplateRow:
        now = _utcnow()
        row = DigestTemplateRow(
            key=key,
            name=name,
            description=description,
            category=category,
            prompt=prompt,
            default_cron_expr=default_cron_expr,
            default_timezone=default_timezone,
            default_output_mode=default_output_mode,
            default_model=default_model,
            default_discord_user_id=default_discord_user_id,
            built_in=built_in,
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def get(self, template_id: UUID) -> DigestTemplateRow | None:
        return await self._session.get(DigestTemplateRow, template_id)

    async def get_by_key(self, key: str) -> DigestTemplateRow | None:
        result = await self._session.execute(
            select(DigestTemplateRow).where(DigestTemplateRow.key == key)
        )
        return result.scalar_one_or_none()

    async def list_enabled(self) -> list[DigestTemplateRow]:
        result = await self._session.execute(
            select(DigestTemplateRow)
            .where(DigestTemplateRow.enabled.is_(True))
            .order_by(DigestTemplateRow.category.asc(), DigestTemplateRow.name.asc())
        )
        return list(result.scalars())

    async def list_all(self) -> list[DigestTemplateRow]:
        result = await self._session.execute(
            select(DigestTemplateRow).order_by(
                DigestTemplateRow.enabled.desc(),
                DigestTemplateRow.category.asc(),
                DigestTemplateRow.name.asc(),
            )
        )
        return list(result.scalars())

    async def update(
        self,
        template_id: UUID,
        *,
        name: str,
        description: str,
        category: str,
        prompt: str,
        default_cron_expr: str,
        default_timezone: str,
        default_output_mode: str,
        default_model: str | None,
        default_discord_user_id: str | None,
    ) -> None:
        await self._session.execute(
            update(DigestTemplateRow)
            .where(DigestTemplateRow.id == template_id)
            .values(
                name=name,
                description=description,
                category=category,
                prompt=prompt,
                default_cron_expr=default_cron_expr,
                default_timezone=default_timezone,
                default_output_mode=default_output_mode,
                default_model=default_model,
                default_discord_user_id=default_discord_user_id,
                updated_at=_utcnow(),
            )
        )
        await self._session.commit()

    async def clone(self, template_id: UUID) -> DigestTemplateRow:
        original = await self.get(template_id)
        if original is None:
            raise ValueError(f"digest template {template_id} not found")
        return await self.create(
            key=None,
            name=f"{original.name} Copy",
            description=original.description,
            category=original.category,
            prompt=original.prompt,
            default_cron_expr=original.default_cron_expr,
            default_timezone=original.default_timezone,
            default_output_mode=original.default_output_mode,
            default_model=original.default_model,
            default_discord_user_id=original.default_discord_user_id,
            built_in=False,
            enabled=True,
        )

    async def disable(self, template_id: UUID) -> None:
        row = await self.get(template_id)
        if row is None:
            raise ValueError(f"digest template {template_id} not found")
        if row.built_in:
            raise ValueError("built-in digest templates cannot be disabled")
        row.enabled = False
        row.updated_at = _utcnow()
        await self._session.commit()
```

- [ ] **Step 8: Run focused tests and verify green**

Run:

```bash
uv run pytest -q \
  tests/integration/test_orm_domain_tables.py::test_digest_template_row_roundtrip \
  tests/integration/test_digest_templates.py \
  tests/integration/test_digest_template_migration.py
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit persistence slice**

Run:

```bash
git add \
  alembic/versions/0007_digest_templates.py \
  jarvis/persistence/models.py \
  jarvis/persistence/repositories.py \
  tests/integration/test_digest_template_migration.py \
  tests/integration/test_digest_templates.py \
  tests/integration/test_orm_domain_tables.py
git commit -m "feat: add digest template persistence"
```

## Task 2: Built-In Seed Templates

**Files:**
- Create: `jarvis/digests/__init__.py`
- Create: `jarvis/digests/seeds.py`
- Modify: `jarvis/main.py`
- Test: `tests/integration/test_digest_template_seeds.py`

- [ ] **Step 1: Add failing seed tests**

Create `tests/integration/test_digest_template_seeds.py`:

```python
import pytest

from jarvis.digests.seeds import BUILT_IN_DIGEST_TEMPLATES, seed_built_in_digest_templates
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
```

- [ ] **Step 2: Run seed tests and verify red**

Run:

```bash
uv run pytest -q tests/integration/test_digest_template_seeds.py
```

Expected: failure because `jarvis.digests.seeds` does not exist.

- [ ] **Step 3: Create the digest package**

Create `jarvis/digests/__init__.py`:

```python
"""Digest template helpers."""
```

- [ ] **Step 4: Add seed definitions and seed function**

Create `jarvis/digests/seeds.py`:

```python
"""Built-in digest templates and idempotent database seeding."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.persistence.repositories import DigestTemplateRepo


@dataclass(frozen=True, slots=True)
class DigestTemplateSeed:
    key: str
    name: str
    description: str
    category: str
    prompt: str
    default_cron_expr: str
    default_timezone: str = "UTC"
    default_output_mode: str = "discord"


BUILT_IN_DIGEST_TEMPLATES: tuple[DigestTemplateSeed, ...] = (
    DigestTemplateSeed(
        key="action-inbox-review",
        name="Action Inbox Review",
        description="Review pending approvals and unresolved agent work.",
        category="operations",
        default_cron_expr="0 16 * * 1-5",
        prompt=(
            "Review pending Action Inbox items if that information is available. "
            "Summarize what each pending action is waiting on, group stale or "
            "risky items first, and suggest approve or reject follow-up where "
            "the context is clear. Stay read-only unless an action is explicitly "
            "approved through the Action Inbox flow."
        ),
    ),
    DigestTemplateSeed(
        key="calendar-brief",
        name="Calendar Brief",
        description="Summarize schedule awareness and meeting preparation.",
        category="brief",
        default_cron_expr="30 7 * * *",
        prompt=(
            "Summarize today's calendar. Identify preparation tasks, travel "
            "buffers, and conflicts. Note tomorrow morning's first commitment "
            "when useful. Keep the response short and suitable for Discord."
        ),
    ),
    DigestTemplateSeed(
        key="daily-brief",
        name="Daily Brief",
        description="Morning summary across calendar, email, actions, and time-sensitive items.",
        category="brief",
        default_cron_expr="0 8 * * *",
        prompt=(
            "Prepare my daily brief for today. Summarize today's calendar, flag "
            "schedule conflicts and preparation items, summarize important unread "
            "or recent email if mail tools are available, include pending Action "
            "Inbox items if available, and end with a short prioritized action "
            "list. Keep it concise and suitable for Discord."
        ),
    ),
    DigestTemplateSeed(
        key="email-digest",
        name="Email Digest",
        description="Summarize important recent email activity.",
        category="digest",
        default_cron_expr="0 9 * * 1-5",
        prompt=(
            "Review recent unread and important messages. Group findings by "
            "sender or topic, identify messages needing a reply, call out "
            "receipts, travel, bills, or operational alerts, and avoid listing "
            "low-value notification noise."
        ),
    ),
)


async def seed_built_in_digest_templates(session: AsyncSession) -> None:
    repo = DigestTemplateRepo(session)
    for seed in BUILT_IN_DIGEST_TEMPLATES:
        existing = await repo.get_by_key(seed.key)
        if existing is not None:
            continue
        await repo.create(
            key=seed.key,
            name=seed.name,
            description=seed.description,
            category=seed.category,
            prompt=seed.prompt,
            default_cron_expr=seed.default_cron_expr,
            default_timezone=seed.default_timezone,
            default_output_mode=seed.default_output_mode,
            default_model=None,
            default_discord_user_id=None,
            built_in=True,
            enabled=True,
        )
```

- [ ] **Step 5: Wire seeding into bootstrap**

In `jarvis/main.py`, add the import:

```python
from jarvis.digests.seeds import seed_built_in_digest_templates
```

After `factory = session_factory(engine)`, add:

```python
    async with factory() as session:
        await seed_built_in_digest_templates(session)
```

The block belongs before `AuditLogger` starts so templates are available before the dashboard is built.

- [ ] **Step 6: Run seed tests and a bootstrap smoke test**

Run:

```bash
uv run pytest -q \
  tests/integration/test_digest_template_seeds.py \
  tests/integration/test_main_smoke.py::test_bootstrap_exposes_scheduler
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit seed slice**

Run:

```bash
git add \
  jarvis/digests/__init__.py \
  jarvis/digests/seeds.py \
  jarvis/main.py \
  tests/integration/test_digest_template_seeds.py
git commit -m "feat: seed built-in digest templates"
```

## Task 3: Templates Dashboard

**Files:**
- Create: `jarvis/web/routes/templates.py`
- Create: `jarvis/web/templates/templates.html`
- Create: `jarvis/web/templates/template_detail.html`
- Modify: `jarvis/web/app.py`
- Modify: `jarvis/web/templates/base.html`
- Test: `tests/integration/test_web_templates.py`

- [ ] **Step 1: Add failing web route tests**

Create `tests/integration/test_web_templates.py`:

```python
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
    client = TestClient(app)
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
```

- [ ] **Step 2: Run web template tests and verify red**

Run:

```bash
uv run pytest -q tests/integration/test_web_templates.py
```

Expected: 404 or import failure because the templates router and views do not exist.

- [ ] **Step 3: Add templates routes**

Create `jarvis/web/routes/templates.py`:

```python
"""Digest template dashboard routes."""

from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jarvis.persistence.repositories import DigestTemplateRepo

router = APIRouter()


@router.get("/templates", response_class=HTMLResponse)
async def template_list(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    async with ctx.session_factory() as session:
        rows = await DigestTemplateRepo(session).list_enabled()
    return templates.TemplateResponse(request, "templates.html", {"templates": rows})


@router.post("/templates")
async def template_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    category: str = Form("custom"),
    prompt: str = Form(...),
    default_cron_expr: str = Form(...),
    default_timezone: str = Form("UTC"),
    default_output_mode: str = Form("discord"),
    default_model: str = Form(""),
    default_discord_user_id: str = Form(""),
):
    _validate_template_form(
        name=name,
        prompt=prompt,
        default_cron_expr=default_cron_expr,
        default_timezone=default_timezone,
        default_output_mode=default_output_mode,
    )
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        await DigestTemplateRepo(session).create(
            key=None,
            name=name.strip(),
            description=description.strip(),
            category=category.strip() or "custom",
            prompt=prompt.strip(),
            default_cron_expr=default_cron_expr.strip(),
            default_timezone=default_timezone.strip(),
            default_output_mode=default_output_mode.strip(),
            default_model=default_model.strip() or None,
            default_discord_user_id=default_discord_user_id.strip() or None,
            built_in=False,
            enabled=True,
        )
    return RedirectResponse(url="/templates", status_code=303)


@router.get("/templates/new", response_class=HTMLResponse)
async def template_new(request: Request):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    async with ctx.session_factory() as session:
        rows = await DigestTemplateRepo(session).list_enabled()
    return templates.TemplateResponse(request, "templates.html", {"templates": rows})


@router.get("/templates/{template_id}", response_class=HTMLResponse)
async def template_detail(request: Request, template_id: UUID):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    async with ctx.session_factory() as session:
        row = await DigestTemplateRepo(session).get(template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return templates.TemplateResponse(request, "template_detail.html", {"template": row})


@router.post("/templates/{template_id}")
async def template_update(
    request: Request,
    template_id: UUID,
    name: str = Form(...),
    description: str = Form(""),
    category: str = Form("custom"),
    prompt: str = Form(...),
    default_cron_expr: str = Form(...),
    default_timezone: str = Form("UTC"),
    default_output_mode: str = Form("discord"),
    default_model: str = Form(""),
    default_discord_user_id: str = Form(""),
):
    _validate_template_form(
        name=name,
        prompt=prompt,
        default_cron_expr=default_cron_expr,
        default_timezone=default_timezone,
        default_output_mode=default_output_mode,
    )
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        repo = DigestTemplateRepo(session)
        row = await repo.get(template_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Template not found")
        await repo.update(
            template_id,
            name=name.strip(),
            description=description.strip(),
            category=category.strip() or "custom",
            prompt=prompt.strip(),
            default_cron_expr=default_cron_expr.strip(),
            default_timezone=default_timezone.strip(),
            default_output_mode=default_output_mode.strip(),
            default_model=default_model.strip() or None,
            default_discord_user_id=default_discord_user_id.strip() or None,
        )
    return RedirectResponse(url=f"/templates/{template_id}", status_code=303)


@router.post("/templates/{template_id}/clone")
async def template_clone(request: Request, template_id: UUID):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        try:
            await DigestTemplateRepo(session).clone(template_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(url="/templates", status_code=303)


@router.post("/templates/{template_id}/disable")
async def template_disable(request: Request, template_id: UUID):
    ctx = request.app.state.ctx
    async with ctx.session_factory() as session:
        try:
            await DigestTemplateRepo(session).disable(template_id)
        except ValueError as exc:
            status = 400 if "built-in" in str(exc) else 404
            raise HTTPException(status_code=status, detail=str(exc)) from exc
    return RedirectResponse(url="/templates", status_code=303)


def _validate_template_form(
    *,
    name: str,
    prompt: str,
    default_cron_expr: str,
    default_timezone: str,
    default_output_mode: str,
) -> None:
    if not name.strip():
        raise HTTPException(status_code=400, detail="Template name is required")
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="Template prompt is required")
    if not default_cron_expr.strip():
        raise HTTPException(status_code=400, detail="Default cron expression is required")
    if not default_timezone.strip():
        raise HTTPException(status_code=400, detail="Default timezone is required")
    if default_output_mode not in {"discord", "dashboard_only", "discord_if_noteworthy"}:
        raise HTTPException(status_code=400, detail="Invalid default output mode")
```

- [ ] **Step 4: Register templates router and nav**

In `jarvis/web/app.py`, include the router after schedules:

```python
    from jarvis.web.routes.templates import router as templates_router

    app.include_router(templates_router)
```

In `jarvis/web/templates/base.html`, add the nav link after Schedules:

```html
            <a href="/templates">Templates</a>
```

- [ ] **Step 5: Add templates list page**

Create `jarvis/web/templates/templates.html`:

```html
{% extends "base.html" %}
{% block title %}Templates{% endblock %}
{% block content %}
<section class="page-head">
    <div>
        <h1>Templates</h1>
        <p class="muted">Reusable prompt defaults for schedule creation.</p>
    </div>
</section>

<section class="section-block">
<h2>Create Template</h2>
<form method="post" action="/templates" class="stack-form">
    <input name="name" placeholder="Name" required>
    <input name="description" placeholder="Description">
    <input name="category" placeholder="Category" value="custom" required>
    <input name="default_cron_expr" placeholder="Cron expression (e.g. 0 8 * * *)" required>
    <input name="default_timezone" placeholder="Timezone" value="UTC" required>
    <select name="default_output_mode">
        <option value="discord">Discord</option>
        <option value="dashboard_only">Dashboard only</option>
        <option value="discord_if_noteworthy">Discord if noteworthy</option>
    </select>
    <input name="default_model" placeholder="Default model (optional)">
    <input name="default_discord_user_id" placeholder="Discord user ID (optional)">
    <textarea name="prompt" placeholder="Template prompt" rows="6" required></textarea>
    <button type="submit">Create</button>
</form>
</section>

<section class="section-block">
<h2>Template Library</h2>
<table class="ops-table">
    <thead>
        <tr><th>Name</th><th>Category</th><th>Cron</th><th>Output</th><th>Prompt</th><th>Actions</th></tr>
    </thead>
    <tbody>
    {% for t in templates %}
        <tr>
            <td>
                <a href="/templates/{{ t.id }}">{{ t.name }}</a>
                {% if t.built_in %}<span class="badge badge-ok">built-in</span>{% endif %}
            </td>
            <td>{{ t.category }}</td>
            <td><code>{{ t.default_cron_expr }}</code></td>
            <td>{{ t.default_output_mode }}</td>
            <td class="muted">{{ t.prompt[:140] }}{% if t.prompt|length > 140 %}...{% endif %}</td>
            <td class="actions">
                <a href="/schedules?template_id={{ t.id }}">Create schedule</a>
                <form method="post" action="/templates/{{ t.id }}/clone" class="inline-form">
                    <button>Clone</button>
                </form>
                {% if not t.built_in %}
                <form method="post" action="/templates/{{ t.id }}/disable" class="inline-form">
                    <button class="btn-danger">Disable</button>
                </form>
                {% endif %}
            </td>
        </tr>
    {% endfor %}
    </tbody>
</table>
{% if not templates %}
<p class="muted">No templates yet.</p>
{% endif %}
</section>
{% endblock %}
```

- [ ] **Step 6: Add template detail page**

Create `jarvis/web/templates/template_detail.html`:

```html
{% extends "base.html" %}
{% block title %}Template{% endblock %}
{% block content %}
<section class="page-head">
    <div>
        <h1>{{ template.name }}</h1>
        <p class="muted">
            {{ template.category }}
            {% if template.built_in %}<span class="badge badge-ok">built-in</span>{% endif %}
        </p>
    </div>
    <div class="actions">
        <a href="/schedules?template_id={{ template.id }}">Create schedule</a>
        <form method="post" action="/templates/{{ template.id }}/clone" class="inline-form">
            <button>Clone</button>
        </form>
    </div>
</section>

<section class="section-block">
<h2>Edit Template</h2>
<form method="post" action="/templates/{{ template.id }}" class="stack-form">
    <input name="name" placeholder="Name" value="{{ template.name }}" required>
    <input name="description" placeholder="Description" value="{{ template.description }}">
    <input name="category" placeholder="Category" value="{{ template.category }}" required>
    <input name="default_cron_expr" placeholder="Cron expression" value="{{ template.default_cron_expr }}" required>
    <input name="default_timezone" placeholder="Timezone" value="{{ template.default_timezone }}" required>
    <select name="default_output_mode">
        <option value="discord" {% if template.default_output_mode == "discord" %}selected{% endif %}>Discord</option>
        <option value="dashboard_only" {% if template.default_output_mode == "dashboard_only" %}selected{% endif %}>Dashboard only</option>
        <option value="discord_if_noteworthy" {% if template.default_output_mode == "discord_if_noteworthy" %}selected{% endif %}>Discord if noteworthy</option>
    </select>
    <input name="default_model" placeholder="Default model (optional)" value="{{ template.default_model or "" }}">
    <input name="default_discord_user_id" placeholder="Discord user ID (optional)" value="{{ template.default_discord_user_id or "" }}">
    <textarea name="prompt" rows="10" required>{{ template.prompt }}</textarea>
    <button type="submit">Save</button>
</form>
</section>
{% endblock %}
```

- [ ] **Step 7: Run web template tests and verify green**

Run:

```bash
uv run pytest -q tests/integration/test_web_templates.py
```

Expected: all tests in `test_web_templates.py` pass.

- [ ] **Step 8: Commit dashboard slice**

Run:

```bash
git add \
  jarvis/web/app.py \
  jarvis/web/routes/templates.py \
  jarvis/web/templates/base.html \
  jarvis/web/templates/template_detail.html \
  jarvis/web/templates/templates.html \
  tests/integration/test_web_templates.py
git commit -m "feat: manage digest templates from dashboard"
```

## Task 4: Schedule Creation From Templates

**Files:**
- Modify: `jarvis/web/routes/schedules.py`
- Modify: `jarvis/web/templates/schedules.html`
- Test: `tests/integration/test_web_schedules.py`

- [ ] **Step 1: Add failing schedule prefill tests**

Append these tests to `tests/integration/test_web_schedules.py`:

```python
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


def test_create_schedule_from_template_snapshot_persists_normal_schedule(client_and_factory):
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
```

- [ ] **Step 2: Run schedule tests and verify red**

Run:

```bash
uv run pytest -q \
  tests/integration/test_web_schedules.py::test_schedules_page_can_prefill_from_template \
  tests/integration/test_web_schedules.py::test_schedules_page_lists_templates_in_selector \
  tests/integration/test_web_schedules.py::test_create_schedule_from_template_snapshot_persists_normal_schedule
```

Expected: failures because `/schedules` does not load templates or prefill defaults yet.

- [ ] **Step 3: Load templates and selected template in schedules route**

In `jarvis/web/routes/schedules.py`, update imports:

```python
from uuid import UUID

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from jarvis.persistence.repositories import DigestTemplateRepo, ScheduleRepo
```

Change the route signature and body:

```python
@router.get("/schedules", response_class=HTMLResponse)
async def schedule_list(request: Request, template_id: UUID | None = Query(default=None)):
    ctx = request.app.state.ctx
    templates = request.app.state.templates
    catalog = await ctx.model_catalog.list_models()
    template_warning = None
    selected_template = None
    async with ctx.session_factory() as session:
        schedule_repo = ScheduleRepo(session)
        template_repo = DigestTemplateRepo(session)
        schedules = await schedule_repo.list_all()
        digest_templates = await template_repo.list_enabled()
        if template_id is not None:
            selected_template = await template_repo.get(template_id)
            if selected_template is None or not selected_template.enabled:
                selected_template = None
                template_warning = "Template not found or disabled."
    available = set(catalog.models) if catalog.ok else None
    return templates.TemplateResponse(
        request,
        "schedules.html",
        {
            "schedules": schedules,
            "available_models": catalog.models,
            "catalog_ok": catalog.ok,
            "available_set": available,
            "digest_templates": digest_templates,
            "selected_template": selected_template,
            "template_warning": template_warning,
        },
    )
```

- [ ] **Step 4: Update schedule form to render template defaults**

In `jarvis/web/templates/schedules.html`, replace the create form block with:

```html
<form method="get" action="/schedules" class="stack-form">
    <select name="template_id">
        <option value="">Start from blank schedule</option>
        {% for t in digest_templates %}
            <option value="{{ t.id }}" {% if selected_template and selected_template.id == t.id %}selected{% endif %}>{{ t.name }}</option>
        {% endfor %}
    </select>
    <button type="submit">Load template</button>
</form>
{% if template_warning %}
<p class="muted">{{ template_warning }}</p>
{% endif %}
<form method="post" action="/schedules" class="stack-form">
    <input name="name" placeholder="Name" value="{{ selected_template.name if selected_template else "" }}" required>
    <input name="description" placeholder="Description" value="{{ selected_template.description if selected_template else "" }}">
    <input name="cron_expr" placeholder="Cron expression (e.g. 0 8 * * *)" value="{{ selected_template.default_cron_expr if selected_template else "" }}" required>
    <input name="timezone" placeholder="Timezone" value="{{ selected_template.default_timezone if selected_template else "UTC" }}">
    <textarea name="prompt" placeholder="Agent prompt" rows="6" required>{{ selected_template.prompt if selected_template else "" }}</textarea>
    <select name="output_mode">
        {% set selected_output = selected_template.default_output_mode if selected_template else "discord" %}
        <option value="discord" {% if selected_output == "discord" %}selected{% endif %}>Discord</option>
        <option value="dashboard_only" {% if selected_output == "dashboard_only" %}selected{% endif %}>Dashboard only</option>
        <option value="discord_if_noteworthy" {% if selected_output == "discord_if_noteworthy" %}selected{% endif %}>Discord if noteworthy</option>
    </select>
    <input name="discord_user_id" placeholder="Discord user ID (optional)" value="{{ selected_template.default_discord_user_id if selected_template and selected_template.default_discord_user_id else "" }}">
    <select name="model">
        {% set selected_model = selected_template.default_model if selected_template else "" %}
        <option value="">Use default model</option>
        {% for m in available_models %}
            <option value="{{ m }}" {% if selected_model == m %}selected{% endif %}>{{ m }}</option>
        {% endfor %}
    </select>
    <button type="submit">Create</button>
</form>
```

Keep the rest of the existing schedules table unchanged.

- [ ] **Step 5: Run focused schedule tests and verify green**

Run:

```bash
uv run pytest -q tests/integration/test_web_schedules.py
```

Expected: all schedule web tests pass.

- [ ] **Step 6: Commit schedule integration slice**

Run:

```bash
git add \
  jarvis/web/routes/schedules.py \
  jarvis/web/templates/schedules.html \
  tests/integration/test_web_schedules.py
git commit -m "feat: create schedules from digest templates"
```

## Task 5: Documentation and Final Verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update README scheduled task docs**

In `README.md`, in the "Scheduled Tasks" section after the current bullet list, add:

```markdown
Digest templates are available from the **Templates** page. Built-in templates
include Daily Brief, Email Digest, Calendar Brief, and Action Inbox Review.
Creating a schedule from a template copies the template fields into the schedule;
future template edits do not change existing schedules.
```

In the "Dashboard" list, add:

```markdown
- **Templates** — create, edit, clone, and apply reusable digest templates for schedules
```

- [ ] **Step 2: Run lint**

Run:

```bash
uv run ruff check jarvis tests
```

Expected: exits 0.

- [ ] **Step 3: Run full test suite**

Run:

```bash
uv run pytest -vv --durations=20
```

Expected: exits 0. If a scheduler test starts a real scheduler and calls `fire_now()`, keep far-future cron expressions in tests to avoid duplicate APScheduler fires.

- [ ] **Step 4: Review diff**

Run:

```bash
git diff --stat main...
git diff --check
git status --short
```

Expected: changes are limited to digest template persistence, seeds, routes, dashboard templates, schedule prefill, tests, docs, and the approved design/plan docs. `git diff --check` exits 0.

- [ ] **Step 5: Commit docs and verification cleanup**

Run:

```bash
git add README.md docs/superpowers/plans/2026-06-04-daily-brief-digest-templates.md
git commit -m "docs: document digest templates"
```

If `README.md` was already committed with an earlier implementation slice, stage and commit only the plan file:

```bash
git add docs/superpowers/plans/2026-06-04-daily-brief-digest-templates.md
git commit -m "docs: plan digest templates implementation"
```

## Browser Verification

After implementation and tests pass, start a local server if one is not already running:

```bash
uv run python -m jarvis serve
```

Open the dashboard and verify:

- `/templates` lists the four built-in templates.
- Editing `Daily Brief` persists local wording.
- Cloning `Email Digest` creates `Email Digest Copy`.
- `/schedules?template_id=<daily-brief-id>` preloads name, description, cron, timezone, output mode, prompt, model, and Discord fields.
- Creating the schedule produces a normal schedule row visible in `/schedules`.

If port `8080` is occupied, use the app's configured URL or stop the conflicting local process after confirming it is not user work.

## Plan Self-Review

Spec coverage:

- Durable template records: Task 1.
- Built-in seeds and idempotent startup behavior: Task 2.
- Dashboard list/new/create/edit/clone/disable behavior: Task 3.
- Snapshot schedule creation: Task 4.
- Existing scheduler execution unchanged: Task 4 tests persist a normal `ScheduleRow`; no scheduler code changes are planned.
- Documentation and final checks: Task 5.

Placeholder scan: no open-ended implementation placeholders remain in this plan.

Type consistency: the plan consistently uses `DigestTemplateRow`, `DigestTemplateRepo`, `digest_templates`, `default_cron_expr`, `default_timezone`, `default_output_mode`, `default_model`, and `default_discord_user_id`.
