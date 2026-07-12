"""Repositories — the only way core modules touch the database."""

import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from jarvis.core.types import AuditEvent, AuditEventType, ChannelKind, MessageRole
from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.persistence.models import (
    ActionRow,
    AuditEventRow,
    AuthCodeRow,
    ConversationRow,
    DigestTemplateRow,
    DocumentChunkRow,
    DocumentRow,
    MCPServerRow,
    MCPToolRow,
    MemoryEntryRow,
    MemoryEvidenceRow,
    MemoryPreferenceRow,
    MemoryRecallEventRow,
    MessageRow,
    NotificationRow,
    RecoveryCodeRow,
    ScheduleRow,
    SessionRow,
    SettingRow,
    TriggerRow,
    UserRow,
    WebAuthnChallengeRow,
    WebAuthnCredentialRow,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class NewPreference:
    content: str
    embedding: list[float] | None = None
    embedding_dimensions: int | None = None


class ConversationRepo:
    """Per-channel conversation sessions with idle-timeout semantics."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_or_create_open(
        self,
        *,
        channel_kind: ChannelKind,
        channel_ref: str,
        idle_timeout_sec: int,
    ) -> ConversationRow:
        """Return an open conversation for (kind, ref).

        If the newest open one is stale (last_activity older than
        idle_timeout_sec), close it and open a fresh one. An
        idle_timeout_sec of 0 always opens a fresh conversation.
        """
        now = _utcnow()

        if idle_timeout_sec == 0:
            return await self._create(channel_kind, channel_ref, now)

        result = await self._session.execute(
            select(ConversationRow)
            .where(
                ConversationRow.channel_kind == channel_kind.value,
                ConversationRow.channel_ref == channel_ref,
                ConversationRow.status == "open",
            )
            .order_by(ConversationRow.last_activity_at.desc())
            .limit(1)
        )
        existing = result.scalar_one_or_none()

        if existing is not None:
            threshold = now - timedelta(seconds=idle_timeout_sec)
            if existing.last_activity_at >= threshold:
                existing.last_activity_at = now
                await self._session.commit()
                await self._session.refresh(existing)
                return existing
            existing.status = "closed"
            await self._session.commit()

        return await self._create(channel_kind, channel_ref, now)

    async def _create(
        self,
        channel_kind: ChannelKind,
        channel_ref: str,
        now: datetime,
    ) -> ConversationRow:
        conv = ConversationRow(
            channel_kind=channel_kind.value,
            channel_ref=channel_ref,
            started_at=now,
            last_activity_at=now,
            status="open",
        )
        self._session.add(conv)
        await self._session.commit()
        await self._session.refresh(conv)
        return conv

    async def list_recent(self, *, limit: int = 50) -> list[ConversationRow]:
        result = await self._session.execute(
            select(ConversationRow).order_by(ConversationRow.last_activity_at.desc()).limit(limit)
        )
        return list(result.scalars())

    async def touch(self, conversation_id: UUID) -> None:
        await self.touch_no_commit(conversation_id)
        await self._session.commit()

    async def touch_no_commit(self, conversation_id: UUID) -> None:
        await self._session.execute(
            update(ConversationRow)
            .where(ConversationRow.id == conversation_id)
            .values(last_activity_at=_utcnow())
        )


class MessageRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._conv_repo = ConversationRepo(session)

    async def append(
        self,
        *,
        conversation_id: UUID,
        role: MessageRole,
        content: str,
    ) -> MessageRow:
        msg = await self.append_no_commit(
            conversation_id=conversation_id,
            role=role,
            content=content,
        )
        await self._session.commit()
        await self._session.refresh(msg)
        return msg

    async def append_no_commit(
        self,
        *,
        conversation_id: UUID,
        role: MessageRole,
        content: str,
    ) -> MessageRow:
        msg = MessageRow(
            conversation_id=conversation_id,
            role=role.value,
            content=content,
            created_at=_utcnow(),
        )
        self._session.add(msg)
        await self._conv_repo.touch_no_commit(conversation_id)
        await self._session.flush()
        return msg

    async def history(self, conversation_id: UUID) -> list[MessageRow]:
        result = await self._session.execute(
            select(MessageRow)
            .where(MessageRow.conversation_id == conversation_id)
            .order_by(MessageRow.created_at.asc())
        )
        return list(result.scalars())

    async def recent_history(self, conversation_id: UUID, *, limit: int) -> list[MessageRow]:
        """Return the newest `limit` messages of a conversation, oldest first."""
        result = await self._session.execute(
            select(MessageRow)
            .where(MessageRow.conversation_id == conversation_id)
            .order_by(MessageRow.created_at.desc())
            .limit(limit)
        )
        return list(reversed(list(result.scalars())))


class TriggerRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, *, kind: str, source_ref: str) -> TriggerRow:
        trig = TriggerRow(kind=kind, source_ref=source_ref, created_at=_utcnow())
        self._session.add(trig)
        await self._session.commit()
        await self._session.refresh(trig)
        return trig


class AuditRepo:
    """Append-only audit event store."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def write_many(self, events: list[AuditEvent]) -> None:
        rows = [
            AuditEventRow(
                id=e.id,
                conversation_id=e.conversation_id,
                trigger_id=e.trigger_id,
                type=e.type.value,
                payload=e.payload,
                created_at=e.created_at,
            )
            for e in events
        ]
        self._session.add_all(rows)
        await self._session.commit()

    async def recent(
        self,
        *,
        types: list[AuditEventType] | None = None,
        limit: int = 100,
    ) -> list[AuditEventRow]:
        stmt = select(AuditEventRow).order_by(AuditEventRow.created_at.desc()).limit(limit)
        if types:
            stmt = stmt.where(AuditEventRow.type.in_([t.value for t in types]))
        result = await self._session.execute(stmt)
        return list(result.scalars())

    async def recent_as_events(
        self,
        *,
        types: list[AuditEventType] | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """Same as recent(), but maps each row to an AuditEvent Pydantic model."""
        rows = await self.recent(types=types, limit=limit)
        return [
            AuditEvent(
                id=r.id,
                conversation_id=r.conversation_id,
                trigger_id=r.trigger_id,
                type=AuditEventType(r.type),
                payload=r.payload,
                created_at=r.created_at,
            )
            for r in rows
        ]


class MemoryPreferenceRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_pending(
        self,
        *,
        content: str,
        source: str,
        embedding: list[float] | None = None,
        embedding_dimensions: int | None = None,
    ) -> MemoryPreferenceRow:
        now = _utcnow()
        content_normalized = _normalize_preference_content(content)
        row = MemoryPreferenceRow(
            content=content,
            content_normalized=content_normalized,
            status="pending",
            source=source,
            embedding=embedding,
            embedding_dimensions=embedding_dimensions,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            existing = await self.get_by_normalized_content(content_normalized)
            if existing is None:
                raise
            return existing
        await self._session.refresh(row)
        return row

    async def create_pending_many(
        self,
        *,
        items: list[NewPreference],
        source: str,
    ) -> list[MemoryPreferenceRow]:
        if not items:
            return []
        items = _dedupe_new_preferences(items)
        now = _utcnow()
        rows = [
            MemoryPreferenceRow(
                content=item.content,
                content_normalized=_normalize_preference_content(item.content),
                status="pending",
                source=source,
                embedding=item.embedding,
                embedding_dimensions=item.embedding_dimensions,
                created_at=now,
                updated_at=now,
            )
            for item in items
        ]
        try:
            self._session.add_all(rows)
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            return await self._create_missing_pending(items=items, source=source)
        except Exception:
            await self._session.rollback()
            raise
        for row in rows:
            await self._session.refresh(row)
        return rows

    async def _create_missing_pending(
        self,
        *,
        items: list[NewPreference],
        source: str,
    ) -> list[MemoryPreferenceRow]:
        existing = await self.existing_normalized_contents()
        missing = _dedupe_new_preferences(
            [item for item in items if _normalize_preference_content(item.content) not in existing]
        )
        if not missing:
            return []
        now = _utcnow()
        rows = [
            MemoryPreferenceRow(
                content=item.content,
                content_normalized=_normalize_preference_content(item.content),
                status="pending",
                source=source,
                embedding=item.embedding,
                embedding_dimensions=item.embedding_dimensions,
                created_at=now,
                updated_at=now,
            )
            for item in missing
        ]
        try:
            self._session.add_all(rows)
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            return []
        except Exception:
            await self._session.rollback()
            raise
        for row in rows:
            await self._session.refresh(row)
        return rows

    async def existing_normalized_contents(self) -> set[str]:
        result = await self._session.execute(select(MemoryPreferenceRow.content))
        return {_normalize_preference_content(content) for content in result.scalars()}

    async def get_by_normalized_content(
        self,
        content_normalized: str,
    ) -> MemoryPreferenceRow | None:
        result = await self._session.execute(
            select(MemoryPreferenceRow).where(
                MemoryPreferenceRow.content_normalized == content_normalized
            )
        )
        return result.scalar_one_or_none()

    async def list_active(self) -> list[MemoryPreferenceRow]:
        result = await self._session.execute(
            select(MemoryPreferenceRow)
            .where(MemoryPreferenceRow.status == "active")
            .order_by(MemoryPreferenceRow.updated_at.desc())
        )
        return list(result.scalars())

    async def list_for_dashboard(self, *, limit: int = 100) -> list[MemoryPreferenceRow]:
        result = await self._session.execute(
            select(MemoryPreferenceRow)
            .order_by(
                case((MemoryPreferenceRow.status == "pending", 0), else_=1),
                MemoryPreferenceRow.updated_at.desc(),
            )
            .limit(limit)
        )
        return list(result.scalars())

    async def list_for_dedup(self) -> list[MemoryPreferenceRow]:
        result = await self._session.execute(
            select(MemoryPreferenceRow)
            .where(MemoryPreferenceRow.status != "archived")
            .order_by(MemoryPreferenceRow.created_at.asc())
        )
        return list(result.scalars())

    async def set_embedding(
        self,
        preference_id: UUID,
        embedding: list[float],
        embedding_dimensions: int,
    ) -> None:
        row = await self._session.get(MemoryPreferenceRow, preference_id)
        if row is None:
            return
        row.embedding = embedding
        row.embedding_dimensions = embedding_dimensions
        row.updated_at = _utcnow()
        await self._session.commit()

    async def approve(self, preference_id: UUID) -> None:
        row = await self._session.get(MemoryPreferenceRow, preference_id)
        if row is None:
            raise LookupError("memory preference not found")
        if row.status != "pending":
            raise ValueError("invalid preference transition")
        now = _utcnow()
        row.status = "active"
        row.approved_at = now
        row.updated_at = now
        await self._session.commit()

    async def reject(self, preference_id: UUID) -> None:
        row = await self._session.get(MemoryPreferenceRow, preference_id)
        if row is None:
            raise LookupError("memory preference not found")
        if row.status != "pending":
            raise ValueError("invalid preference transition")
        row.status = "rejected"
        row.updated_at = _utcnow()
        await self._session.commit()

    async def archive(self, preference_id: UUID) -> None:
        row = await self._session.get(MemoryPreferenceRow, preference_id)
        if row is None:
            raise LookupError("memory preference not found")
        if row.status == "archived":
            raise ValueError("invalid preference transition")
        now = _utcnow()
        row.status = "archived"
        row.archived_at = now
        row.updated_at = now
        await self._session.commit()


class MemoryEntryRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_or_get_by_source_hash(
        self,
        *,
        source_hash: str,
        conversation_id: UUID | None,
        source_channel_kind: str,
        source_channel_ref: str,
        summary: str,
        topics: list,
        entities: list,
        evidence: list[dict],
        status: str,
    ) -> tuple[MemoryEntryRow, bool]:
        now = _utcnow()
        row = MemoryEntryRow(
            conversation_id=conversation_id,
            source_channel_kind=source_channel_kind,
            source_channel_ref=source_channel_ref,
            source_hash=source_hash,
            summary=summary,
            topics=topics,
            entities=entities,
            status=status,
            created_at=now,
            updated_at=now,
            evidence=[
                MemoryEvidenceRow(
                    kind=item["kind"],
                    label=item["label"],
                    content=item["content"],
                    created_at=now,
                )
                for item in evidence
            ],
        )
        self._session.add(row)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            existing = await self.get_by_source_hash(source_hash)
            if existing is None:
                raise
            return existing, False
        result = await self._session.execute(
            select(MemoryEntryRow)
            .where(MemoryEntryRow.id == row.id)
            .options(selectinload(MemoryEntryRow.evidence))
        )
        return result.scalar_one(), True

    async def create(
        self,
        *,
        conversation_id: UUID | None,
        source_channel_kind: str,
        source_channel_ref: str,
        summary: str,
        topics: list,
        entities: list,
        evidence: list[dict],
        source_hash: str | None = None,
        status: str = "active",
    ) -> MemoryEntryRow:
        now = _utcnow()
        row = MemoryEntryRow(
            conversation_id=conversation_id,
            source_channel_kind=source_channel_kind,
            source_channel_ref=source_channel_ref,
            source_hash=source_hash,
            summary=summary,
            topics=topics,
            entities=entities,
            status=status,
            created_at=now,
            updated_at=now,
            evidence=[
                MemoryEvidenceRow(
                    kind=item["kind"],
                    label=item["label"],
                    content=item["content"],
                    created_at=now,
                )
                for item in evidence
            ],
        )
        self._session.add(row)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            if source_hash is None:
                raise
            existing = await self.get_by_source_hash(source_hash)
            if existing is None:
                raise
            return existing
        result = await self._session.execute(
            select(MemoryEntryRow)
            .where(MemoryEntryRow.id == row.id)
            .options(selectinload(MemoryEntryRow.evidence))
        )
        return result.scalar_one()

    async def get_by_source_hash(self, source_hash: str) -> MemoryEntryRow | None:
        result = await self._session.execute(
            select(MemoryEntryRow)
            .where(MemoryEntryRow.source_hash == source_hash)
            .options(selectinload(MemoryEntryRow.evidence))
        )
        return result.scalar_one_or_none()

    async def list_recent(self, *, limit: int = 100) -> list[MemoryEntryRow]:
        result = await self._session.execute(
            select(MemoryEntryRow).order_by(MemoryEntryRow.updated_at.desc()).limit(limit)
        )
        return list(result.scalars())

    async def list_for_reindex(self, *, limit: int = 1000) -> list[MemoryEntryRow]:
        result = await self._session.execute(
            select(MemoryEntryRow)
            .where(MemoryEntryRow.status.in_(("active", "indexing", "unindexed")))
            .order_by(MemoryEntryRow.updated_at.asc())
            .limit(limit)
        )
        return list(result.scalars())

    async def list_by_ids(self, ids: list[UUID]) -> list[MemoryEntryRow]:
        if not ids:
            return []
        ordering = case(
            {memory_id: index for index, memory_id in enumerate(ids)}, value=MemoryEntryRow.id
        )
        result = await self._session.execute(
            select(MemoryEntryRow).where(MemoryEntryRow.id.in_(ids)).order_by(ordering)
        )
        return list(result.scalars())

    async def list_active_by_ids(self, ids: list[UUID]) -> list[MemoryEntryRow]:
        if not ids:
            return []
        ordering = case(
            {memory_id: index for index, memory_id in enumerate(ids)}, value=MemoryEntryRow.id
        )
        result = await self._session.execute(
            select(MemoryEntryRow)
            .where(MemoryEntryRow.id.in_(ids), MemoryEntryRow.status == "active")
            .order_by(ordering)
        )
        return list(result.scalars())

    async def list_evidence(self, memory_entry_id: UUID) -> list[MemoryEvidenceRow]:
        result = await self._session.execute(
            select(MemoryEvidenceRow)
            .where(MemoryEvidenceRow.memory_entry_id == memory_entry_id)
            .order_by(MemoryEvidenceRow.created_at.asc())
        )
        return list(result.scalars())

    async def list_evidence_for_entries(
        self, ids: list[UUID]
    ) -> dict[UUID, list[MemoryEvidenceRow]]:
        evidence_by_entry = {memory_id: [] for memory_id in ids}
        if not ids:
            return evidence_by_entry
        ordering = case(
            {memory_id: index for index, memory_id in enumerate(ids)},
            value=MemoryEvidenceRow.memory_entry_id,
        )
        result = await self._session.execute(
            select(MemoryEvidenceRow)
            .where(MemoryEvidenceRow.memory_entry_id.in_(ids))
            .order_by(ordering, MemoryEvidenceRow.created_at.asc())
        )
        for evidence in result.scalars():
            evidence_by_entry[evidence.memory_entry_id].append(evidence)
        return evidence_by_entry

    async def archive(self, memory_entry_id: UUID) -> None:
        row = await self._session.get(MemoryEntryRow, memory_entry_id)
        if row is None:
            raise LookupError("memory entry not found")
        if row.status == "archived":
            raise ValueError("invalid memory entry transition")
        row.status = "archived"
        row.updated_at = _utcnow()
        await self._session.commit()

    async def mark_recalled(self, ids: list[UUID]) -> None:
        if not ids:
            return
        await self._session.execute(
            update(MemoryEntryRow)
            .where(MemoryEntryRow.id.in_(ids))
            .values(last_recalled_at=_utcnow())
        )
        await self._session.commit()


class MemoryRecallRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_many(
        self,
        *,
        conversation_id: UUID | None,
        trigger_id: UUID | None,
        recalled: list[dict],
    ) -> None:
        now = _utcnow()
        rows = [
            MemoryRecallEventRow(
                conversation_id=conversation_id,
                trigger_id=trigger_id,
                memory_entry_id=item["memory_entry_id"],
                score=item["score"],
                rank=item["rank"],
                created_at=now,
            )
            for item in recalled
        ]
        self._session.add_all(rows)
        await self._session.commit()

    async def list_for_conversation(self, conversation_id: UUID) -> list[MemoryRecallEventRow]:
        result = await self._session.execute(
            select(MemoryRecallEventRow)
            .where(MemoryRecallEventRow.conversation_id == conversation_id)
            .order_by(MemoryRecallEventRow.created_at.desc(), MemoryRecallEventRow.rank.asc())
        )
        return list(result.scalars())


class DocumentRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_source_ref(self, source_ref: str) -> DocumentRow | None:
        stmt = select(DocumentRow).where(DocumentRow.source_ref == source_ref)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        source_type: str,
        source_ref: str,
        title: str,
        content_hash: str,
    ) -> DocumentRow:
        now = _utcnow()
        row = DocumentRow(
            source_type=source_type,
            source_ref=source_ref,
            title=title,
            content_hash=content_hash,
            status="indexing",
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.commit()
        return row

    async def mark_reingesting(
        self,
        document_id: UUID,
        *,
        title: str,
        content_hash: str,
    ) -> None:
        await self._session.execute(
            update(DocumentRow)
            .where(DocumentRow.id == document_id)
            .values(
                title=title,
                content_hash=content_hash,
                status="indexing",
                error=None,
                updated_at=_utcnow(),
            )
        )
        await self._session.commit()

    async def set_status(
        self,
        document_id: UUID,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        await self._session.execute(
            update(DocumentRow)
            .where(DocumentRow.id == document_id)
            .values(status=status, error=error, updated_at=_utcnow())
        )
        await self._session.commit()

    async def replace_chunks(
        self,
        document_id: UUID,
        contents: list[str],
    ) -> tuple[list[UUID], list[DocumentChunkRow]]:
        old_stmt = select(DocumentChunkRow.id).where(DocumentChunkRow.document_id == document_id)
        old_ids = list((await self._session.execute(old_stmt)).scalars())
        await self._session.execute(
            delete(DocumentChunkRow).where(DocumentChunkRow.document_id == document_id)
        )
        now = _utcnow()
        rows = [
            DocumentChunkRow(
                document_id=document_id,
                chunk_index=index,
                content=content,
                created_at=now,
            )
            for index, content in enumerate(contents)
        ]
        self._session.add_all(rows)
        await self._session.commit()
        return old_ids, rows

    async def count_chunks(self, document_id: UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(DocumentChunkRow)
            .where(DocumentChunkRow.document_id == document_id)
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def get_chunks_with_documents(
        self,
        ids: list[UUID],
    ) -> dict[UUID, tuple[DocumentChunkRow, DocumentRow]]:
        if not ids:
            return {}
        stmt = (
            select(DocumentChunkRow, DocumentRow)
            .join(DocumentRow, DocumentChunkRow.document_id == DocumentRow.id)
            .where(DocumentChunkRow.id.in_(ids), DocumentRow.status == "active")
        )
        result = await self._session.execute(stmt)
        return {chunk.id: (chunk, document) for chunk, document in result.all()}


def _normalize_preference_content(content: str) -> str:
    return re.sub(r"\s+", " ", content).strip().casefold()


def _dedupe_new_preferences(items: list[NewPreference]) -> list[NewPreference]:
    """Drop items whose normalized content repeats within the batch (first wins)."""
    deduped: list[NewPreference] = []
    seen: set[str] = set()
    for item in items:
        normalized = _normalize_preference_content(item.content)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(item)
    return deduped


class ScheduleRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        name: str,
        description: str,
        cron_expr: str,
        timezone: str,
        prompt: str,
        output_mode: str,
        notify_on_error: bool,
        enabled: bool,
        model: str | None = None,
        discord_user_id: str | None = None,
    ) -> ScheduleRow:
        now = _utcnow()
        row = ScheduleRow(
            name=name,
            description=description,
            cron_expr=cron_expr,
            timezone=timezone,
            prompt=prompt,
            output_mode=output_mode,
            model=model,
            discord_user_id=discord_user_id,
            notify_on_error=notify_on_error,
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def get(self, schedule_id: UUID) -> ScheduleRow | None:
        return await self._session.get(ScheduleRow, schedule_id)

    async def list_enabled(self) -> list[ScheduleRow]:
        result = await self._session.execute(
            select(ScheduleRow).where(ScheduleRow.enabled.is_(True))
        )
        return list(result.scalars())

    async def set_enabled(self, schedule_id: UUID, enabled: bool) -> None:
        await self._session.execute(
            update(ScheduleRow)
            .where(ScheduleRow.id == schedule_id)
            .values(enabled=enabled, updated_at=_utcnow())
        )
        await self._session.commit()

    async def record_run(
        self,
        schedule_id: UUID,
        *,
        at: datetime,
        status: str,
    ) -> None:
        await self._session.execute(
            update(ScheduleRow)
            .where(ScheduleRow.id == schedule_id)
            .values(last_run_at=at, last_run_status=status)
        )
        await self._session.commit()

    async def list_all(self) -> list[ScheduleRow]:
        result = await self._session.execute(select(ScheduleRow))
        return list(result.scalars())

    async def update(self, schedule_id: UUID, **fields) -> None:
        """Update arbitrary fields on a schedule row."""
        fields["updated_at"] = _utcnow()
        await self._session.execute(
            update(ScheduleRow).where(ScheduleRow.id == schedule_id).values(**fields)
        )
        await self._session.commit()

    async def replace_prompt_exact(self, *, old_prompt: str, new_prompt: str) -> int:
        """Roll forward every schedule whose prompt exactly matches `old_prompt`.

        Used by digest-template seeding: an exact match means the schedule was
        created from a built-in template and never customized. Returns the
        number of rows updated.
        """
        result = await self._session.execute(
            update(ScheduleRow)
            .where(ScheduleRow.prompt == old_prompt)
            .values(prompt=new_prompt, updated_at=_utcnow())
        )
        await self._session.commit()
        return result.rowcount or 0

    async def delete(self, schedule_id: UUID) -> None:
        row = await self._session.get(ScheduleRow, schedule_id)
        if row is not None:
            await self._session.delete(row)
            await self._session.commit()


class DigestTemplateRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        key: str | None,
        name: str,
        description: str,
        category: str,
        prompt: str,
        default_cron_expr: str,
        default_timezone: str,
        default_output_mode: str,
        default_model: str | None,
        default_discord_user_id: str | None,
        built_in: bool,
        enabled: bool,
    ) -> DigestTemplateRow:
        now = _utcnow()
        row = DigestTemplateRow(
            key=key,
            name=name,
            description=description,
            category=category,
            prompt=prompt,
            default_cron_expr=default_cron_expr,
            default_timezone=default_timezone,
            default_output_mode=default_output_mode,
            default_model=default_model,
            default_discord_user_id=default_discord_user_id,
            built_in=built_in,
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def get(self, template_id: UUID) -> DigestTemplateRow | None:
        return await self._session.get(DigestTemplateRow, template_id)

    async def get_by_key(self, key: str) -> DigestTemplateRow | None:
        result = await self._session.execute(
            select(DigestTemplateRow).where(DigestTemplateRow.key == key)
        )
        return result.scalar_one_or_none()

    async def list_enabled(self) -> list[DigestTemplateRow]:
        result = await self._session.execute(
            select(DigestTemplateRow)
            .where(DigestTemplateRow.enabled.is_(True))
            .order_by(DigestTemplateRow.category.asc(), DigestTemplateRow.name.asc())
        )
        return list(result.scalars())

    async def list_all(self) -> list[DigestTemplateRow]:
        result = await self._session.execute(
            select(DigestTemplateRow).order_by(
                DigestTemplateRow.enabled.desc(),
                DigestTemplateRow.category.asc(),
                DigestTemplateRow.name.asc(),
            )
        )
        return list(result.scalars())

    async def update(
        self,
        template_id: UUID,
        *,
        name: str,
        description: str,
        category: str,
        prompt: str,
        default_cron_expr: str,
        default_timezone: str,
        default_output_mode: str,
        default_model: str | None,
        default_discord_user_id: str | None,
    ) -> None:
        await self._session.execute(
            update(DigestTemplateRow)
            .where(DigestTemplateRow.id == template_id)
            .values(
                name=name,
                description=description,
                category=category,
                prompt=prompt,
                default_cron_expr=default_cron_expr,
                default_timezone=default_timezone,
                default_output_mode=default_output_mode,
                default_model=default_model,
                default_discord_user_id=default_discord_user_id,
                updated_at=_utcnow(),
            )
        )
        await self._session.commit()

    async def refresh_prompt(self, template_id: UUID, *, prompt: str) -> None:
        """Replace only the prompt text (seed roll-forward), leaving user-
        tunable fields (name, cron, output mode, ...) untouched."""
        await self._session.execute(
            update(DigestTemplateRow)
            .where(DigestTemplateRow.id == template_id)
            .values(prompt=prompt, updated_at=_utcnow())
        )
        await self._session.commit()

    async def clone(self, template_id: UUID) -> DigestTemplateRow:
        original = await self.get(template_id)
        if original is None:
            raise ValueError(f"digest template {template_id} not found")
        return await self.create(
            key=None,
            name=f"{original.name} Copy",
            description=original.description,
            category=original.category,
            prompt=original.prompt,
            default_cron_expr=original.default_cron_expr,
            default_timezone=original.default_timezone,
            default_output_mode=original.default_output_mode,
            default_model=original.default_model,
            default_discord_user_id=original.default_discord_user_id,
            built_in=False,
            enabled=True,
        )

    async def disable(self, template_id: UUID) -> None:
        row = await self.get(template_id)
        if row is None:
            raise ValueError(f"digest template {template_id} not found")
        if row.built_in:
            raise ValueError("built-in digest templates cannot be disabled")
        row.enabled = False
        row.updated_at = _utcnow()
        await self._session.commit()


class MCPServerRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        *,
        name: str,
        transport: str,
        source: str = "stdio",
        connection_id: "UUID | None" = None,
    ) -> MCPServerRow:
        result = await self._session.execute(select(MCPServerRow).where(MCPServerRow.name == name))
        existing = result.scalar_one_or_none()
        if existing:
            existing.transport = transport
            existing.source = source
            existing.connection_id = connection_id
            await self._session.commit()
            await self._session.refresh(existing)
            return existing

        row = MCPServerRow(
            name=name,
            transport=transport,
            status="disconnected",
            source=source,
            connection_id=connection_id,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def set_status(
        self,
        server_id: UUID,
        *,
        status: str,
        last_error: str | None,
    ) -> None:
        values: dict = {"status": status, "last_error": last_error}
        if status == "connected":
            values["last_connected_at"] = _utcnow()
        await self._session.execute(
            update(MCPServerRow).where(MCPServerRow.id == server_id).values(**values)
        )
        await self._session.commit()

    async def list_all(self) -> list[MCPServerRow]:
        result = await self._session.execute(select(MCPServerRow))
        return list(result.scalars())

    async def delete_stdio_absent_from(self, names: Iterable[str]) -> int:
        """Prune config (``source='stdio'``) server rows whose name is not in `names`.

        Reconciles the table against the current yaml config: rows for servers that
        were removed from the config — or left mislabeled as stdio by the 0011
        migration (old per-provider OAuth/HTTP rows) — are deleted. Connection-backed
        rows (``source='connection'``) are never touched. Deleting via the ORM cascades
        to each server's tools. Returns the number of rows removed.
        """
        keep = set(names)
        result = await self._session.execute(
            select(MCPServerRow).where(MCPServerRow.source == "stdio")
        )
        stale = [row for row in result.scalars() if row.name not in keep]
        for row in stale:
            await self._session.delete(row)
        await self._session.commit()
        return len(stale)


class MCPToolRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_for_server(
        self,
        server_id: UUID,
        *,
        tools: list[MCPToolDescriptor],
    ) -> None:
        """Replace the tool set for a server atomically (full overwrite),
        preserving per-tool `policy_override` user-set state across the swap.
        """
        # Snapshot existing overrides keyed by tool name so we can re-apply
        # them after the delete-then-insert.
        existing = await self._session.execute(
            select(MCPToolRow).where(MCPToolRow.server_id == server_id)
        )
        existing_rows = list(existing.scalars())
        overrides: dict[str, str] = {
            r.name: r.policy_override for r in existing_rows if r.policy_override is not None
        }

        for row in existing_rows:
            await self._session.delete(row)

        for tool in tools:
            self._session.add(
                MCPToolRow(
                    server_id=server_id,
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    read_only_hint=tool.read_only_hint,
                    destructive_hint=tool.destructive_hint,
                    policy_override=overrides.get(tool.name),
                )
            )
        await self._session.commit()

    async def list_for_server(self, server_id: UUID) -> list[MCPToolRow]:
        result = await self._session.execute(
            select(MCPToolRow).where(MCPToolRow.server_id == server_id)
        )
        return list(result.scalars())

    async def set_policy_override(self, tool_id: UUID, policy_override: str | None) -> None:
        await self._session.execute(
            update(MCPToolRow)
            .where(MCPToolRow.id == tool_id)
            .values(policy_override=policy_override)
        )
        await self._session.commit()

    async def set_policy_override_for_server(
        self, server_id: UUID, policy_override: str | None
    ) -> None:
        """Bulk-set policy_override for every tool of a server."""
        await self._session.execute(
            update(MCPToolRow)
            .where(MCPToolRow.server_id == server_id)
            .values(policy_override=policy_override)
        )
        await self._session.commit()


class SettingsRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str) -> object | None:
        row = await self._session.get(SettingRow, key)
        return row.value if row is not None else None

    async def set(self, key: str, value: object) -> None:
        existing = await self._session.get(SettingRow, key)
        if existing is None:
            self._session.add(SettingRow(key=key, value=value))
        else:
            existing.value = value
        await self._session.commit()

    async def delete(self, key: str) -> None:
        row = await self._session.get(SettingRow, key)
        if row is not None:
            await self._session.delete(row)
            await self._session.commit()


class ActionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_pending(
        self,
        *,
        conversation_id: UUID | None,
        trigger_id: UUID | None,
        channel_kind: str,
        channel_ref: str,
        server_name: str,
        tool_name: str,
        tool_call_id: str | None,
        arguments_json: dict,
        run_state_json: dict,
        approval_item_json: dict,
        model: str,
    ) -> ActionRow:
        row = await self.create_pending_no_commit(
            conversation_id=conversation_id,
            trigger_id=trigger_id,
            channel_kind=channel_kind,
            channel_ref=channel_ref,
            server_name=server_name,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments_json=arguments_json,
            run_state_json=run_state_json,
            approval_item_json=approval_item_json,
            model=model,
        )
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def create_pending_no_commit(
        self,
        *,
        conversation_id: UUID | None,
        trigger_id: UUID | None,
        channel_kind: str,
        channel_ref: str,
        server_name: str,
        tool_name: str,
        tool_call_id: str | None,
        arguments_json: dict,
        run_state_json: dict,
        approval_item_json: dict,
        model: str,
    ) -> ActionRow:
        row = ActionRow(
            status="pending",
            decision=None,
            conversation_id=conversation_id,
            trigger_id=trigger_id,
            channel_kind=channel_kind,
            channel_ref=channel_ref,
            server_name=server_name,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments_json=arguments_json,
            run_state_json=run_state_json,
            approval_item_json=approval_item_json,
            model=model,
            created_at=_utcnow(),
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get(self, action_id: UUID) -> ActionRow | None:
        return await self._session.get(ActionRow, action_id)

    async def list_pending(self, *, limit: int = 100) -> list[ActionRow]:
        result = await self._session.execute(
            select(ActionRow)
            .where(ActionRow.status == "pending")
            .order_by(ActionRow.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars())

    async def list_recent(self, *, limit: int = 100) -> list[ActionRow]:
        result = await self._session.execute(
            select(ActionRow).order_by(ActionRow.created_at.desc()).limit(limit)
        )
        return list(result.scalars())

    async def list_for_inbox(self, *, limit: int = 100) -> list[ActionRow]:
        result = await self._session.execute(
            select(ActionRow)
            .order_by(
                case((ActionRow.status == "pending", 0), else_=1),
                ActionRow.created_at.desc(),
            )
            .limit(limit)
        )
        return list(result.scalars())

    async def mark_running(
        self,
        action_id: UUID,
        *,
        decision: str,
        decision_reason: str | None,
    ) -> None:
        result = await self._session.execute(
            update(ActionRow)
            .where(ActionRow.id == action_id, ActionRow.status == "pending")
            .values(
                status="running",
                decision=decision,
                decision_reason=decision_reason,
                decided_at=_utcnow(),
            )
        )
        if result.rowcount != 1:
            await self._session.rollback()
            raise ValueError(f"action {action_id} not found or not pending")
        await self._session.commit()

    async def mark_completed(self, action_id: UUID) -> None:
        result = await self._session.execute(
            update(ActionRow)
            .where(ActionRow.id == action_id, ActionRow.status == "running")
            .values(status="completed", completed_at=_utcnow(), error=None)
        )
        if result.rowcount != 1:
            await self._session.rollback()
            raise ValueError(f"action {action_id} not found or not running")
        await self._session.commit()

    async def mark_failed(self, action_id: UUID, error: str) -> None:
        result = await self._session.execute(
            update(ActionRow)
            .where(ActionRow.id == action_id, ActionRow.status == "running")
            .values(status="failed", completed_at=_utcnow(), error=error)
        )
        if result.rowcount != 1:
            await self._session.rollback()
            raise ValueError(f"action {action_id} not found or not running")
        await self._session.commit()


class NotificationRepo:
    """Outbound notification ledger: sent pings and the queue for the next digest."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_sent(
        self,
        *,
        priority: int,
        source: str,
        text: str,
        at: datetime,
    ) -> NotificationRow:
        row = NotificationRow(
            priority=priority, source=source, text=text, status="sent", created_at=at
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def enqueue(
        self,
        *,
        priority: int,
        source: str,
        text: str,
        at: datetime,
    ) -> NotificationRow:
        row = NotificationRow(
            priority=priority, source=source, text=text, status="queued", created_at=at
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def count_sent_since(self, cutoff: datetime) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(NotificationRow)
            .where(NotificationRow.status == "sent", NotificationRow.created_at >= cutoff)
        )
        return int(result.scalar_one())

    async def claim_queued(self, *, at: datetime) -> list[NotificationRow]:
        """Mark every queued notification digested and return them oldest-first."""
        result = await self._session.execute(
            select(NotificationRow)
            .where(NotificationRow.status == "queued")
            .order_by(NotificationRow.created_at)
        )
        rows = list(result.scalars())
        for row in rows:
            row.status = "digested"
            row.digested_at = at
        await self._session.commit()
        return rows


class AuthRepo:
    """Users, login codes, sessions, passkeys, and recovery codes.

    Codes and session tokens are stored as SHA-256 hashes: they are only ever
    compared, never read back, so no reversible encryption (Fernet is for the
    read-back secrets in ``jarvis/oauth/crypto.py``). Single-use secrets are
    consumed with one atomic compare-and-set UPDATE so concurrent redemption
    attempts can never both succeed.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- users ---------------------------------------------------------

    async def get_or_create_user(self, email: str) -> UserRow:
        """Fetch or create the user for `email`. Caller enforces the allow-list.

        The WebAuthn user_handle is random bytes fixed at creation and never
        rotated — rotating would orphan discoverable credentials — and never
        derived from the email (W3C WebAuthn §14.6.1 forbids PII in it).
        """
        result = await self._session.execute(select(UserRow).where(UserRow.email == email))
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing
        row = UserRow(
            email=email,
            user_handle=secrets.token_bytes(16),
            created_at=_utcnow(),
        )
        self._session.add(row)
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            result = await self._session.execute(select(UserRow).where(UserRow.email == email))
            return result.scalar_one()
        await self._session.refresh(row)
        return row

    async def get_user(self, user_id: UUID) -> UserRow | None:
        return await self._session.get(UserRow, user_id)

    # -- login codes ---------------------------------------------------

    async def create_auth_code(
        self,
        *,
        user_id: UUID,
        code_hash: str,
        nonce_hash: str | None,
        expires_at: datetime,
        ip: str | None = None,
    ) -> AuthCodeRow:
        row = AuthCodeRow(
            user_id=user_id,
            code_hash=code_hash,
            nonce_hash=nonce_hash,
            expires_at=expires_at,
            requested_ip=ip,
            requested_at=_utcnow(),
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def replace_auth_code(
        self,
        *,
        user_id: UUID,
        code_hash: str,
        nonce_hash: str | None,
        expires_at: datetime,
        ip: str | None = None,
    ) -> AuthCodeRow:
        """Invalidate the user's outstanding codes and issue a new one.

        The new row INHERITS the highest attempt count among the codes it
        replaces: re-requesting a code must never refill the guess budget,
        or an attacker could reset the 5-attempt lockout at will.
        """
        now = _utcnow()
        live = (
            AuthCodeRow.user_id == user_id,
            AuthCodeRow.consumed_at.is_(None),
            AuthCodeRow.expires_at > now,
        )
        carried = (
            await self._session.execute(select(func.max(AuthCodeRow.attempts)).where(*live))
        ).scalar_one_or_none() or 0
        await self._session.execute(update(AuthCodeRow).where(*live).values(consumed_at=now))
        row = AuthCodeRow(
            user_id=user_id,
            code_hash=code_hash,
            nonce_hash=nonce_hash,
            expires_at=expires_at,
            attempts=carried,
            requested_ip=ip,
            requested_at=now,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def record_code_attempt(self, nonce_hash: str) -> int | None:
        """+1 attempt on the live code bound to this nonce; returns the new count.

        None when no live code matches the nonce — a mismatched nonce never
        burns the real code's attempts (and can never redeem it either).
        """
        now = _utcnow()
        result = await self._session.execute(
            update(AuthCodeRow)
            .where(
                AuthCodeRow.nonce_hash == nonce_hash,
                AuthCodeRow.consumed_at.is_(None),
                AuthCodeRow.expires_at > now,
            )
            .values(attempts=AuthCodeRow.attempts + 1)
            .returning(AuthCodeRow.attempts)
        )
        attempts = result.scalar_one_or_none()
        await self._session.commit()
        return attempts

    async def consume_auth_code(
        self,
        code_hash: str,
        *,
        nonce_hash: str | None = None,
        max_attempts: int | None = None,
    ) -> UUID | None:
        """Redeem a login code exactly once; None if unknown, consumed, or expired.

        A single CAS UPDATE (not read-then-write) — under concurrent redemption
        exactly one caller gets the user_id back. The row's nonce_hash must
        equal `nonce_hash` exactly (None matches only nonce-less codes), and
        with `max_attempts` set the code is dead once attempts exceed it.
        """
        now = _utcnow()
        nonce_matches = (
            AuthCodeRow.nonce_hash.is_(None)
            if nonce_hash is None
            else AuthCodeRow.nonce_hash == nonce_hash
        )
        stmt = (
            update(AuthCodeRow)
            .where(
                AuthCodeRow.code_hash == code_hash,
                nonce_matches,
                AuthCodeRow.consumed_at.is_(None),
                AuthCodeRow.expires_at > now,
            )
            .values(consumed_at=now)
            .returning(AuthCodeRow.user_id)
        )
        if max_attempts is not None:
            stmt = stmt.where(AuthCodeRow.attempts <= max_attempts)
        user_id = (await self._session.execute(stmt)).scalar_one_or_none()
        await self._session.commit()
        return user_id

    # -- sessions ------------------------------------------------------

    async def create_session(
        self,
        *,
        user_id: UUID,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip: str | None = None,
    ) -> SessionRow:
        now = _utcnow()
        row = SessionRow(
            user_id=user_id,
            token_hash=token_hash,
            created_at=now,
            last_seen_at=now,
            expires_at=expires_at,
            last_auth_at=now,
            user_agent=user_agent,
            ip=ip,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def get_session_by_token_hash(self, token_hash: str) -> SessionRow | None:
        result = await self._session.execute(
            select(SessionRow).where(SessionRow.token_hash == token_hash)
        )
        return result.scalar_one_or_none()

    async def rotate_session_token(self, token_hash: str, *, new_token_hash: str) -> bool:
        """Swap a live session's token hash (rotation at an authentication event).

        Same CAS shape as consume_auth_code: matching on the old hash of a
        non-revoked session makes concurrent rotations yield one winner.
        """
        now = _utcnow()
        result = await self._session.execute(
            update(SessionRow)
            .where(
                SessionRow.token_hash == token_hash,
                SessionRow.revoked_at.is_(None),
                SessionRow.expires_at > now,
            )
            .values(token_hash=new_token_hash, last_auth_at=now)
            .returning(SessionRow.id)
        )
        rotated = result.scalar_one_or_none() is not None
        await self._session.commit()
        return rotated

    async def touch_session(self, session_id: UUID) -> None:
        await self._session.execute(
            update(SessionRow).where(SessionRow.id == session_id).values(last_seen_at=_utcnow())
        )
        await self._session.commit()

    async def revoke_session(self, session_id: UUID) -> None:
        await self._session.execute(
            update(SessionRow)
            .where(SessionRow.id == session_id, SessionRow.revoked_at.is_(None))
            .values(revoked_at=_utcnow())
        )
        await self._session.commit()

    async def revoke_all_sessions_for_user(self, user_id: UUID) -> int:
        result = await self._session.execute(
            update(SessionRow)
            .where(SessionRow.user_id == user_id, SessionRow.revoked_at.is_(None))
            .values(revoked_at=_utcnow())
        )
        await self._session.commit()
        return result.rowcount or 0

    async def delete_expired_sessions_and_codes(self) -> int:
        """Purge hard-expired sessions and dead (expired or consumed) login
        codes and WebAuthn challenges."""
        now = _utcnow()
        sessions = await self._session.execute(
            delete(SessionRow).where(SessionRow.expires_at <= now)
        )
        codes = await self._session.execute(
            delete(AuthCodeRow).where(
                (AuthCodeRow.expires_at <= now) | (AuthCodeRow.consumed_at.is_not(None))
            )
        )
        challenges = await self._session.execute(
            delete(WebAuthnChallengeRow).where(
                (WebAuthnChallengeRow.expires_at <= now)
                | (WebAuthnChallengeRow.consumed_at.is_not(None))
            )
        )
        await self._session.commit()
        return (sessions.rowcount or 0) + (codes.rowcount or 0) + (challenges.rowcount or 0)

    # -- webauthn credentials ------------------------------------------

    async def add_credential(
        self,
        *,
        credential_id: str,
        user_id: UUID,
        public_key: bytes,
        sign_count: int = 0,
        transports: list | None = None,
        aaguid: str | None = None,
        backup_eligible: bool = False,
        backup_state: bool = False,
        name: str | None = None,
    ) -> WebAuthnCredentialRow:
        row = WebAuthnCredentialRow(
            credential_id=credential_id,
            user_id=user_id,
            public_key=public_key,
            sign_count=sign_count,
            transports=transports,
            aaguid=aaguid,
            backup_eligible=backup_eligible,
            backup_state=backup_state,
            name=name,
            created_at=_utcnow(),
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def get_credential(self, credential_id: str) -> WebAuthnCredentialRow | None:
        return await self._session.get(WebAuthnCredentialRow, credential_id)

    async def list_credentials_for_user(self, user_id: UUID) -> list[WebAuthnCredentialRow]:
        result = await self._session.execute(
            select(WebAuthnCredentialRow)
            .where(WebAuthnCredentialRow.user_id == user_id)
            .order_by(WebAuthnCredentialRow.created_at.asc())
        )
        return list(result.scalars())

    async def record_credential_use(
        self,
        credential_id: str,
        *,
        sign_count: int,
        backup_state: bool | None = None,
    ) -> None:
        values: dict = {"sign_count": sign_count, "last_used_at": _utcnow()}
        if backup_state is not None:
            values["backup_state"] = backup_state
        await self._session.execute(
            update(WebAuthnCredentialRow)
            .where(WebAuthnCredentialRow.credential_id == credential_id)
            .values(**values)
        )
        await self._session.commit()

    async def rename_credential(self, credential_id: str, name: str) -> None:
        await self._session.execute(
            update(WebAuthnCredentialRow)
            .where(WebAuthnCredentialRow.credential_id == credential_id)
            .values(name=name)
        )
        await self._session.commit()

    async def delete_credential(self, credential_id: str) -> None:
        row = await self._session.get(WebAuthnCredentialRow, credential_id)
        if row is not None:
            await self._session.delete(row)
            await self._session.commit()

    # -- recovery codes ------------------------------------------------

    async def create_recovery_codes(
        self, user_id: UUID, code_hashes: list[str]
    ) -> list[RecoveryCodeRow]:
        rows = [RecoveryCodeRow(user_id=user_id, code_hash=code_hash) for code_hash in code_hashes]
        self._session.add_all(rows)
        await self._session.commit()
        for row in rows:
            await self._session.refresh(row)
        return rows

    async def consume_recovery_code(self, user_id: UUID, code_hash: str) -> bool:
        """Redeem a recovery code exactly once (same atomic CAS as login codes)."""
        result = await self._session.execute(
            update(RecoveryCodeRow)
            .where(
                RecoveryCodeRow.user_id == user_id,
                RecoveryCodeRow.code_hash == code_hash,
                RecoveryCodeRow.consumed_at.is_(None),
            )
            .values(consumed_at=_utcnow())
            .returning(RecoveryCodeRow.id)
        )
        consumed = result.scalar_one_or_none() is not None
        await self._session.commit()
        return consumed

    async def has_recovery_codes(self, user_id: UUID) -> bool:
        result = await self._session.execute(
            select(RecoveryCodeRow.id).where(RecoveryCodeRow.user_id == user_id).limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def has_credentials(self, user_id: UUID) -> bool:
        result = await self._session.execute(
            select(WebAuthnCredentialRow.credential_id)
            .where(WebAuthnCredentialRow.user_id == user_id)
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    # -- webauthn challenges -------------------------------------------

    async def create_challenge(
        self,
        *,
        kind: str,
        challenge: bytes,
        expires_at: datetime,
        user_id: UUID | None = None,
    ) -> WebAuthnChallengeRow:
        row = WebAuthnChallengeRow(
            kind=kind,
            user_id=user_id,
            challenge=challenge,
            created_at=_utcnow(),
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def consume_challenge(
        self, challenge_id: UUID, *, kind: str
    ) -> tuple[bytes, UUID | None] | None:
        """Redeem a ceremony challenge exactly once; None if unknown, wrong
        kind, consumed, or expired.

        Returns (challenge bytes, bound user_id) — the server-stored value the
        signed clientDataJSON must be verified against; nothing the client
        echoes is trusted. Same atomic CAS shape as consume_auth_code.
        """
        result = await self._session.execute(
            update(WebAuthnChallengeRow)
            .where(
                WebAuthnChallengeRow.id == challenge_id,
                WebAuthnChallengeRow.kind == kind,
                WebAuthnChallengeRow.consumed_at.is_(None),
                WebAuthnChallengeRow.expires_at > _utcnow(),
            )
            .values(consumed_at=_utcnow())
            .returning(WebAuthnChallengeRow.challenge, WebAuthnChallengeRow.user_id)
        )
        row = result.one_or_none()
        await self._session.commit()
        return (row.challenge, row.user_id) if row is not None else None
