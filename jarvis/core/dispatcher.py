"""TriggerDispatcher — sole producer of InvocationRequest.

Cross-cutting policy lives here:
  - Allow-list enforcement for channel messages.
  - Dedup via bounded LRU on external_id (Discord gateway retry protection).
  - Concurrency gate (semaphore).

Every trigger path (Discord message, scheduled fire, manual) lands in
one of the `dispatch_*` methods and eventually calls `AgentRunner.run`.
"""

import asyncio
import logging
from collections import OrderedDict

from jarvis.agents.runner import AgentRunner, AgentRunResult
from jarvis.audit.logger import AuditLogger
from jarvis.core.output_router import OutputRouter
from jarvis.core.types import (
    ChannelMessage,
    EventTrigger,
    InvocationRequest,
    ManualTrigger,
    ScheduledTrigger,
)

_log = logging.getLogger(__name__)


class TriggerDispatcher:
    def __init__(
        self,
        *,
        runner: AgentRunner,
        audit: AuditLogger,
        output_router: OutputRouter | None = None,
        max_concurrent: int = 3,
        dedup_window: int = 256,
    ) -> None:
        self._runner = runner
        self._audit = audit
        self._output_router = output_router
        self._sem = asyncio.Semaphore(max_concurrent)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_cap = dedup_window

    async def dispatch_channel_message(
        self,
        msg: ChannelMessage,
        *,
        allowed_refs: set[str],
    ) -> AgentRunResult | None:
        """Dispatch a channel message. Returns None if rejected or a dup."""
        if msg.channel_ref not in allowed_refs:
            _log.info("rejected channel message from %r (not allow-listed)", msg.channel_ref)
            return None
        if not self.remember_if_new(msg.external_id):
            _log.debug("dedup: suppressing repeat of %r", msg.external_id)
            return None

        return await self._run(InvocationRequest(trigger=msg))

    async def dispatch_scheduled(self, trigger: ScheduledTrigger) -> AgentRunResult:
        return await self._run(InvocationRequest(trigger=trigger))

    async def dispatch_event(self, trigger: EventTrigger) -> AgentRunResult | None:
        """Dispatch an inbound external event. Returns None if it's a dup."""
        if not self.remember_if_new(trigger.external_id):
            _log.debug("dedup: suppressing repeat of event %r", trigger.external_id)
            return None

        return await self._run(InvocationRequest(trigger=trigger))

    async def dispatch_manual(
        self,
        *,
        user: str,
        prompt: str,
    ) -> AgentRunResult:
        return await self._run(
            InvocationRequest(trigger=ManualTrigger(user=user, prompt=prompt)),
        )

    async def _run(self, request: InvocationRequest) -> AgentRunResult:
        async with self._sem:
            result = await self._runner.run(request)
        if self._output_router is not None:
            await self._output_router.route(result)
        return result

    def remember_if_new(self, external_id: str) -> bool:
        """Record `external_id` in the dedup LRU; False if it was already seen.

        Public so upstream producers (the event coalescer) can dedup at intake
        against the same bounded LRU used by the dispatch paths.
        """
        if external_id in self._seen:
            return False
        self._seen[external_id] = None
        # Bounded LRU: trim from the oldest.
        while len(self._seen) > self._seen_cap:
            self._seen.popitem(last=False)
        return True
