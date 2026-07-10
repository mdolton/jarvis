from uuid import uuid4

import pytest

from jarvis.core.types import AuditEventType, ChannelKind, TriggerKind
from jarvis.memory.service import MemoryService
from jarvis.memory.types import MemorySummary, VectorSearchResult
from jarvis.persistence.db import Base, create_engine, session_factory
from jarvis.persistence.repositories import ConversationRepo, MemoryEntryRepo, TriggerRepo


class _RecordingAudit:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


class _FakeEmbeddingProvider:
    async def embed(self, text: str):
        return [0.1, 0.2, 0.3]


class _BrokenEmbeddingProvider:
    async def embed(self, text: str):
        raise RuntimeError("embedding failed")


class _FakeVectorStore:
    available = True
    last_error = None

    def __init__(self, result_id=None):
        self.result_id = result_id
        self.upserts = []

    async def search(self, embedding, *, limit):
        if self.result_id is None:
            return []
        return [
            VectorSearchResult(
                entry_id=self.result_id,
                distance=0.1,
                score=0.91,
            )
        ]

    async def upsert(self, memory_entry_id, embedding):
        self.upserts.append((memory_entry_id, embedding))


class _FakeSummarizer:
    async def summarize(self, *, user_prompt: str, assistant_output: str):
        return MemorySummary(
            summary="We discussed Jarvis memory.",
            topics=["jarvis"],
            entities=[],
            evidence=[],
            preference_candidates=["Prefer concise answers."],
        )


@pytest.fixture
async def factory(tmp_path):
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = session_factory(engine)
    yield factory
    await engine.dispose()


async def test_memory_recall_emits_audit_event(factory):
    async with factory() as session:
        conv = await ConversationRepo(session).find_or_create_open(
            channel_kind=ChannelKind.DASHBOARD,
            channel_ref="mark",
            idle_timeout_sec=900,
        )
        trigger = await TriggerRepo(session).record(
            kind=TriggerKind.MANUAL.value,
            source_ref="mark",
        )
        entry = await MemoryEntryRepo(session).create(
            conversation_id=conv.id,
            source_channel_kind=ChannelKind.DASHBOARD.value,
            source_channel_ref="mark",
            summary="We discussed Jarvis memory.",
            topics=["jarvis"],
            entities=[],
            evidence=[],
        )

    audit = _RecordingAudit()
    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FakeEmbeddingProvider(),
        vector_store=_FakeVectorStore(entry.id),
        audit=audit,
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    await service.build_context(
        conversation_id=conv.id,
        trigger_id=trigger.id,
        prompt="memory?",
    )

    assert [event.type for event in audit.events] == [AuditEventType.MEMORY_RECALLED]
    assert audit.events[0].conversation_id == conv.id
    assert audit.events[0].trigger_id == trigger.id
    assert audit.events[0].payload["count"] == 1
    assert audit.events[0].payload["memory_entry_ids"] == [str(entry.id)]


async def test_memory_failure_emits_audit_event(factory):
    audit = _RecordingAudit()
    trigger_id = uuid4()
    service = MemoryService(
        session_factory=factory,
        embedding_provider=_BrokenEmbeddingProvider(),
        vector_store=_FakeVectorStore(),
        audit=audit,
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    await service.build_context(
        conversation_id=None,
        trigger_id=trigger_id,
        prompt="memory?",
    )

    assert [event.type for event in audit.events] == [AuditEventType.MEMORY_FAILED]
    assert audit.events[0].trigger_id == trigger_id
    assert audit.events[0].payload["stage"] == "recall"
    assert "embedding failed" in audit.events[0].payload["error"]


async def test_memory_summary_emits_creation_and_preference_events(factory):
    audit = _RecordingAudit()
    async with factory() as session:
        conv = await ConversationRepo(session).find_or_create_open(
            channel_kind=ChannelKind.DASHBOARD,
            channel_ref="mark",
            idle_timeout_sec=900,
        )
    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FakeEmbeddingProvider(),
        vector_store=_FakeVectorStore(),
        summarizer=_FakeSummarizer(),
        audit=audit,
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    await service.summarize_run(
        conversation_id=conv.id,
        channel_kind=ChannelKind.DASHBOARD.value,
        channel_ref="mark",
        user_prompt="hello",
        assistant_output="done",
    )

    assert [event.type for event in audit.events] == [
        AuditEventType.MEMORY_ENTRY_CREATED,
        AuditEventType.MEMORY_PREFERENCE_PROPOSED,
    ]
    assert audit.events[0].conversation_id == conv.id
    assert "memory_entry_id" in audit.events[0].payload
    assert audit.events[1].conversation_id == conv.id
    assert audit.events[1].payload["content"] == "Prefer concise answers."
