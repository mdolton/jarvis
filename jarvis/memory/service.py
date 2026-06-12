from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.core.types import AuditEvent, AuditEventType
from jarvis.memory.embeddings import EmbeddingProvider
from jarvis.memory.preference_dedup import (
    ClusterPreference,
    DuplicateMatch,
    ExistingPreference,
    choose_keeper,
)
from jarvis.memory.types import (
    MemoryContext,
    MemorySummary,
    RecalledMemory,
    VectorSearchResult,
)
from jarvis.memory.vector_store import MemoryVectorStore
from jarvis.persistence.models import MemoryEntryRow, MemoryPreferenceRow
from jarvis.persistence.repositories import (
    MemoryEntryRepo,
    MemoryPreferenceRepo,
    MemoryRecallRepo,
    NewPreference,
)

_MAX_PREFERENCE_PROPOSALS_PER_SUMMARY = 5


@dataclass(frozen=True, slots=True)
class MemorySummarizeOutcome:
    status: str
    memory_entry_id: UUID | None = None
    preferences_created: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _ProposalResult:
    created: list[MemoryPreferenceRow]
    dropped: list[DuplicateMatch]
    fell_back: bool


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
        audit=None,
        preference_deduplicator=None,
    ) -> None:
        self._session_factory = session_factory
        self._embedding_provider = embedding_provider
        self._vector_store = vector_store
        self._summarizer = summarizer
        self._audit = audit
        self._preference_deduplicator = preference_deduplicator
        self._max_recalled_memories = max_recalled_memories
        self._min_relevance_score = min_relevance_score

    async def reindex_entries(self) -> int:
        if not self._vector_store.available:
            return 0

        async with self._session_factory() as session:
            entries = await MemoryEntryRepo(session).list_for_reindex()

        reindexed = 0
        for entry in entries:
            try:
                embedding = await self._embedding_provider.embed(_entry_embedding_text(entry))
                await self._vector_store.upsert(entry.id, embedding)
                async with self._session_factory() as session:
                    await _set_memory_entry_status(session, entry.id, "active")
            except Exception as exc:
                await _mark_unindexed_best_effort(self._session_factory, entry.id)
                await self._emit(
                    AuditEventType.MEMORY_FAILED,
                    conversation_id=entry.conversation_id,
                    payload={"stage": "reindex", "error": str(exc)},
                )
                continue
            reindexed += 1
        return reindexed

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
            await self._emit(
                AuditEventType.MEMORY_FAILED,
                conversation_id=conversation_id,
                payload={"stage": "summarize", "error": str(exc)},
            )
            return MemorySummarizeOutcome(status="failed", error=str(exc))

        if not summary.summary:
            return MemorySummarizeOutcome(status="skipped", error="empty summary")

        entry_id: UUID | None = None
        entry_status: str | None = None
        entry_created = False
        proposal_result: _ProposalResult = _ProposalResult([], [], False)
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
                proposal_result = await _create_preference_proposals(
                    session,
                    summary.preference_candidates,
                    self._preference_deduplicator,
                )
                created_preferences = proposal_result.created
                preferences_created = len(created_preferences)
            except Exception as exc:
                if entry_id is not None:
                    await _mark_unindexed_best_effort(self._session_factory, entry_id)
                await self._emit(
                    AuditEventType.MEMORY_FAILED,
                    conversation_id=conversation_id,
                    payload={"stage": "summarize", "error": str(exc)},
                )
                return MemorySummarizeOutcome(
                    status="failed",
                    memory_entry_id=entry_id,
                    error=str(exc),
                )

        if entry_created and entry_id is not None:
            await self._emit(
                AuditEventType.MEMORY_ENTRY_CREATED,
                conversation_id=conversation_id,
                payload={"memory_entry_id": str(entry_id)},
            )
        for preference in created_preferences:
            await self._emit(
                AuditEventType.MEMORY_PREFERENCE_PROPOSED,
                conversation_id=conversation_id,
                payload={
                    "preference_id": str(preference.id),
                    "content": preference.content,
                },
            )
        for dropped in proposal_result.dropped:
            await self._emit(
                AuditEventType.MEMORY_PREFERENCE_DEDUP_DROPPED,
                conversation_id=conversation_id,
                payload={
                    "matched_preference_id": str(dropped.matched_id) if dropped.matched_id else None,
                    "matched_content": dropped.matched_content,
                    "score": dropped.score,
                    "method": dropped.method,
                },
            )
        if proposal_result.fell_back:
            await self._emit(
                AuditEventType.MEMORY_PREFERENCE_DEDUP_SKIPPED,
                conversation_id=conversation_id,
                payload={"reason": "dedup pass failed; used exact-match fallback"},
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
            await self._emit(
                AuditEventType.MEMORY_FAILED,
                conversation_id=conversation_id,
                payload={
                    "stage": "index",
                    "error": self._vector_store.last_error or "vector store unavailable",
                },
            )
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
            await self._emit(
                AuditEventType.MEMORY_FAILED,
                conversation_id=conversation_id,
                payload={"stage": "index", "error": str(exc)},
            )
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
            await self._emit(
                AuditEventType.MEMORY_FAILED,
                conversation_id=conversation_id,
                payload={"stage": "index", "error": str(exc)},
            )
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

    async def find_duplicate_preferences(self) -> list[dict]:
        if self._preference_deduplicator is None:
            return []
        async with self._session_factory() as session:
            repo = MemoryPreferenceRepo(session)
            rows = await repo.list_for_dedup()
            cluster_prefs: list[ClusterPreference] = []
            for row in rows:
                embedding = row.embedding
                dims = row.embedding_dimensions
                if embedding is None:
                    embedding = await self._preference_deduplicator.embed(row.content)
                    if embedding is not None:
                        dims = len(embedding)
                        await repo.set_embedding(row.id, embedding, dims)
                cluster_prefs.append(
                    ClusterPreference(
                        preference_id=row.id,
                        content=row.content,
                        status=row.status,
                        created_at=row.created_at,
                        updated_at=row.updated_at,
                        embedding=embedding,
                        embedding_dimensions=dims,
                    )
                )
        groups = await self._preference_deduplicator.cluster(cluster_prefs)
        clusters: list[dict] = []
        for group in groups:
            keeper = choose_keeper(group)
            clusters.append(
                {
                    "keeper": keeper,
                    "duplicates": [p for p in group if p.preference_id != keeper.preference_id],
                }
            )
        return clusters

    async def build_context(
        self,
        *,
        conversation_id: UUID | None,
        trigger_id: UUID | None,
        prompt: str,
    ) -> MemoryContext:
        preferences, preference_error = await self._load_preferences()
        if preference_error is not None:
            await self._emit(
                AuditEventType.MEMORY_FAILED,
                conversation_id=conversation_id,
                trigger_id=trigger_id,
                payload={"stage": "preferences", "error": preference_error},
            )

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
            await self._emit(
                AuditEventType.MEMORY_FAILED,
                conversation_id=conversation_id,
                trigger_id=trigger_id,
                payload={"stage": "recall", "error": str(exc)},
            )
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
            await self._emit(
                AuditEventType.MEMORY_FAILED,
                conversation_id=conversation_id,
                trigger_id=trigger_id,
                payload={"stage": "recall", "error": str(exc)},
            )
            return MemoryContext(
                preferences=preferences,
                recalled=[],
                recall_available=False,
                error=_combine_errors(preference_error, str(exc)),
            )

        if recalled:
            await self._emit(
                AuditEventType.MEMORY_RECALLED,
                conversation_id=conversation_id,
                trigger_id=trigger_id,
                payload={
                    "count": len(recalled),
                    "memory_entry_ids": [
                        str(memory.memory_entry_id) for memory in recalled
                    ],
                },
            )
        if recall_error is not None:
            await self._emit(
                AuditEventType.MEMORY_FAILED,
                conversation_id=conversation_id,
                trigger_id=trigger_id,
                payload={"stage": "recall_bookkeeping", "error": recall_error},
            )

        return MemoryContext(
            preferences=preferences,
            recalled=recalled,
            recall_available=True,
            error=_combine_errors(preference_error, recall_error),
        )

    async def _emit(
        self,
        event_type: AuditEventType,
        *,
        conversation_id: UUID | None = None,
        trigger_id: UUID | None = None,
        payload: dict | None = None,
    ) -> None:
        if self._audit is None:
            return
        try:
            await self._audit.emit(
                AuditEvent(
                    type=event_type,
                    conversation_id=conversation_id,
                    trigger_id=trigger_id,
                    payload=payload or {},
                )
            )
        except Exception:
            return

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


def _entry_embedding_text(entry: MemoryEntryRow) -> str:
    return _embedding_text(
        MemorySummary(
            summary=entry.summary,
            topics=entry.topics or [],
            entities=entry.entities or [],
            evidence=[],
            preference_candidates=[],
        )
    )


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
    deduplicator,
) -> _ProposalResult:
    existing_norm = await _existing_preferences(session)
    limited_candidates = _limited_unique_preference_candidates(candidates)
    surviving = [
        (normalized, content)
        for normalized, content in limited_candidates
        if normalized not in existing_norm
    ]
    if not surviving:
        return _ProposalResult([], [], False)

    repo = MemoryPreferenceRepo(session)

    if deduplicator is None:
        created = await repo.create_pending_many(
            items=[NewPreference(content=content) for _, content in surviving],
            source="agent_proposal",
        )
        return _ProposalResult(created, [], False)

    try:
        existing_rows = await repo.list_for_dedup()
        comparison_set = await _ensure_dedup_embeddings(session, repo, deduplicator, existing_rows)
        budget = deduplicator.new_budget()
        accepted: list[NewPreference] = []
        dropped: list[DuplicateMatch] = []
        for _, content in surviving:
            embedding = await deduplicator.embed(content)
            match = await deduplicator.is_duplicate(
                candidate_content=content,
                candidate_embedding=embedding,
                existing=comparison_set,
                judge_budget=budget,
            )
            if match is not None:
                dropped.append(match)
                continue
            dims = len(embedding) if embedding else None
            accepted.append(
                NewPreference(content=content, embedding=embedding, embedding_dimensions=dims)
            )
            comparison_set.append(
                ExistingPreference(
                    content=content,
                    embedding=embedding,
                    embedding_dimensions=dims,
                    status="pending",
                    preference_id=None,
                )
            )
        created = await repo.create_pending_many(items=accepted, source="agent_proposal")
        return _ProposalResult(created, dropped, False)
    except Exception:
        created = await repo.create_pending_many(
            items=[NewPreference(content=content) for _, content in surviving],
            source="agent_proposal",
        )
        return _ProposalResult(created, [], True)


async def _ensure_dedup_embeddings(
    session: AsyncSession,
    repo: MemoryPreferenceRepo,
    deduplicator,
    rows: list[MemoryPreferenceRow],
) -> list[ExistingPreference]:
    result: list[ExistingPreference] = []
    for row in rows:
        embedding = row.embedding
        dims = row.embedding_dimensions
        if embedding is None:
            embedding = await deduplicator.embed(row.content)
            if embedding is not None:
                dims = len(embedding)
                await repo.set_embedding(row.id, embedding, dims)
        result.append(
            ExistingPreference(
                content=row.content,
                embedding=embedding,
                embedding_dimensions=dims,
                status=row.status,
                preference_id=row.id,
            )
        )
    return result


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
