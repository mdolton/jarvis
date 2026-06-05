"""Repositories — the only way core modules touch the database."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jarvis.core.types import AuditEvent, AuditEventType, ChannelKind, MessageRole
from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.persistence.models import (
    ActionRow,
    AuditEventRow,
    ConversationRow,
    DigestTemplateRow,
    MCPServerRow,
    MCPToolRow,
    MessageRow,
    ScheduleRow,
    SettingRow,
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

    async def upsert(self, *, name: str, transport: str) -> MCPServerRow:
        result = await self._session.execute(select(MCPServerRow).where(MCPServerRow.name == name))
        existing = result.scalar_one_or_none()
        if existing:
            existing.transport = transport
            await self._session.commit()
            await self._session.refresh(existing)
            return existing

        row = MCPServerRow(name=name, transport=transport, status="disconnected")
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
