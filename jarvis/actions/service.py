from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

from agents import RunConfig, Runner
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.actions.serialization import (
    approval_item_to_json,
    run_state_from_json,
    run_state_to_json,
)
from jarvis.agents.factory import build_agent
from jarvis.agents.runner import AgentRunResult
from jarvis.audit.logger import AuditLogger
from jarvis.config.schema import LLMConfig
from jarvis.core.output_router import OutputRouter
from jarvis.core.types import AuditEvent, AuditEventType, ChannelKind, MessageRole
from jarvis.persistence.models import ActionRow
from jarvis.persistence.repositories import ActionRepo, MessageRepo, ScheduleRepo
from jarvis.scheduler.scheduled_output import ScheduledOutputRouter

_log = logging.getLogger(__name__)


class ActionService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        audit: AuditLogger,
        output_router: OutputRouter | None,
        llm_config: LLMConfig,
        mcp_servers_provider: Callable[[], list],
        scheduled_output_router: ScheduledOutputRouter | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit
        self._output_router = output_router
        self._scheduled_output_router = scheduled_output_router
        self._llm_config = llm_config
        self._mcp_servers_provider = mcp_servers_provider

    async def approve(self, action_id: UUID) -> AgentRunResult:
        return await self._decide(action_id, decision="approved", reason=None)

    async def reject(self, action_id: UUID, *, reason: str | None = None) -> AgentRunResult:
        return await self._decide(
            action_id,
            decision="rejected",
            reason=reason or "Tool execution was not approved.",
        )

    async def _decide(
        self,
        action_id: UUID,
        *,
        decision: str,
        reason: str | None,
    ) -> AgentRunResult:
        async with self._session_factory() as session:
            repo = ActionRepo(session)
            action = await repo.get(action_id)
            if action is None:
                raise ValueError(f"action {action_id} not found")
            await repo.mark_running(action_id, decision=decision, decision_reason=reason)

        try:
            agent, _ = build_agent(
                llm_config=self._llm_config,
                mcp_servers_provider=self._mcp_servers_provider,
                model_override=action.model,
            )
            run_state = await run_state_from_json(agent, action.run_state_json)
            approval_item = _approval_interruption_for_action(run_state, action)
            event_type = (
                AuditEventType.ACTION_APPROVED
                if decision == "approved"
                else AuditEventType.ACTION_REJECTED
            )

            if decision == "approved":
                run_state.approve(approval_item)
            else:
                run_state.reject(approval_item, rejection_message=reason)

            await self._audit.emit(
                AuditEvent(
                    type=event_type,
                    conversation_id=action.conversation_id,
                    trigger_id=action.trigger_id,
                    payload=_action_payload(action),
                )
            )

            sdk_result = await Runner.run(
                agent,
                run_state,
                run_config=RunConfig(workflow_name="jarvis-action-resume"),
            )
            interruptions = list(getattr(sdk_result, "interruptions", []) or [])
            if interruptions:
                return await self._create_followup_action(action, sdk_result, interruptions[0])

            final_text = _extract_text(sdk_result)
            result = AgentRunResult(
                final_output=final_text,
                conversation_id=action.conversation_id,
                trigger_id=action.trigger_id,
                channel_kind=ChannelKind(action.channel_kind),
                channel_ref=action.channel_ref,
            )

            async with self._session_factory() as session:
                if action.conversation_id is not None:
                    await MessageRepo(session).append(
                        conversation_id=action.conversation_id,
                        role=MessageRole.ASSISTANT,
                        content=final_text,
                    )

            async with self._session_factory() as session:
                await ActionRepo(session).mark_completed(action_id)

            await self._audit.emit(
                AuditEvent(
                    type=AuditEventType.ACTION_COMPLETED,
                    conversation_id=action.conversation_id,
                    trigger_id=action.trigger_id,
                    payload=_action_payload(action),
                )
            )

            await self._route_result(result)
            return result
        except Exception as exc:
            await self._fail_action(action, exc)
            raise

    async def _fail_action(self, action: ActionRow, exc: Exception) -> None:
        error = f"{type(exc).__name__}: {exc}"
        async with self._session_factory() as session:
            await ActionRepo(session).mark_failed(action.id, error)
        await self._audit.emit(
            AuditEvent(
                type=AuditEventType.ACTION_FAILED,
                conversation_id=action.conversation_id,
                trigger_id=action.trigger_id,
                payload=_action_payload(action, error=str(exc)),
            )
        )
        result = AgentRunResult(
            final_output=_failure_notice(action),
            conversation_id=action.conversation_id,
            trigger_id=action.trigger_id,
            channel_kind=ChannelKind(action.channel_kind),
            channel_ref=action.channel_ref,
        )
        await self._route_result(result)

    async def _create_followup_action(
        self,
        action: ActionRow,
        sdk_result: Any,
        approval_item: Any,
    ) -> AgentRunResult:
        to_state = getattr(sdk_result, "to_state", None)
        run_state = to_state() if callable(to_state) else None
        if run_state is None:
            run_state = getattr(sdk_result, "state", None) or getattr(
                sdk_result, "_run_state", None
            )
        if run_state is None:
            raise RuntimeError("approval interruption did not include a serializable run state")

        approval_payload = approval_item_to_json(approval_item)
        async with self._session_factory() as session:
            async with session.begin():
                next_action = await ActionRepo(session).create_pending_no_commit(
                    conversation_id=action.conversation_id,
                    trigger_id=action.trigger_id,
                    channel_kind=action.channel_kind,
                    channel_ref=action.channel_ref,
                    server_name=approval_payload["server_name"],
                    tool_name=approval_payload["tool_name"],
                    tool_call_id=approval_payload["tool_call_id"],
                    arguments_json=approval_payload["arguments_json"],
                    run_state_json=run_state_to_json(run_state),
                    approval_item_json=approval_payload,
                    model=action.model,
                )
                notice = _approval_required_notice(next_action)
                if action.conversation_id is not None:
                    await MessageRepo(session).append_no_commit(
                        conversation_id=action.conversation_id,
                        role=MessageRole.ASSISTANT,
                        content=notice,
                    )

        await self._audit.emit(
            AuditEvent(
                type=AuditEventType.ACTION_CREATED,
                conversation_id=next_action.conversation_id,
                trigger_id=next_action.trigger_id,
                payload=_action_payload(next_action),
            )
        )

        result = AgentRunResult(
            final_output=notice,
            conversation_id=action.conversation_id,
            trigger_id=action.trigger_id,
            channel_kind=ChannelKind(action.channel_kind),
            channel_ref=action.channel_ref,
        )
        async with self._session_factory() as session:
            await ActionRepo(session).mark_completed(action.id)

        await self._audit.emit(
            AuditEvent(
                type=AuditEventType.ACTION_COMPLETED,
                conversation_id=action.conversation_id,
                trigger_id=action.trigger_id,
                payload=_action_payload(action),
            )
        )
        await self._route_result(result)
        return result

    async def _route_result(self, result: AgentRunResult) -> None:
        try:
            if result.channel_kind == ChannelKind.SCHEDULED:
                await self._route_scheduled_result(result)
            elif self._output_router is not None:
                await self._output_router.route(result)
        except Exception:
            _log.exception("failed to route action resume output")

    async def _route_scheduled_result(self, result: AgentRunResult) -> None:
        if self._scheduled_output_router is None:
            _log.warning("scheduled action resume has no scheduled output router")
            return
        schedule_id = UUID(result.channel_ref)
        async with self._session_factory() as session:
            row = await ScheduleRepo(session).get(schedule_id)
        if row is None:
            raise LookupError(f"schedule {schedule_id} not found for action resume output")
        await self._scheduled_output_router.route(
            result=result,
            output_mode=row.output_mode,
            discord_user_id=row.discord_user_id or "",
        )


def _approval_interruption_for_action(run_state: Any, action: ActionRow) -> Any:
    get_interruptions = getattr(run_state, "get_interruptions", None)
    interruptions = list(get_interruptions() if callable(get_interruptions) else [])
    if not interruptions:
        raise RuntimeError(f"action {action.id} has no approval interruptions in run state")

    if action.tool_call_id is None:
        return interruptions[0]

    for interruption in interruptions:
        if _tool_call_id(interruption) == action.tool_call_id:
            return interruption
    raise RuntimeError(
        f"action {action.id} has no approval interruption matching tool call {action.tool_call_id}"
    )


def _tool_call_id(approval_item: Any) -> str | None:
    raw = getattr(approval_item, "raw_item", approval_item)
    for candidate in (
        getattr(approval_item, "call_id", None),
        getattr(raw, "call_id", None),
        getattr(raw, "id", None),
    ):
        if candidate is not None:
            return str(candidate)
    if isinstance(raw, dict):
        candidate = raw.get("call_id") or raw.get("id")
        if candidate is not None:
            return str(candidate)
    return None


def _extract_text(sdk_result: Any) -> str:
    if hasattr(sdk_result, "final_output") and sdk_result.final_output is not None:
        return str(sdk_result.final_output)
    return ""


def _approval_required_notice(action: ActionRow) -> str:
    return (
        f"Action approval required: {action.server_name}.{action.tool_name} "
        f"({action.id}). Review it in the Action Inbox."
    )


def _failure_notice(action: ActionRow) -> str:
    return (
        f"Action failed: {action.server_name}.{action.tool_name} ({action.id}). "
        "Check the Action Inbox for details."
    )


def _action_payload(action: ActionRow, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action_id": str(action.id),
        "server_name": action.server_name,
        "tool_name": action.tool_name,
    }
    if action.conversation_id is not None:
        payload["conversation_id"] = str(action.conversation_id)
    if action.trigger_id is not None:
        payload["trigger_id"] = str(action.trigger_id)
    if action.tool_call_id is not None:
        payload["tool_call_id"] = action.tool_call_id
    payload.update(extra)
    return payload
