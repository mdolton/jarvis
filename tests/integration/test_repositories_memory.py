from datetime import UTC, datetime
from uuid import uuid4

import pytest

from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.models import ConversationRow, TriggerRow
from jarvis.persistence.repositories import MemoryEntryRepo, MemoryPreferenceRepo, MemoryRecallRepo


@pytest.fixture
async def session(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    async with factory() as s:
        yield s
    await engine.dispose()


async def test_memory_preference_repo_lifecycle(session):
    repo = MemoryPreferenceRepo(session)

    preference = await repo.create_pending(content="Use concise answers", source="user")

    assert preference.status == "pending"
    assert preference.content == "Use concise answers"

    await repo.approve(preference.id)
    active = await repo.list_active()
    assert [row.content for row in active] == ["Use concise answers"]

    await repo.archive(preference.id)
    assert await repo.list_active() == []


async def test_memory_preference_repo_duplicate_create_pending_keeps_session_usable(session):
    repo = MemoryPreferenceRepo(session)

    first = await repo.create_pending(content="Use concise answers", source="user")
    second = await repo.create_pending(content=" use concise answers ", source="agent_proposal")
    rows = await repo.list_for_dashboard()

    assert second.id == first.id
    assert [row.id for row in rows] == [first.id]


async def test_memory_entry_repo_lifecycle(session):
    repo = MemoryEntryRepo(session)
    conversation_id = uuid4()
    session.add(
        ConversationRow(
            id=conversation_id,
            channel_kind="discord",
            channel_ref="123",
            started_at=datetime.now(UTC),
            last_activity_at=datetime.now(UTC),
            status="open",
        )
    )
    await session.commit()

    entry = await repo.create(
        conversation_id=conversation_id,
        source_channel_kind="discord",
        source_channel_ref="123",
        summary="User prefers concrete verification.",
        topics=["verification", "workflow"],
        entities=["Jarvis"],
        evidence=[
            {
                "kind": "message",
                "label": "User instruction",
                "content": "Use live checks.",
            }
        ],
    )

    recent = await repo.list_recent()
    assert [row.id for row in recent] == [entry.id]
    assert recent[0].topics == ["verification", "workflow"]
    assert recent[0].entities == ["Jarvis"]

    evidence = await repo.list_evidence(entry.id)
    assert len(evidence) == 1
    assert evidence[0].memory_entry_id == entry.id
    assert evidence[0].kind == "message"
    assert evidence[0].label == "User instruction"
    assert evidence[0].content == "Use live checks."

    assert [row.id for row in await repo.list_active_by_ids([entry.id])] == [entry.id]

    await repo.archive(entry.id)
    assert await repo.list_active_by_ids([entry.id]) == []


async def test_memory_entry_repo_list_evidence_for_entries_batches_results(session):
    repo = MemoryEntryRepo(session)
    first = await repo.create(
        conversation_id=None,
        source_channel_kind="dashboard",
        source_channel_ref="manual",
        summary="First memory",
        topics=[],
        entities=[],
        evidence=[
            {
                "kind": "message",
                "label": "First note",
                "content": "First content.",
            },
            {
                "kind": "summary",
                "label": "First summary",
                "content": "First summary content.",
            },
        ],
    )
    second = await repo.create(
        conversation_id=None,
        source_channel_kind="dashboard",
        source_channel_ref="manual",
        summary="Second memory",
        topics=[],
        entities=[],
        evidence=[
            {
                "kind": "message",
                "label": "Second note",
                "content": "Second content.",
            }
        ],
    )

    evidence_by_entry = await repo.list_evidence_for_entries([second.id, first.id])

    assert list(evidence_by_entry) == [second.id, first.id]
    assert [item.label for item in evidence_by_entry[first.id]] == [
        "First note",
        "First summary",
    ]
    assert [item.label for item in evidence_by_entry[second.id]] == ["Second note"]


async def test_memory_entry_repo_create_returns_entry_with_loaded_evidence(session):
    repo = MemoryEntryRepo(session)

    entry = await repo.create(
        conversation_id=None,
        source_channel_kind="dashboard",
        source_channel_ref="manual",
        summary="User prefers concrete verification.",
        topics=[],
        entities=[],
        evidence=[
            {
                "kind": "message",
                "label": "User instruction",
                "content": "Use live checks.",
            }
        ],
    )

    assert len(entry.evidence) == 1
    assert entry.evidence[0].kind == "message"
    assert entry.evidence[0].label == "User instruction"
    assert entry.evidence[0].content == "Use live checks."


async def test_memory_entry_repo_list_active_by_ids_preserves_input_order(session):
    repo = MemoryEntryRepo(session)
    first = await repo.create(
        conversation_id=None,
        source_channel_kind="dashboard",
        source_channel_ref="manual",
        summary="First memory",
        topics=[],
        entities=[],
        evidence=[],
    )
    second = await repo.create(
        conversation_id=None,
        source_channel_kind="dashboard",
        source_channel_ref="manual",
        summary="Second memory",
        topics=[],
        entities=[],
        evidence=[],
    )

    rows = await repo.list_active_by_ids([second.id, first.id])

    assert [row.id for row in rows] == [second.id, first.id]


async def test_memory_recall_repo_record_many_and_list_for_conversation(session):
    entry_repo = MemoryEntryRepo(session)
    recall_repo = MemoryRecallRepo(session)
    conversation_id = uuid4()
    trigger_id = uuid4()
    session.add(
        ConversationRow(
            id=conversation_id,
            channel_kind="discord",
            channel_ref="123",
            started_at=datetime.now(UTC),
            last_activity_at=datetime.now(UTC),
            status="open",
        )
    )
    session.add(
        TriggerRow(
            id=trigger_id,
            kind="discord",
            source_ref="123",
            created_at=datetime.now(UTC),
        )
    )
    await session.commit()
    entry = await entry_repo.create(
        conversation_id=conversation_id,
        source_channel_kind="discord",
        source_channel_ref="123",
        summary="User prefers TDD.",
        topics=["testing"],
        entities=["Jarvis"],
        evidence=[],
    )

    await recall_repo.record_many(
        conversation_id=conversation_id,
        trigger_id=trigger_id,
        recalled=[
            {
                "memory_entry_id": entry.id,
                "score": 0.82,
                "rank": 1,
            }
        ],
    )

    events = await recall_repo.list_for_conversation(conversation_id)
    assert len(events) == 1
    assert events[0].memory_entry_id == entry.id
    assert events[0].score == 0.82
    assert events[0].rank == 1


async def test_memory_recall_repo_lists_newest_batch_first_then_rank(session):
    entry_repo = MemoryEntryRepo(session)
    recall_repo = MemoryRecallRepo(session)
    conversation_id = uuid4()
    trigger_id = uuid4()
    session.add(
        ConversationRow(
            id=conversation_id,
            channel_kind="discord",
            channel_ref="123",
            started_at=datetime.now(UTC),
            last_activity_at=datetime.now(UTC),
            status="open",
        )
    )
    session.add(
        TriggerRow(
            id=trigger_id,
            kind="discord",
            source_ref="123",
            created_at=datetime.now(UTC),
        )
    )
    await session.commit()
    first = await entry_repo.create(
        conversation_id=conversation_id,
        source_channel_kind="discord",
        source_channel_ref="123",
        summary="First memory",
        topics=[],
        entities=[],
        evidence=[],
    )
    second = await entry_repo.create(
        conversation_id=conversation_id,
        source_channel_kind="discord",
        source_channel_ref="123",
        summary="Second memory",
        topics=[],
        entities=[],
        evidence=[],
    )
    third = await entry_repo.create(
        conversation_id=conversation_id,
        source_channel_kind="discord",
        source_channel_ref="123",
        summary="Third memory",
        topics=[],
        entities=[],
        evidence=[],
    )

    await recall_repo.record_many(
        conversation_id=conversation_id,
        trigger_id=trigger_id,
        recalled=[
            {"memory_entry_id": first.id, "score": 0.9, "rank": 1},
        ],
    )
    await recall_repo.record_many(
        conversation_id=conversation_id,
        trigger_id=trigger_id,
        recalled=[
            {"memory_entry_id": second.id, "score": 0.8, "rank": 2},
            {"memory_entry_id": third.id, "score": 0.95, "rank": 1},
        ],
    )

    events = await recall_repo.list_for_conversation(conversation_id)

    assert [event.memory_entry_id for event in events] == [third.id, second.id, first.id]
