"""Repositories — the only way core modules touch the database."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.core.types import AuditEvent, AuditEventType, ChannelKind
from jarvis.persistence.models import (
    AuditEventRow,
    ConversationRow,
    MessageRow,
    ScheduleRow,
    TriggerRow,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


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

    async def touch(self, conversation_id: UUID) -> None:
        await self._session.execute(
            update(ConversationRow)
            .where(ConversationRow.id == conversation_id)
            .values(last_activity_at=_utcnow())
        )
        await self._session.commit()


class MessageRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        conversation_id: UUID,
        role: str,
        content: str,
    ) -> MessageRow:
        msg = MessageRow(
            conversation_id=conversation_id,
            role=role,
            content=content,
            created_at=_utcnow(),
        )
        self._session.add(msg)
        await self._session.commit()
        await self._session.refresh(msg)
        return msg

    async def history(self, conversation_id: UUID) -> list[MessageRow]:
        result = await self._session.execute(
            select(MessageRow)
            .where(MessageRow.conversation_id == conversation_id)
            .order_by(MessageRow.created_at.asc())
        )
        return list(result.scalars())


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
    ) -> ScheduleRow:
        now = _utcnow()
        row = ScheduleRow(
            name=name,
            description=description,
            cron_expr=cron_expr,
            timezone=timezone,
            prompt=prompt,
            output_mode=output_mode,
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
