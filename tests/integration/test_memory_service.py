from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from jarvis.memory.service import MemoryService
from jarvis.memory.types import VectorSearchResult
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.models import ConversationRow, TriggerRow
from jarvis.persistence.repositories import MemoryEntryRepo, MemoryPreferenceRepo, MemoryRecallRepo


class FakeEmbeddingProvider:
    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class BrokenEmbeddingProvider:
    async def embed(self, text: str) -> list[float]:
        raise RuntimeError("embedding failed")


class FakeVectorStore:
    def __init__(
        self,
        memory_entry_id: UUID,
        *,
        available: bool = True,
        last_error: str | None = None,
        score: float = 0.91,
        result_ids: list[UUID] | None = None,
    ) -> None:
        self.available = available
        self.last_error = last_error
        self._score = score
        self._result_ids = result_ids or [memory_entry_id]

    async def search(self, embedding: list[float], *, limit: int) -> list[VectorSearchResult]:
        return [
            VectorSearchResult(
                entry_id=memory_entry_id,
                distance=0.1,
                score=self._score,
            )
            for memory_entry_id in self._result_ids[:limit]
        ]


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    yield factory
    await engine.dispose()


async def _seed_memory(factory, *, preference_content: str | None = "Prefer concise answers."):
    async with factory() as session:
        conv = ConversationRow(
            channel_kind="discord",
            channel_ref="123",
            started_at=datetime.now(UTC),
            last_activity_at=datetime.now(UTC),
            status="open",
        )
        trigger = TriggerRow(
            kind="discord",
            source_ref="123",
            created_at=datetime.now(UTC),
        )
        session.add_all([conv, trigger])
        await session.commit()
        await session.refresh(conv)
        await session.refresh(trigger)

        if preference_content is not None:
            preference_repo = MemoryPreferenceRepo(session)
            preference = await preference_repo.create_pending(
                content=preference_content,
                source="user",
            )
            await preference_repo.approve(preference.id)

        entry = await MemoryEntryRepo(session).create(
            conversation_id=conv.id,
            source_channel_kind="discord",
            source_channel_ref="123",
            summary="We discussed PR #18 Action Inbox deploy validation.",
            topics=["jarvis"],
            entities=["PR #18"],
            evidence=[
                {
                    "kind": "pull_request",
                    "label": "PR #18",
                    "content": "PR #18",
                }
            ],
        )

    return conv, trigger, entry


async def test_memory_service_build_context_recalls_preferences_and_memory(factory):
    conv, trigger, entry = await _seed_memory(factory)
    service = MemoryService(
        session_factory=factory,
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=FakeVectorStore(entry.id),
        max_recalled_memories=3,
        min_relevance_score=0.8,
    )

    ctx = await service.build_context(
        conversation_id=conv.id,
        trigger_id=trigger.id,
        prompt="What did we ship?",
    )

    assert ctx.preferences == ["Prefer concise answers."]
    assert len(ctx.recalled) == 1
    assert ctx.recalled[0].memory_entry_id == entry.id
    assert ctx.recalled[0].summary == "We discussed PR #18 Action Inbox deploy validation."
    assert ctx.recalled[0].topics == ["jarvis"]
    assert ctx.recalled[0].entities == ["PR #18"]
    assert ctx.recalled[0].evidence == [
        {
            "kind": "pull_request",
            "label": "PR #18",
            "content": "PR #18",
        }
    ]
    assert ctx.recall_available is True
    assert ctx.error is None

    async with factory() as session:
        events = await MemoryRecallRepo(session).list_for_conversation(conv.id)

    assert len(events) == 1
    assert events[0].memory_entry_id == entry.id
    assert events[0].score == 0.91
    assert events[0].rank == 1


async def test_memory_service_build_context_returns_empty_on_embedding_error(factory):
    conv, _trigger, entry = await _seed_memory(factory, preference_content=None)
    service = MemoryService(
        session_factory=factory,
        embedding_provider=BrokenEmbeddingProvider(),
        vector_store=FakeVectorStore(entry.id),
        max_recalled_memories=3,
        min_relevance_score=0.8,
    )

    ctx = await service.build_context(
        conversation_id=conv.id,
        trigger_id=None,
        prompt="What did we ship?",
    )

    assert ctx.preferences == []
    assert ctx.recalled == []
    assert ctx.recall_available is False
    assert ctx.error is not None
    assert "embedding failed" in ctx.error


async def test_memory_service_vector_unavailable_returns_preferences_and_error(factory):
    conv, trigger, entry = await _seed_memory(factory)
    service = MemoryService(
        session_factory=factory,
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=FakeVectorStore(
            entry.id,
            available=False,
            last_error="vector store unavailable",
        ),
        max_recalled_memories=3,
        min_relevance_score=0.8,
    )

    ctx = await service.build_context(
        conversation_id=conv.id,
        trigger_id=trigger.id,
        prompt="What did we ship?",
    )

    assert ctx.preferences == ["Prefer concise answers."]
    assert ctx.recalled == []
    assert ctx.recall_available is False
    assert ctx.error == "vector store unavailable"


async def test_memory_service_recall_disabled_reports_unavailable(factory):
    conv, trigger, entry = await _seed_memory(factory)
    service = MemoryService(
        session_factory=factory,
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=FakeVectorStore(entry.id),
        max_recalled_memories=0,
        min_relevance_score=0.8,
    )

    ctx = await service.build_context(
        conversation_id=conv.id,
        trigger_id=trigger.id,
        prompt="What did we ship?",
    )

    assert ctx.preferences == ["Prefer concise answers."]
    assert ctx.recalled == []
    assert ctx.recall_available is False
    assert ctx.error is None


async def test_memory_service_excludes_below_threshold_results_without_recall_event(factory):
    conv, trigger, entry = await _seed_memory(factory)
    service = MemoryService(
        session_factory=factory,
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=FakeVectorStore(entry.id, score=0.7),
        max_recalled_memories=3,
        min_relevance_score=0.8,
    )

    ctx = await service.build_context(
        conversation_id=conv.id,
        trigger_id=trigger.id,
        prompt="What did we ship?",
    )

    assert ctx.preferences == ["Prefer concise answers."]
    assert ctx.recalled == []
    assert ctx.recall_available is True
    assert ctx.error is None
    async with factory() as session:
        events = await MemoryRecallRepo(session).list_for_conversation(conv.id)
    assert events == []


async def test_memory_service_skips_missing_and_archived_vector_results(factory):
    conv, trigger, entry = await _seed_memory(factory)
    async with factory() as session:
        await MemoryEntryRepo(session).archive(entry.id)
    service = MemoryService(
        session_factory=factory,
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=FakeVectorStore(entry.id, result_ids=[entry.id, uuid4()]),
        max_recalled_memories=3,
        min_relevance_score=0.8,
    )

    ctx = await service.build_context(
        conversation_id=conv.id,
        trigger_id=trigger.id,
        prompt="What did we ship?",
    )

    assert ctx.preferences == ["Prefer concise answers."]
    assert ctx.recalled == []
    assert ctx.recall_available is True
    assert ctx.error is None
    async with factory() as session:
        events = await MemoryRecallRepo(session).list_for_conversation(conv.id)
    assert events == []


async def test_memory_service_hydration_failure_degrades_without_raising(factory, monkeypatch):
    conv, trigger, entry = await _seed_memory(factory)

    async def fail_list_active_by_ids(self, ids):
        raise RuntimeError("hydration failed")

    monkeypatch.setattr(MemoryEntryRepo, "list_active_by_ids", fail_list_active_by_ids)
    service = MemoryService(
        session_factory=factory,
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=FakeVectorStore(entry.id),
        max_recalled_memories=3,
        min_relevance_score=0.8,
    )

    ctx = await service.build_context(
        conversation_id=conv.id,
        trigger_id=trigger.id,
        prompt="What did we ship?",
    )

    assert ctx.preferences == ["Prefer concise answers."]
    assert ctx.recalled == []
    assert ctx.recall_available is False
    assert ctx.error is not None
    assert "hydration failed" in ctx.error


async def test_memory_service_recall_write_failure_preserves_recalled_context(
    factory, monkeypatch
):
    conv, trigger, entry = await _seed_memory(factory)

    async def fail_record_many(self, *, conversation_id, trigger_id, recalled):
        raise RuntimeError("recall write failed")

    monkeypatch.setattr(MemoryRecallRepo, "record_many", fail_record_many)
    service = MemoryService(
        session_factory=factory,
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=FakeVectorStore(entry.id),
        max_recalled_memories=3,
        min_relevance_score=0.8,
    )

    ctx = await service.build_context(
        conversation_id=conv.id,
        trigger_id=trigger.id,
        prompt="What did we ship?",
    )

    assert ctx.preferences == ["Prefer concise answers."]
    assert len(ctx.recalled) == 1
    assert ctx.recalled[0].memory_entry_id == entry.id
    assert ctx.recall_available is True
    assert ctx.error is not None
    assert "recall write failed" in ctx.error


async def test_memory_service_mark_recalled_failure_preserves_recalled_context(
    factory, monkeypatch
):
    conv, trigger, entry = await _seed_memory(factory)

    async def fail_mark_recalled(self, ids):
        raise RuntimeError("mark recalled failed")

    monkeypatch.setattr(MemoryEntryRepo, "mark_recalled", fail_mark_recalled)
    service = MemoryService(
        session_factory=factory,
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=FakeVectorStore(entry.id),
        max_recalled_memories=3,
        min_relevance_score=0.8,
    )

    ctx = await service.build_context(
        conversation_id=conv.id,
        trigger_id=trigger.id,
        prompt="What did we ship?",
    )

    assert ctx.preferences == ["Prefer concise answers."]
    assert len(ctx.recalled) == 1
    assert ctx.recalled[0].memory_entry_id == entry.id
    assert ctx.recall_available is True
    assert ctx.error is not None
    assert "mark recalled failed" in ctx.error


async def test_memory_service_combines_preference_and_embedding_errors(factory, monkeypatch):
    _conv, _trigger, entry = await _seed_memory(factory, preference_content=None)

    async def fail_list_active(self):
        raise RuntimeError("preference failed")

    monkeypatch.setattr(MemoryPreferenceRepo, "list_active", fail_list_active)
    service = MemoryService(
        session_factory=factory,
        embedding_provider=BrokenEmbeddingProvider(),
        vector_store=FakeVectorStore(entry.id),
        max_recalled_memories=3,
        min_relevance_score=0.8,
    )

    ctx = await service.build_context(
        conversation_id=None,
        trigger_id=None,
        prompt="What did we ship?",
    )

    assert ctx.preferences == []
    assert ctx.recalled == []
    assert ctx.recall_available is False
    assert ctx.error == "preference failed; embedding failed"


class FakeSummarizer:
    def __init__(self, preference_candidates):
        self._candidates = preference_candidates

    async def summarize(self, *, user_prompt, assistant_output):
        from jarvis.memory.types import MemorySummary

        return MemorySummary(
            summary="did a thing",
            topics=[],
            entities=[],
            evidence=[],
            preference_candidates=list(self._candidates),
        )


class SequencedEmbeddingProvider:
    """Returns a preset vector per exact text, else a default."""

    def __init__(self, mapping, default=None):
        self._mapping = mapping
        self._default = default or [0.0, 0.0]

    async def embed(self, text: str) -> list[float]:
        return list(self._mapping.get(text, self._default))


async def test_summarize_run_drops_semantic_duplicate_of_active(factory):
    from jarvis.memory.preference_dedup import PreferenceDeduplicator
    from jarvis.persistence.repositories import MemoryPreferenceRepo

    async with factory() as session:
        repo = MemoryPreferenceRepo(session)
        pref = await repo.create_pending(
            content="Always run the tests before committing",
            source="agent_proposal",
            embedding=[1.0, 0.0],
            embedding_dimensions=2,
        )
        await repo.approve(pref.id)

    class _NoJudge:
        async def judge(self, *, candidate, existing):
            return False

    embeddings = SequencedEmbeddingProvider(
        {"Run the test suite before each commit": [1.0, 0.0]}
    )
    dedup = PreferenceDeduplicator(
        embedding_provider=embeddings,
        judge=_NoJudge(),
        high_threshold=0.92,
        low_threshold=0.82,
        max_judge_calls=5,
    )
    service = MemoryService(
        session_factory=factory,
        embedding_provider=embeddings,
        vector_store=FakeVectorStore(uuid4(), available=False),
        max_recalled_memories=0,
        min_relevance_score=0.25,
        summarizer=FakeSummarizer(["Run the test suite before each commit"]),
        preference_deduplicator=dedup,
    )

    outcome = await service.summarize_run(
        conversation_id=None,
        channel_kind="discord",
        channel_ref="c1",
        user_prompt="u",
        assistant_output="a",
    )
    assert outcome.preferences_created == 0


async def test_summarize_run_keeps_distinct_preference(factory):
    from jarvis.memory.preference_dedup import PreferenceDeduplicator
    from jarvis.persistence.repositories import MemoryPreferenceRepo

    async with factory() as session:
        repo = MemoryPreferenceRepo(session)
        pref = await repo.create_pending(
            content="Always run the tests before committing",
            source="agent_proposal",
            embedding=[1.0, 0.0],
            embedding_dimensions=2,
        )
        await repo.approve(pref.id)

    class _NoJudge:
        async def judge(self, *, candidate, existing):
            return False

    embeddings = SequencedEmbeddingProvider({"Use dark mode in all dashboards": [0.0, 1.0]})
    dedup = PreferenceDeduplicator(
        embedding_provider=embeddings,
        judge=_NoJudge(),
        high_threshold=0.92,
        low_threshold=0.82,
        max_judge_calls=5,
    )
    service = MemoryService(
        session_factory=factory,
        embedding_provider=embeddings,
        vector_store=FakeVectorStore(uuid4(), available=False),
        max_recalled_memories=0,
        min_relevance_score=0.25,
        summarizer=FakeSummarizer(["Use dark mode in all dashboards"]),
        preference_deduplicator=dedup,
    )

    outcome = await service.summarize_run(
        conversation_id=None, channel_kind="discord", channel_ref="c1",
        user_prompt="u", assistant_output="a",
    )
    assert outcome.preferences_created == 1


async def test_summarize_run_falls_back_when_dedup_embed_fails(factory):
    from jarvis.memory.preference_dedup import PreferenceDeduplicator

    class _BrokenEmbeddings:
        async def embed(self, text: str):
            raise RuntimeError("embed down")

    class _NoJudge:
        async def judge(self, *, candidate, existing):
            return False

    dedup = PreferenceDeduplicator(
        embedding_provider=_BrokenEmbeddings(),
        judge=_NoJudge(),
        high_threshold=0.92,
        low_threshold=0.82,
        max_judge_calls=5,
    )
    service = MemoryService(
        session_factory=factory,
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=FakeVectorStore(uuid4(), available=False),
        max_recalled_memories=0,
        min_relevance_score=0.25,
        summarizer=FakeSummarizer(["Brand new rule about formatting"]),
        preference_deduplicator=dedup,
    )

    outcome = await service.summarize_run(
        conversation_id=None, channel_kind="discord", channel_ref="c1",
        user_prompt="u", assistant_output="a",
    )
    assert outcome.preferences_created == 1


async def test_summarize_run_dedupes_within_batch(factory):
    from jarvis.memory.preference_dedup import PreferenceDeduplicator

    class _NoJudge:
        async def judge(self, *, candidate, existing):
            return False

    # Two distinct candidate strings that embed to the SAME vector -> the second
    # is a semantic duplicate of the first accepted one, with no pre-existing rows.
    embeddings = SequencedEmbeddingProvider(
        {
            "Always run tests before committing": [1.0, 0.0],
            "Run the test suite before every commit": [1.0, 0.0],
        }
    )
    dedup = PreferenceDeduplicator(
        embedding_provider=embeddings,
        judge=_NoJudge(),
        high_threshold=0.92,
        low_threshold=0.82,
        max_judge_calls=5,
    )
    service = MemoryService(
        session_factory=factory,
        embedding_provider=embeddings,
        vector_store=FakeVectorStore(uuid4(), available=False),
        max_recalled_memories=0,
        min_relevance_score=0.25,
        summarizer=FakeSummarizer(
            [
                "Always run tests before committing",
                "Run the test suite before every commit",
            ]
        ),
        preference_deduplicator=dedup,
    )

    outcome = await service.summarize_run(
        conversation_id=None, channel_kind="discord", channel_ref="c1",
        user_prompt="u", assistant_output="a",
    )
    assert outcome.preferences_created == 1


async def test_sensitivity_terms_come_from_active_preferences(factory):
    async with factory() as session:
        repo = MemoryPreferenceRepo(session)
        marked = await repo.create_pending(
            content="sensitive: mom@example.com, Salary", source="user"
        )
        await repo.approve(marked.id)
        unmarked = await repo.create_pending(content="Prefer concise answers.", source="user")
        await repo.approve(unmarked.id)
        # A pending (never approved) marked preference must not contribute.
        await repo.create_pending(content="sensitive: pending@example.com", source="user")

    service = MemoryService(
        session_factory=factory,
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=FakeVectorStore(uuid4()),
        max_recalled_memories=3,
        min_relevance_score=0.8,
    )

    assert await service.sensitivity_terms() == ["mom@example.com", "salary"]
