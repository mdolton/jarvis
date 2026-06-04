from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from agents import RunConfig, Runner
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.actions.serialization import run_state_from_json
from jarvis.agents.factory import build_agent
from jarvis.agents.runner import AgentRunResult
from jarvis.audit.logger import AuditLogger
from jarvis.config.schema import LLMConfig
from jarvis.core.output_router import OutputRouter
from jarvis.core.types import AuditEvent, AuditEventType, ChannelKind, MessageRole
from jarvis.persistence.models import ActionRow
from jarvis.persistence.repositories import ActionRepo, MessageRepo


class ActionService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        audit: AuditLogger,
        output_router: OutputRouter | None,
        llm_config: LLMConfig,
        mcp_servers_provider: Callable[[], list],
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit
        self._output_router = output_router
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
                    payload={
                        "action_id": str(action.id),
                        "server_name": action.server_name,
                        "tool_name": action.tool_name,
                    },
                )
            )

            sdk_result = await Runner.run(
                agent,
                run_state,
                run_config=RunConfig(workflow_name="jarvis-action-resume"),
            )
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

            if self._output_router is not None:
                await self._output_router.route(result)

            async with self._session_factory() as session:
                await ActionRepo(session).mark_completed(action_id)

            await self._audit.emit(
                AuditEvent(
                    type=AuditEventType.ACTION_COMPLETED,
                    conversation_id=action.conversation_id,
                    trigger_id=action.trigger_id,
                    payload={"action_id": str(action.id)},
                )
            )

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
                payload={"action_id": str(action.id), "error": str(exc)},
            )
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
    return interruptions[0]


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
