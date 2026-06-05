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
                memory_entry_id=memory_entry_id,
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
