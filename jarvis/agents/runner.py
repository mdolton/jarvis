"""AgentRunner — wraps OpenAI Agents SDK Runner with our persistence.

Responsibilities:
  1. Convert an `InvocationRequest` into a conversation + user message row.
  2. Build system prompt + hand off to `agents.Runner.run`.
  3. Persist assistant output as a message row.
  4. Emit audit events at trigger boundaries (the SDK tracing handles the
     intra-run LLM/tool events via the tracer bridge).
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agents import RunConfig, Runner
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.actions.serialization import approval_item_to_json, run_state_to_json
from jarvis.agents.factory import build_agent
from jarvis.agents.factory import resolve_model as resolve_model
from jarvis.audit.logger import AuditLogger
from jarvis.config.schema import LLMConfig
from jarvis.core.types import (
    AuditEvent,
    AuditEventType,
    ChannelKind,
    ChannelMessage,
    InvocationRequest,
    ManualTrigger,
    MessageRole,
    ScheduledTrigger,
)
from jarvis.memory.prompt import assemble_memory_prompt
from jarvis.memory.types import MemoryContext
from jarvis.persistence.repositories import (
    ActionRepo,
    ConversationRepo,
    MessageRepo,
    TriggerRepo,
)

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentRunResult:
    final_output: str
    conversation_id: UUID | None
    trigger_id: UUID | None
    channel_kind: ChannelKind
    channel_ref: str


class AgentRunner:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        audit: AuditLogger,
        mcp_servers_provider: Callable[[], list],
        llm_config: LLMConfig,
        mcp_context_provider: Callable[[], str] | None = None,
        model: Any = None,  # Override for tests; None means "use config.model"
        model_provider: Callable[[], str] | None = None,
        idle_timeout_sec: int = 900,
        run_timeout_sec: float | None = None,
        memory_service: Any = None,
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit
        self._mcp_servers_provider = mcp_servers_provider
        self._mcp_context_provider = mcp_context_provider
        self._llm_config = llm_config
        self._model = model
        self._model_provider = model_provider
        self._idle_timeout_sec = idle_timeout_sec
        self._run_timeout_sec = run_timeout_sec
        self._memory_service = memory_service
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def run(self, request: InvocationRequest) -> AgentRunResult:
        channel_kind, channel_ref, user_prompt, trigger_context = _extract_from_trigger(request)
        trigger_kind = request.trigger.kind
        stored_user_prompt = _assemble_trigger_prompt(
            trigger_context=trigger_context,
            user_prompt=user_prompt,
        )

        async with self._session_factory() as session:
            # Record the trigger.
            trig = await TriggerRepo(session).record(
                kind=trigger_kind.value,
                source_ref=_trigger_source_ref(request),
            )
            trigger_id = trig.id

            # Find-or-create the conversation.
            conv = await ConversationRepo(session).find_or_create_open(
                channel_kind=channel_kind,
                channel_ref=channel_ref,
                idle_timeout_sec=_idle_for_kind(channel_kind, self._idle_timeout_sec),
            )
            conv_id = conv.id

            # Persist user message.
            await MessageRepo(session).append(
                conversation_id=conv_id,
                role=MessageRole.USER,
                content=stored_user_prompt,
            )

        prompt = await self._build_prompt_with_memory(
            conversation_id=conv_id,
            trigger_id=trigger_id,
            trigger_context=trigger_context,
            user_prompt=user_prompt,
        )

        await self._audit.emit(
            AuditEvent(
                type=AuditEventType.TRIGGER_RECEIVED,
                conversation_id=conv_id,
                trigger_id=trigger_id,
                payload={
                    "trigger_kind": trigger_kind.value,
                    "channel_kind": channel_kind.value,
                    "channel_ref": channel_ref,
                },
            )
        )

        agent, resolved_model = build_agent(
            llm_config=self._llm_config,
            mcp_servers_provider=self._mcp_servers_provider,
            trigger=request.trigger,
            explicit_model=self._model,
            model_provider=self._model_provider,
        )

        if self._run_timeout_sec is None:
            sdk_result = await Runner.run(
                agent,
                prompt,
                run_config=RunConfig(workflow_name="jarvis-invoke"),
            )
        else:
            async with asyncio.timeout(self._run_timeout_sec):
                sdk_result = await Runner.run(
                    agent,
                    prompt,
                    run_config=RunConfig(workflow_name="jarvis-invoke"),
                )

        interruptions = list(getattr(sdk_result, "interruptions", []) or [])
        if interruptions:
            approval_payload = approval_item_to_json(interruptions[0])
            to_state = getattr(sdk_result, "to_state", None)
            run_state = to_state() if callable(to_state) else None
            if run_state is None:
                run_state = getattr(sdk_result, "state", None) or getattr(
                    sdk_result, "_run_state", None
                )
            if run_state is None:
                raise RuntimeError("approval interruption did not include a serializable run state")

            async with self._session_factory() as session:
                async with session.begin():
                    action = await ActionRepo(session).create_pending_no_commit(
                        conversation_id=conv_id,
                        trigger_id=trigger_id,
                        channel_kind=channel_kind.value,
                        channel_ref=channel_ref,
                        server_name=approval_payload["server_name"],
                        tool_name=approval_payload["tool_name"],
                        tool_call_id=approval_payload["tool_call_id"],
                        arguments_json=approval_payload["arguments_json"],
                        run_state_json=run_state_to_json(run_state),
                        approval_item_json=approval_payload,
                        model=resolved_model,
                    )
                    final_text = (
                        f"Action approval required: {action.server_name}.{action.tool_name} "
                        f"({action.id}). Review it in the Action Inbox."
                    )
                    await MessageRepo(session).append_no_commit(
                        conversation_id=conv_id,
                        role=MessageRole.ASSISTANT,
                        content=final_text,
                    )
            await self._audit.emit(
                AuditEvent(
                    type=AuditEventType.ACTION_CREATED,
                    conversation_id=conv_id,
                    trigger_id=trigger_id,
                    payload={
                        "action_id": str(action.id),
                        "server_name": action.server_name,
                        "tool_name": action.tool_name,
                    },
                )
            )

            return AgentRunResult(
                final_output=final_text,
                conversation_id=conv_id,
                trigger_id=trigger_id,
                channel_kind=channel_kind,
                channel_ref=channel_ref,
            )

        final_text = _extract_text(sdk_result)

        # Persist assistant message.
        async with self._session_factory() as session:
            await MessageRepo(session).append(
                conversation_id=conv_id,
                role=MessageRole.ASSISTANT,
                content=final_text,
            )

        self._schedule_memory_summary(
            conversation_id=conv_id,
            channel_kind=channel_kind.value,
            channel_ref=channel_ref,
            user_prompt=user_prompt,
            assistant_output=final_text,
        )

        return AgentRunResult(
            final_output=final_text,
            conversation_id=conv_id,
            trigger_id=trigger_id,
            channel_kind=channel_kind,
            channel_ref=channel_ref,
        )

    async def _build_prompt_with_memory(
        self,
        *,
        conversation_id: UUID,
        trigger_id: UUID,
        trigger_context: str,
        user_prompt: str,
    ) -> str:
        if self._memory_service is None:
            return assemble_memory_prompt(
                memory_context=_empty_memory_context(),
                runtime_context=self._build_runtime_context(),
                trigger_context=trigger_context,
                current_prompt=user_prompt,
            )

        try:
            memory_context = await self._memory_service.build_context(
                conversation_id=conversation_id,
                trigger_id=trigger_id,
                prompt=user_prompt,
            )
        except Exception:
            _log.exception("memory recall failed")
            memory_context = _empty_memory_context()

        return assemble_memory_prompt(
            memory_context=memory_context,
            runtime_context=self._build_runtime_context(),
            trigger_context=trigger_context,
            current_prompt=user_prompt,
        )

    def _build_runtime_context(self) -> str:
        if self._mcp_context_provider is None:
            return ""
        return self._mcp_context_provider()

    def _schedule_memory_summary(
        self,
        *,
        conversation_id: UUID,
        channel_kind: str,
        channel_ref: str,
        user_prompt: str,
        assistant_output: str,
    ) -> None:
        if self._memory_service is None:
            return

        async def _run() -> None:
            try:
                await self._memory_service.summarize_run(
                    conversation_id=conversation_id,
                    channel_kind=channel_kind,
                    channel_ref=channel_ref,
                    user_prompt=user_prompt,
                    assistant_output=assistant_output,
                )
            except Exception:
                _log.exception("memory summarization failed")

        task = asyncio.create_task(_run(), name="memory-summary")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def drain_memory_tasks(self) -> None:
        while self._background_tasks:
            pending = tuple(self._background_tasks)
            await asyncio.gather(*pending, return_exceptions=True)

    async def shutdown(self) -> None:
        await self.drain_memory_tasks()


def _extract_from_trigger(request: InvocationRequest):
    t = request.trigger
    if isinstance(t, ChannelMessage):
        return t.channel_kind, t.channel_ref, t.text, ""
    if isinstance(t, ScheduledTrigger):
        return ChannelKind.SCHEDULED, t.schedule_id, t.prompt, _scheduled_context(t)
    if isinstance(t, ManualTrigger):
        return ChannelKind.DASHBOARD, t.user, t.prompt, ""
    raise ValueError(f"unknown trigger: {t!r}")


def _trigger_source_ref(request: InvocationRequest) -> str:
    t = request.trigger
    if isinstance(t, ChannelMessage):
        return t.external_id
    if isinstance(t, ScheduledTrigger):
        return t.schedule_id
    if isinstance(t, ManualTrigger):
        return t.user
    return "unknown"


def _idle_for_kind(kind: ChannelKind, default_sec: int) -> int:
    """Scheduled triggers always get a fresh conversation (spec §5.2)."""
    if kind == ChannelKind.SCHEDULED:
        return 0
    return default_sec


def _scheduled_context(trigger: ScheduledTrigger) -> str:
    if trigger.timezone is None or trigger.fired_at is None:
        return ""

    try:
        zone = ZoneInfo(trigger.timezone)
    except ZoneInfoNotFoundError:
        return ""

    fired_at_utc = trigger.fired_at
    if fired_at_utc.tzinfo is None:
        fired_at_utc = fired_at_utc.replace(tzinfo=UTC)
    local_time = fired_at_utc.astimezone(zone)
    return (
        "Schedule context:\n"
        f"- Timezone: {trigger.timezone}\n"
        f"- Local date: {local_time:%Y-%m-%d}\n"
        f"- Local time: {local_time:%Y-%m-%d %H:%M %Z}\n"
        "- Interpret relative dates like today, tomorrow, and yesterday in this timezone."
    )


def _extract_text(sdk_result) -> str:
    """Best-effort extraction of the final assistant text from SDK RunResult."""
    if hasattr(sdk_result, "final_output") and sdk_result.final_output is not None:
        return str(sdk_result.final_output)
    return ""


def _assemble_trigger_prompt(*, trigger_context: str, user_prompt: str) -> str:
    return assemble_memory_prompt(
        memory_context=_empty_memory_context(),
        trigger_context=trigger_context,
        current_prompt=user_prompt,
    )


def _empty_memory_context() -> MemoryContext:
    return MemoryContext(
        preferences=[],
        recalled=[],
        recall_available=False,
        error=None,
    )
