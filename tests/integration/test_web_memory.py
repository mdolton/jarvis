from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest_asyncio
from fastapi.testclient import TestClient

from jarvis.memory.preference_dedup import ClusterPreference
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import MemoryEntryRepo, MemoryPreferenceRepo
from jarvis.web.app import create_app


@pytest_asyncio.fixture(loop_scope="function")
async def client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    async with factory() as session:
        preference_repo = MemoryPreferenceRepo(session)
        pending = await preference_repo.create_pending(
            content="Use concise answers.",
            source="user",
        )
        active = await preference_repo.create_pending(
            content="Prefer verification with real checks.",
            source="agent_proposal",
        )
        archived_preference = await preference_repo.create_pending(
            content="Old archived preference.",
            source="user",
        )
        rejected_preference = await preference_repo.create_pending(
            content="Rejected preference.",
            source="agent_proposal",
        )
        await preference_repo.approve(active.id)
        await preference_repo.reject(rejected_preference.id)
        await preference_repo.archive(archived_preference.id)

        entry_repo = MemoryEntryRepo(session)
        active_entry = await entry_repo.create(
            conversation_id=None,
            source_channel_kind="dashboard",
            source_channel_ref="manual",
            summary="User prefers concrete verification and short close-outs.",
            topics=["verification", "workflow"],
            entities=["Jarvis", "Codex"],
            evidence=[
                {
                    "kind": "message",
                    "label": "User instruction",
                    "content": "Use live checks before claiming success.",
                },
                {
                    "kind": "summary",
                    "label": "Run summary",
                    "content": "The user prefers concrete verification.",
                },
            ],
        )
        archived_entry = await entry_repo.create(
            conversation_id=None,
            source_channel_kind="discord",
            source_channel_ref="user-1",
            summary="An archived memory entry.",
            topics=["history"],
            entities=["Jarvis"],
            evidence=[
                {
                    "kind": "message",
                    "label": "Older note",
                    "content": "Archive this later.",
                }
            ],
        )
        await entry_repo.archive(archived_entry.id)

    ctx = SimpleNamespace(session_factory=factory)
    app = create_app(app_context=ctx)
    yield (
        TestClient(app),
        pending.id,
        active.id,
        rejected_preference.id,
        archived_preference.id,
        active_entry.id,
        archived_entry.id,
        factory,
    )
    await engine.dispose()


def test_memory_page_lists_preferences_entries_and_evidence(client):
    c, pending_id, active_id, rejected_preference_id, archived_preference_id, active_entry_id, archived_entry_id, _ = client

    resp = c.get("/memory")

    assert resp.status_code == 200
    assert 'href="/memory"' in resp.text
    assert str(pending_id) in resp.text
    assert str(active_id) in resp.text
    assert str(rejected_preference_id) in resp.text
    assert str(archived_preference_id) in resp.text
    assert "Use concise answers." in resp.text
    assert "Prefer verification with real checks." in resp.text
    assert "Rejected preference." in resp.text
    assert "pending" in resp.text
    assert "active" in resp.text
    assert "rejected" in resp.text
    assert "archived" in resp.text
    assert str(active_entry_id) in resp.text
    assert str(archived_entry_id) in resp.text
    assert "User prefers concrete verification and short close-outs." in resp.text
    assert "verification" in resp.text
    assert "workflow" in resp.text
    assert "Jarvis" in resp.text
    assert "Codex" in resp.text
    assert "User instruction" in resp.text
    assert "Use live checks before claiming success." in resp.text
    assert "Run summary" in resp.text
    assert "The user prefers concrete verification." in resp.text


def test_archive_memory_entry_redirects_and_updates_state(client):
    c, _, _, _, _, active_entry_id, _, factory = client

    resp = c.post(f"/memory/entries/{active_entry_id}/archive", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/memory"

    async def _load_entry():
        async with factory() as session:
            return await MemoryEntryRepo(session).list_recent()

    import anyio

    rows = anyio.run(_load_entry)
    archived = next(row for row in rows if row.id == active_entry_id)
    assert archived.status == "archived"


def test_preference_routes_update_state_and_redirect(client):
    c, pending_id, _, rejected_preference_id, _, _, _, factory = client

    approve_resp = c.post(
        f"/memory/preferences/{pending_id}/approve",
        follow_redirects=False,
    )
    reject_resp = c.post(
        f"/memory/preferences/{rejected_preference_id}/archive",
        follow_redirects=False,
    )

    assert approve_resp.status_code == 303
    assert approve_resp.headers["location"] == "/memory"
    assert reject_resp.status_code == 303
    assert reject_resp.headers["location"] == "/memory"

    async def _load_preferences():
        async with factory() as session:
            return await MemoryPreferenceRepo(session).list_for_dashboard()

    import anyio

    rows = anyio.run(_load_preferences)
    by_id = {row.id: row for row in rows}
    assert by_id[pending_id].status == "active"
    assert by_id[rejected_preference_id].status == "archived"


def test_preference_routes_reject_invalid_transition_and_missing_ids(client):
    c, _, active_id, rejected_preference_id, archived_preference_id, _, _, _ = client
    safe_client = TestClient(c.app, raise_server_exceptions=False)

    invalid_reject = safe_client.post(
        f"/memory/preferences/{active_id}/reject",
        follow_redirects=False,
    )
    invalid_approve = safe_client.post(
        f"/memory/preferences/{rejected_preference_id}/approve",
        follow_redirects=False,
    )
    invalid_archive = safe_client.post(
        f"/memory/preferences/{archived_preference_id}/archive",
        follow_redirects=False,
    )
    missing = safe_client.post(
        f"/memory/preferences/{uuid4()}/approve",
        follow_redirects=False,
    )

    assert invalid_reject.status_code == 409
    assert invalid_reject.json() == {"detail": "invalid preference transition"}
    assert invalid_approve.status_code == 409
    assert invalid_approve.json() == {"detail": "invalid preference transition"}
    assert invalid_archive.status_code == 409
    assert invalid_archive.json() == {"detail": "invalid preference transition"}
    assert missing.status_code == 404
    assert missing.json() == {"detail": "memory preference not found"}


def test_entry_archive_rejects_missing_and_already_archived_rows(client):
    c, _, _, _, _, _, archived_entry_id, _ = client
    safe_client = TestClient(c.app, raise_server_exceptions=False)

    archived = safe_client.post(
        f"/memory/entries/{archived_entry_id}/archive",
        follow_redirects=False,
    )
    missing = safe_client.post(
        f"/memory/entries/{uuid4()}/archive",
        follow_redirects=False,
    )

    assert archived.status_code == 409
    assert archived.json() == {"detail": "invalid memory entry transition"}
    assert missing.status_code == 404
    assert missing.json() == {"detail": "memory entry not found"}


def test_memory_page_hides_invalid_controls_per_row(client):
    (
        c,
        pending_id,
        active_id,
        rejected_preference_id,
        archived_preference_id,
        active_entry_id,
        archived_entry_id,
        _,
    ) = client

    resp = c.get("/memory")

    assert resp.status_code == 200
    assert f'action="/memory/preferences/{pending_id}/approve"' in resp.text
    assert f'action="/memory/preferences/{pending_id}/reject"' in resp.text
    assert f'action="/memory/preferences/{pending_id}/archive"' in resp.text

    assert f'action="/memory/preferences/{active_id}/approve"' not in resp.text
    assert f'action="/memory/preferences/{active_id}/reject"' not in resp.text
    assert f'action="/memory/preferences/{active_id}/archive"' in resp.text

    assert f'action="/memory/preferences/{rejected_preference_id}/approve"' not in resp.text
    assert f'action="/memory/preferences/{rejected_preference_id}/reject"' not in resp.text
    assert f'action="/memory/preferences/{rejected_preference_id}/archive"' in resp.text

    assert f'action="/memory/preferences/{archived_preference_id}/approve"' not in resp.text
    assert f'action="/memory/preferences/{archived_preference_id}/reject"' not in resp.text
    assert f'action="/memory/preferences/{archived_preference_id}/archive"' not in resp.text

    assert f'action="/memory/entries/{active_entry_id}/archive"' in resp.text
    assert f'action="/memory/entries/{archived_entry_id}/archive"' not in resp.text


# ---------------------------------------------------------------------------
# Duplicate preferences tests
# ---------------------------------------------------------------------------


def _cluster_pref(content, status="pending"):
    now = datetime(2026, 6, 1, tzinfo=UTC)
    return ClusterPreference(
        preference_id=uuid4(),
        content=content,
        status=status,
        created_at=now,
        updated_at=now,
        embedding=[1.0, 0.0],
        embedding_dimensions=2,
    )


@pytest_asyncio.fixture(loop_scope="function")
async def memory_client(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)

    keeper = _cluster_pref("Always run tests before committing", status="active")
    dup = _cluster_pref("Run the test suite before each commit")

    ctx = MagicMock()
    ctx.session_factory = factory
    ctx.memory_service.find_duplicate_preferences = AsyncMock(
        return_value=[{"keeper": keeper, "duplicates": [dup]}]
    )

    app = create_app(app_context=ctx)
    client = TestClient(app)
    yield client, dup
    await engine.dispose()


def test_find_duplicates_renders_clusters(memory_client):
    client, dup = memory_client
    resp = client.post("/memory/preferences/find-duplicates")
    assert resp.status_code == 200
    assert "Duplicate groups" in resp.text
    assert "Run the test suite before each commit" in resp.text
    assert f"/memory/preferences/{dup.preference_id}/archive" in resp.text


def test_memory_page_hides_duplicate_section(memory_client):
    client, _ = memory_client
    resp = client.get("/memory")
    assert resp.status_code == 200
    assert "Duplicate groups" not in resp.text
