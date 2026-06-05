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


class _FakeVectorStore:
    available = True
    last_error = None

    def __init__(self) -> None:
        self.upserts = []

    async def upsert(self, memory_entry_id, embedding):
        self.upserts.append((memory_entry_id, embedding))

    async def search(self, embedding, *, limit):
        return []


class _FakeSummarizer:
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


async def test_memory_service_summarize_run_creates_entry_vector_and_preference(factory):
    vector_store = _FakeVectorStore()
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

    service = MemoryService(
        session_factory=factory,
        embedding_provider=_FakeEmbeddingProvider(),
        vector_store=vector_store,
        summarizer=_FakeSummarizer(),
        max_recalled_memories=5,
        min_relevance_score=0.25,
    )

    await service.summarize_run(
        conversation_id=conversation_id,
        channel_kind=ChannelKind.DASHBOARD.value,
        channel_ref="mark",
        user_prompt="let's add memory",
        assistant_output="done",
    )

    async with factory() as session:
        entries = await MemoryEntryRepo(session).list_recent()
        preferences = await MemoryPreferenceRepo(session).list_for_dashboard()
        evidence = await MemoryEntryRepo(session).list_evidence(entries[0].id)

    assert len(entries) == 1
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
