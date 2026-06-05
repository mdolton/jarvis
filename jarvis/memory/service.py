from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.memory.embeddings import EmbeddingProvider
from jarvis.memory.types import MemoryContext, RecalledMemory, VectorSearchResult
from jarvis.memory.vector_store import MemoryVectorStore
from jarvis.persistence.repositories import MemoryEntryRepo, MemoryPreferenceRepo, MemoryRecallRepo


class MemoryService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_provider: EmbeddingProvider,
        vector_store: MemoryVectorStore,
        max_recalled_memories: int,
        min_relevance_score: float,
    ) -> None:
        self._session_factory = session_factory
        self._embedding_provider = embedding_provider
        self._vector_store = vector_store
        self._max_recalled_memories = max_recalled_memories
        self._min_relevance_score = min_relevance_score

    async def build_context(
        self,
        *,
        conversation_id: UUID | None,
        trigger_id: UUID | None,
        prompt: str,
    ) -> MemoryContext:
        preferences, preference_error = await self._load_preferences()

        if not self._vector_store.available or self._max_recalled_memories <= 0:
            return MemoryContext(
                preferences=preferences,
                recalled=[],
                recall_available=self._vector_store.available,
                error=_combine_errors(preference_error, self._vector_store.last_error),
            )

        try:
            embedding = await self._embedding_provider.embed(prompt)
            results = await self._vector_store.search(
                embedding,
                limit=self._max_recalled_memories,
            )
        except Exception as exc:
            return MemoryContext(
                preferences=preferences,
                recalled=[],
                recall_available=False,
                error=_combine_errors(preference_error, str(exc)),
            )

        filtered_results = [
            result for result in results if result.score >= self._min_relevance_score
        ][: self._max_recalled_memories]
        try:
            recalled = await self._load_recalled_memories(
                conversation_id=conversation_id,
                trigger_id=trigger_id,
                results=filtered_results,
            )
        except Exception as exc:
            return MemoryContext(
                preferences=preferences,
                recalled=[],
                recall_available=False,
                error=_combine_errors(preference_error, str(exc)),
            )

        return MemoryContext(
            preferences=preferences,
            recalled=recalled,
            recall_available=True,
            error=preference_error,
        )

    async def _load_preferences(self) -> tuple[list[str], str | None]:
        try:
            async with self._session_factory() as session:
                rows = await MemoryPreferenceRepo(session).list_active()
        except Exception as exc:
            return [], str(exc)
        return [row.content for row in rows], None

    async def _load_recalled_memories(
        self,
        *,
        conversation_id: UUID | None,
        trigger_id: UUID | None,
        results: list[VectorSearchResult],
    ) -> list[RecalledMemory]:
        if not results:
            return []

        result_by_id = {result.memory_entry_id: result for result in results}
        async with self._session_factory() as session:
            entry_repo = MemoryEntryRepo(session)
            entries = await entry_repo.list_active_by_ids(list(result_by_id))
            recalled = []
            for entry in entries:
                result = result_by_id[entry.id]
                evidence_rows = await entry_repo.list_evidence(entry.id)
                recalled.append(
                    RecalledMemory(
                        memory_entry_id=entry.id,
                        summary=entry.summary,
                        topics=list(entry.topics),
                        entities=list(entry.entities),
                        evidence=[
                            {
                                "kind": evidence.kind,
                                "label": evidence.label,
                                "content": evidence.content,
                            }
                            for evidence in evidence_rows
                        ],
                        score=result.score,
                        rank=len(recalled) + 1,
                    )
                )

            await MemoryRecallRepo(session).record_many(
                conversation_id=conversation_id,
                trigger_id=trigger_id,
                recalled=[
                    {
                        "memory_entry_id": memory.memory_entry_id,
                        "score": memory.score,
                        "rank": memory.rank,
                    }
                    for memory in recalled
                ],
            )
            await entry_repo.mark_recalled([memory.memory_entry_id for memory in recalled])

        return recalled


def _combine_errors(*errors: str | None) -> str | None:
    messages = [error for error in errors if error]
    if not messages:
        return None
    return "; ".join(messages)
