"""Repositories — the only way core modules touch the database."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.core.types import ChannelKind
from jarvis.persistence.models import (
    ConversationRow,
    MessageRow,
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
