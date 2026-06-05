from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.memory.embeddings import EmbeddingProvider
from jarvis.memory.types import (
    MemoryContext,
    MemorySummary,
    RecalledMemory,
    VectorSearchResult,
)
from jarvis.memory.vector_store import MemoryVectorStore
from jarvis.persistence.models import MemoryEntryRow
from jarvis.persistence.repositories import MemoryEntryRepo, MemoryPreferenceRepo, MemoryRecallRepo

_MAX_PREFERENCE_PROPOSALS_PER_SUMMARY = 5


@dataclass(frozen=True, slots=True)
class MemorySummarizeOutcome:
    status: str
    memory_entry_id: UUID | None = None
    preferences_created: int = 0
    error: str | None = None


class MemoryService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        embedding_provider: EmbeddingProvider,
        vector_store: MemoryVectorStore,
        max_recalled_memories: int,
        min_relevance_score: float,
        summarizer=None,
    ) -> None:
        self._session_factory = session_factory
        self._embedding_provider = embedding_provider
        self._vector_store = vector_store
        self._summarizer = summarizer
        self._max_recalled_memories = max_recalled_memories
        self._min_relevance_score = min_relevance_score

    async def summarize_run(
        self,
        *,
        conversation_id: UUID | None,
        channel_kind: str,
        channel_ref: str,
        user_prompt: str,
        assistant_output: str,
    ) -> MemorySummarizeOutcome:
        if self._summarizer is None:
            return MemorySummarizeOutcome(
                status="skipped",
                error="summarizer unavailable",
            )

        try:
            summary = await self._summarizer.summarize(
                user_prompt=user_prompt,
                assistant_output=assistant_output,
            )
        except Exception as exc:
            return MemorySummarizeOutcome(status="failed", error=str(exc))

        if not summary.summary:
            return MemorySummarizeOutcome(status="skipped", error="empty summary")

        entry_id: UUID | None = None
        entry_status: str | None = None
        entry_created = False
        source_hash = _source_hash(
            conversation_id=conversation_id,
            channel_kind=channel_kind,
            channel_ref=channel_ref,
            user_prompt=user_prompt,
            assistant_output=assistant_output,
        )
        async with self._session_factory() as session:
            try:
                entry_repo = MemoryEntryRepo(session)
                existing_entry = await entry_repo.get_by_source_hash(source_hash)
                if existing_entry is None:
                    existing_entry = await _find_existing_summary_entry(
                        session=session,
                        conversation_id=conversation_id,
                        channel_kind=channel_kind,
                        channel_ref=channel_ref,
                        summary=summary,
                    )
                if existing_entry is None:
                    entry, entry_created = await entry_repo.create_or_get_by_source_hash(
                        source_hash=source_hash,
                        conversation_id=conversation_id,
                        source_channel_kind=channel_kind,
                        source_channel_ref=channel_ref,
                        summary=summary.summary,
                        topics=summary.topics,
                        entities=summary.entities,
                        evidence=summary.evidence,
                        status="indexing",
                    )
                    entry_id = entry.id
                    entry_status = entry.status
                else:
                    entry_id = existing_entry.id
                    entry_status = existing_entry.status
                    if existing_entry.source_hash is None:
                        await _set_memory_entry_source_hash(
                            session=session,
                            memory_entry_id=entry_id,
                            source_hash=source_hash,
                        )
                preferences_created = await _create_preference_proposals(
                    session,
                    summary.preference_candidates,
                )
            except Exception as exc:
                if entry_id is not None:
                    await _mark_unindexed_best_effort(self._session_factory, entry_id)
                return MemorySummarizeOutcome(
                    status="failed",
                    memory_entry_id=entry_id,
                    error=str(exc),
                )

        if entry_status == "active":
            return MemorySummarizeOutcome(
                status="created",
                memory_entry_id=entry_id,
                preferences_created=preferences_created,
            )
        if entry_status == "indexing" and not entry_created:
            return MemorySummarizeOutcome(
                status="created",
                memory_entry_id=entry_id,
                preferences_created=preferences_created,
            )

        if not self._vector_store.available:
            if entry_id is not None:
                await _mark_unindexed_best_effort(self._session_factory, entry_id)
            return MemorySummarizeOutcome(
                status="unindexed",
                memory_entry_id=entry_id,
                preferences_created=preferences_created,
                error=self._vector_store.last_error or "vector store unavailable",
            )

        try:
            embedding = await self._embedding_provider.embed(_embedding_text(summary))
            await self._vector_store.upsert(entry_id, embedding)
        except Exception as exc:
            if entry_id is not None:
                await _mark_unindexed_best_effort(self._session_factory, entry_id)
            return MemorySummarizeOutcome(
                status="unindexed",
                memory_entry_id=entry_id,
                preferences_created=preferences_created,
                error=str(exc),
            )

        try:
            async with self._session_factory() as session:
                await _set_memory_entry_status(session, entry_id, "active")
        except Exception as exc:
            return MemorySummarizeOutcome(
                status="failed",
                memory_entry_id=entry_id,
                preferences_created=preferences_created,
                error=str(exc),
            )

        return MemorySummarizeOutcome(
            status="created",
            memory_entry_id=entry_id,
            preferences_created=preferences_created,
        )

    async def build_context(
        self,
        *,
        conversation_id: UUID | None,
        trigger_id: UUID | None,
        prompt: str,
    ) -> MemoryContext:
        preferences, preference_error = await self._load_preferences()

        if self._max_recalled_memories <= 0:
            return MemoryContext(
                preferences=preferences,
                recalled=[],
                recall_available=False,
                error=preference_error,
            )

        if not self._vector_store.available:
            return MemoryContext(
                preferences=preferences,
                recalled=[],
                recall_available=False,
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
            recalled, recall_error = await self._load_recalled_memories(
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
            error=_combine_errors(preference_error, recall_error),
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
    ) -> tuple[list[RecalledMemory], str | None]:
        if not results:
            return [], None

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

            try:
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
            except Exception as exc:
                return recalled, str(exc)

        return recalled, None


def _combine_errors(*errors: str | None) -> str | None:
    messages = [error for error in errors if error]
    if not messages:
        return None
    return "; ".join(messages)


def _embedding_text(summary: MemorySummary) -> str:
    parts = [
        summary.summary,
        " ".join(summary.topics),
        " ".join(summary.entities),
    ]
    return "\n".join(part for part in parts if part)


def _source_hash(
    *,
    conversation_id: UUID | None,
    channel_kind: str,
    channel_ref: str,
    user_prompt: str,
    assistant_output: str,
) -> str:
    payload = {
        "conversation_id": str(conversation_id) if conversation_id else None,
        "channel_kind": channel_kind,
        "channel_ref": channel_ref,
        "user_prompt": user_prompt,
        "assistant_output": assistant_output,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


async def _set_memory_entry_status(
    session: AsyncSession,
    memory_entry_id: UUID,
    status: str,
) -> None:
    await session.execute(
        update(MemoryEntryRow)
        .where(MemoryEntryRow.id == memory_entry_id)
        .values(status=status, updated_at=datetime.now(UTC))
    )
    await session.commit()


async def _set_memory_entry_source_hash(
    *,
    session: AsyncSession,
    memory_entry_id: UUID,
    source_hash: str,
) -> None:
    try:
        await session.execute(
            update(MemoryEntryRow)
            .where(MemoryEntryRow.id == memory_entry_id)
            .values(source_hash=source_hash, updated_at=datetime.now(UTC))
        )
        await session.commit()
    except Exception:
        await session.rollback()


async def _mark_unindexed_best_effort(
    session_factory: async_sessionmaker[AsyncSession],
    memory_entry_id: UUID,
) -> None:
    try:
        async with session_factory() as session:
            await _set_memory_entry_status(session, memory_entry_id, "unindexed")
    except Exception:
        return


async def _create_preference_proposals(
    session: AsyncSession,
    candidates: list[str],
) -> int:
    existing = await _existing_preferences(session)
    limited_candidates = _limited_unique_preference_candidates(candidates)
    proposals = [
        content
        for normalized, content in limited_candidates
        if normalized not in existing
    ]
    if not proposals:
        return 0

    rows = await MemoryPreferenceRepo(session).create_pending_many(
        contents=proposals,
        source="agent_proposal",
    )
    return len(rows)


def _limited_unique_preference_candidates(candidates: list[str]) -> list[tuple[str, str]]:
    proposals = []
    seen = set()
    for candidate in candidates:
        content = str(candidate).strip()
        normalized = _normalize_preference(content)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        proposals.append((normalized, content))
        if len(proposals) >= _MAX_PREFERENCE_PROPOSALS_PER_SUMMARY:
            break
    return proposals


async def _existing_preferences(session: AsyncSession) -> set[str]:
    return await MemoryPreferenceRepo(session).existing_normalized_contents()


def _normalize_preference(content: str) -> str:
    return re.sub(r"\s+", " ", content).strip().casefold()


async def _find_existing_summary_entry(
    *,
    session: AsyncSession,
    conversation_id: UUID | None,
    channel_kind: str,
    channel_ref: str,
    summary: MemorySummary,
) -> MemoryEntryRow | None:
    stmt = select(MemoryEntryRow).where(
        MemoryEntryRow.conversation_id == conversation_id,
        MemoryEntryRow.source_channel_kind == channel_kind,
        MemoryEntryRow.source_channel_ref == channel_ref,
        MemoryEntryRow.summary == summary.summary,
    )
    result = await session.execute(stmt)
    for entry in result.scalars():
        if (
            list(entry.topics) == summary.topics
            and list(entry.entities) == summary.entities
            and await _entry_evidence_matches(session, entry.id, summary.evidence)
        ):
            return entry
    return None


async def _entry_evidence_matches(
    session: AsyncSession,
    entry_id: UUID,
    evidence: list[dict[str, str]],
) -> bool:
    rows = await MemoryEntryRepo(session).list_evidence(entry_id)
    return [
        {
            "kind": row.kind,
            "label": row.label,
            "content": row.content,
        }
        for row in rows
    ] == evidence
