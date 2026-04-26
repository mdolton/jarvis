"""AgentRunner — wraps OpenAI Agents SDK Runner with our persistence.

Responsibilities:
  1. Convert an `InvocationRequest` into a conversation + user message row.
  2. Build system prompt + hand off to `agents.Runner.run`.
  3. Persist assistant output as a message row.
  4. Emit audit events at trigger boundaries (the SDK tracing handles the
     intra-run LLM/tool events via the tracer bridge).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from agents import Agent, RunConfig, Runner
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
from jarvis.persistence.repositories import (
    ConversationRepo,
    MessageRepo,
    TriggerRepo,
)

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class AgentRunResult:
    final_output: str
    conversation_id: UUID
    trigger_id: UUID
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
        model: Any = None,  # Override for tests; None means "use config.model"
        idle_timeout_sec: int = 900,
    ) -> None:
        self._session_factory = session_factory
        self._audit = audit
        self._mcp_servers_provider = mcp_servers_provider
        self._llm_config = llm_config
        self._model = model
        self._idle_timeout_sec = idle_timeout_sec

    async def run(self, request: InvocationRequest) -> AgentRunResult:
        channel_kind, channel_ref, prompt = _extract_from_trigger(request)
        trigger_kind = request.trigger.kind

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
                content=prompt,
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

        # Build the SDK agent. Resolve mcp_servers fresh per run so OAuth
        # servers connected after bootstrap are visible to new agent invocations.
        agent_kwargs: dict[str, Any] = {
            "name": "jarvis",
            "instructions": _system_prompt(),
            "mcp_servers": self._mcp_servers_provider(),
        }
        if self._model is not None:
            agent_kwargs["model"] = self._model
        else:
            agent_kwargs["model"] = self._llm_config.model
        agent = Agent(**agent_kwargs)

        sdk_result = await Runner.run(
            agent,
            prompt,
            run_config=RunConfig(workflow_name="jarvis-invoke"),
        )

        final_text = _extract_text(sdk_result)

        # Persist assistant message.
        async with self._session_factory() as session:
            await MessageRepo(session).append(
                conversation_id=conv_id,
                role=MessageRole.ASSISTANT,
                content=final_text,
            )

        return AgentRunResult(
            final_output=final_text,
            conversation_id=conv_id,
            trigger_id=trigger_id,
            channel_kind=channel_kind,
            channel_ref=channel_ref,
        )


def _extract_from_trigger(request: InvocationRequest):
    t = request.trigger
    if isinstance(t, ChannelMessage):
        return t.channel_kind, t.channel_ref, t.text
    if isinstance(t, ScheduledTrigger):
        return ChannelKind.SCHEDULED, t.schedule_id, t.prompt
    if isinstance(t, ManualTrigger):
        return ChannelKind.DASHBOARD, t.user, t.prompt
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


def _system_prompt() -> str:
    return (
        "You are Jarvis, a helpful personal assistant. "
        "Use the available MCP tools when they help answer the user. "
        "Be concise."
    )


def _extract_text(sdk_result) -> str:
    """Best-effort extraction of the final assistant text from SDK RunResult."""
    if hasattr(sdk_result, "final_output") and sdk_result.final_output is not None:
        return str(sdk_result.final_output)
    return ""
