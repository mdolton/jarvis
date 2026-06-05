from datetime import UTC, datetime
from uuid import uuid4

import pytest

from jarvis.core.types import ChannelKind
from jarvis.memory.service import MemoryService
from jarvis.memory.types import MemorySummary
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.models import ConversationRow
from jarvis.persistence.repositories import MemoryEntryRepo, MemoryPreferenceRepo


class _FakeEmbeddingProvider:
    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _FailingEmbeddingProvider:
    async def embed(self, text: str) -> list[float]:
        raise RuntimeError("embedding failed")


class _FakeVectorStore:
    last_error = None

    def __init__(self, *, available: bool = True, fail_upsert: bool = False) -> None:
        self.available = available
        self.fail_upsert = fail_upsert
        self.upserts = []

    async def upsert(self, memory_entry_id, embedding):
        if self.fail_upsert:
            raise RuntimeError("vector failed")
        self.upserts.append((memory_entry_id, embedding))

    async def search(self, embedding, *, limit):
        return []


class _FakeSummarizer:
    def __init__(self, *, preference_candidates=None) -> None:
        self.preference_candidates = preference_candidates or ["Prefer concise answers."]

    async def summarize(self, *, user_prompt: str, assistant_output: str) -> MemorySummary:
        return MemorySummary(
            summary="We discussed Jarvis memory.",
            topics=["jarvis", "memory"],
            entities=["sqlite-vec"],
            evidence=[
                {
                    "kind": "identifier",
                    "label": "library",
                    "content": "sqlite-vec",
                }
            ],
            preference_candidates=self.preference_candidates,
        )


class _EmptySummarizer:
    async def summarize(self, *, user_prompt: str, assistant_output: str) -> MemorySummary:
        return MemorySummary(
            summary="",
            topics=[],
            entities=[],
            evidence=[],
            preference_candidates=[],
        )


class _FailingSummarizer:
    async def summarize(self, *, user_prompt: str, assistant_output: str) -> MemorySummary:
        raise RuntimeError("summary failed")


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    yield factory
    await engine.dispose()


async def _create_conversation(factory):
    conversation_id = uuid4()
    async with factory() as session:
        session.add(
            ConversationRow(
                id=conversation_id,
                channel_kind=ChannelKind.DASHBOARD.value,
                channel_ref="mark",
                started_at=datetime.now(UTC),
                last_activity_at=datetime.now(UTC),
                status="open",
            )
        )
        await session.commit()
    return conversation_id


async def _summarize(service, conversation_id):
    return await service.summarize_run(
        conversation_id=conversation_id,
        channel_kind=ChannelKind.DASHBOARD.value,
        channel_ref="mark",
        user_prompt="let's add memory",
        assistant_output="done",
    )


async def _list_entries_preferences_and_evidence(factory):
    async with factory() as session:
        entries = await MemoryEntryRepo(session).list_recent()
        preferences = await MemoryPreferenceRepo(session).list_for_dashboard()
        evidence = []
        if entries:
            evidence = await MemoryEntryRepo(session).list_evidence(entries[0].id)
    return entries, preferences, evidence


async def test_memory_service_summarize_run_creates_entry_vector_and_preference(factory):
    vector_store = _FakeVectorStore()
    conversation_id = await _create_conversation(factory)

    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FakeEmbeddingProvider(),
        vector_store=vector_store,
        summarizer=_FakeSummarizer(),
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    outcome = await _summarize(service, conversation_id)

    entries, preferences, evidence = await _list_entries_preferences_and_evidence(factory)

    assert outcome.status == "created"
    assert outcome.memory_entry_id == entries[0].id
    assert outcome.error is None
    assert outcome.preferences_created == 1
    assert len(entries) == 1
    assert entries[0].status == "active"
    assert entries[0].conversation_id == conversation_id
    assert entries[0].source_channel_kind == ChannelKind.DASHBOARD.value
    assert entries[0].source_channel_ref == "mark"
    assert entries[0].summary == "We discussed Jarvis memory."
    assert entries[0].topics == ["jarvis", "memory"]
    assert entries[0].entities == ["sqlite-vec"]
    assert [
        {
            "kind": item.kind,
            "label": item.label,
            "content": item.content,
        }
        for item in evidence
    ] == [
        {
            "kind": "identifier",
            "label": "library",
            "content": "sqlite-vec",
        }
    ]

    assert vector_store.upserts == [(entries[0].id, [0.1, 0.2, 0.3])]
    assert len(preferences) == 1
    assert preferences[0].content == "Prefer concise answers."
    assert preferences[0].source == "agent_proposal"
    assert preferences[0].status == "pending"


async def test_memory_service_summarize_run_skips_when_no_summarizer(factory):
    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FakeEmbeddingProvider(),
        vector_store=_FakeVectorStore(),
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    outcome = await _summarize(service, None)

    assert outcome.status == "skipped"
    assert outcome.memory_entry_id is None
    assert outcome.error == "summarizer unavailable"


async def test_memory_service_summarize_run_catches_summarizer_failure(factory):
    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FakeEmbeddingProvider(),
        vector_store=_FakeVectorStore(),
        summarizer=_FailingSummarizer(),
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    outcome = await _summarize(service, None)

    async with factory() as session:
        entries = await MemoryEntryRepo(session).list_recent()
        preferences = await MemoryPreferenceRepo(session).list_for_dashboard()

    assert outcome.status == "failed"
    assert "summary failed" in outcome.error
    assert outcome.memory_entry_id is None
    assert entries == []
    assert preferences == []


async def test_memory_service_summarize_run_skips_empty_summary(factory):
    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FakeEmbeddingProvider(),
        vector_store=_FakeVectorStore(),
        summarizer=_EmptySummarizer(),
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    outcome = await _summarize(service, None)

    async with factory() as session:
        entries = await MemoryEntryRepo(session).list_recent()

    assert outcome.status == "skipped"
    assert outcome.memory_entry_id is None
    assert outcome.error == "empty summary"
    assert entries == []


async def test_memory_service_summarize_run_marks_entry_unindexed_on_embedding_failure(
    factory,
):
    conversation_id = await _create_conversation(factory)
    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FailingEmbeddingProvider(),
        vector_store=_FakeVectorStore(),
        summarizer=_FakeSummarizer(),
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    outcome = await _summarize(service, conversation_id)

    async with factory() as session:
        entries = await MemoryEntryRepo(session).list_recent()
        preferences = await MemoryPreferenceRepo(session).list_for_dashboard()

    assert outcome.status == "unindexed"
    assert "embedding failed" in outcome.error
    assert outcome.memory_entry_id == entries[0].id
    assert entries[0].status == "unindexed"
    assert len(preferences) == 1


async def test_memory_service_summarize_run_marks_entry_unindexed_when_vector_unavailable(
    factory,
):
    conversation_id = await _create_conversation(factory)
    vector_store = _FakeVectorStore(available=False)
    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FakeEmbeddingProvider(),
        vector_store=vector_store,
        summarizer=_FakeSummarizer(),
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    outcome = await _summarize(service, conversation_id)

    async with factory() as session:
        entries = await MemoryEntryRepo(session).list_recent()

    assert outcome.status == "unindexed"
    assert outcome.error == "vector store unavailable"
    assert outcome.memory_entry_id == entries[0].id
    assert entries[0].status == "unindexed"
    assert vector_store.upserts == []


async def test_memory_service_summarize_run_marks_entry_unindexed_on_vector_failure(
    factory,
):
    conversation_id = await _create_conversation(factory)
    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FakeEmbeddingProvider(),
        vector_store=_FakeVectorStore(fail_upsert=True),
        summarizer=_FakeSummarizer(),
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    outcome = await _summarize(service, conversation_id)

    async with factory() as session:
        entries = await MemoryEntryRepo(session).list_recent()

    assert outcome.status == "unindexed"
    assert "vector failed" in outcome.error
    assert outcome.memory_entry_id == entries[0].id
    assert entries[0].status == "unindexed"


async def test_memory_service_summarize_run_dedupes_and_caps_pending_preferences(factory):
    candidates = [
        " Prefer concise answers. ",
        "prefer concise answers.",
        "Use bullets.",
        "Use bullets. ",
        "A",
        "B",
        "C",
        "D",
        "E",
    ]
    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FakeEmbeddingProvider(),
        vector_store=_FakeVectorStore(),
        summarizer=_FakeSummarizer(preference_candidates=candidates),
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    first = await _summarize(service, None)
    second = await _summarize(service, None)

    async with factory() as session:
        preferences = await MemoryPreferenceRepo(session).list_for_dashboard()

    assert first.preferences_created == 5
    assert second.preferences_created == 0
    assert sorted(preference.content for preference in preferences) == [
        "A",
        "B",
        "C",
        "Prefer concise answers.",
        "Use bullets.",
    ]


async def test_memory_service_summarize_run_does_not_repropose_rejected_preference(factory):
    async with factory() as session:
        repo = MemoryPreferenceRepo(session)
        preference = await repo.create_pending(
            content="Prefer concise answers.",
            source="agent_proposal",
        )
        await repo.reject(preference.id)
    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FakeEmbeddingProvider(),
        vector_store=_FakeVectorStore(),
        summarizer=_FakeSummarizer(),
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    outcome = await _summarize(service, None)

    async with factory() as session:
        preferences = await MemoryPreferenceRepo(session).list_for_dashboard()

    assert outcome.preferences_created == 0
    assert len(preferences) == 1
    assert preferences[0].status == "rejected"


async def test_memory_service_summarize_run_does_not_repropose_active_user_preference(factory):
    async with factory() as session:
        repo = MemoryPreferenceRepo(session)
        preference = await repo.create_pending(
            content="Prefer concise answers.",
            source="user",
        )
        await repo.approve(preference.id)
    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FakeEmbeddingProvider(),
        vector_store=_FakeVectorStore(),
        summarizer=_FakeSummarizer(),
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    outcome = await _summarize(service, None)

    async with factory() as session:
        preferences = await MemoryPreferenceRepo(session).list_for_dashboard()

    assert outcome.preferences_created == 0
    assert len(preferences) == 1
    assert preferences[0].source == "user"
    assert preferences[0].status == "active"


async def test_memory_service_summarize_run_retry_reuses_entry_vector_and_preferences(factory):
    vector_store = _FakeVectorStore()
    conversation_id = await _create_conversation(factory)
    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FakeEmbeddingProvider(),
        vector_store=vector_store,
        summarizer=_FakeSummarizer(),
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    first = await _summarize(service, conversation_id)
    second = await _summarize(service, conversation_id)

    entries, preferences, _evidence = await _list_entries_preferences_and_evidence(factory)

    assert first.status == "created"
    assert second.status == "created"
    assert second.memory_entry_id == first.memory_entry_id
    assert second.preferences_created == 0
    assert len(entries) == 1
    assert len(preferences) == 1
    assert vector_store.upserts == [(entries[0].id, [0.1, 0.2, 0.3])]


async def test_memory_service_summarize_run_rolls_back_preference_batch_on_failure(
    factory,
    monkeypatch,
):
    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FakeEmbeddingProvider(),
        vector_store=_FakeVectorStore(),
        summarizer=_FakeSummarizer(preference_candidates=["A", "B"]),
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    original_add_all = list

    def fail_add_all(self, instances):
        original_add_all(instances)
        raise RuntimeError("preference batch failed")

    monkeypatch.setattr("sqlalchemy.ext.asyncio.AsyncSession.add_all", fail_add_all)

    outcome = await _summarize(service, None)

    async with factory() as session:
        preferences = await MemoryPreferenceRepo(session).list_for_dashboard()

    assert outcome.status == "failed"
    assert "preference batch failed" in outcome.error
    assert preferences == []
