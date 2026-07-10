"""OutputRouter — sends agent results to the originating channel.

The router holds a list of channel adapters and dispatches by channel_kind.
Dashboard / manual triggers (where the CLI or web app prints the reply
itself) and scheduled triggers (which have their own output routing in
Plan 4) are explicitly no-ops.

A Discord-kind result with no Discord adapter wired is a misconfiguration:
the run produced output that the user expected to receive, and silently
dropping it is worse than raising. We surface a LookupError.

Unsolicited notifications (event-triggered runs, noteworthy scheduled runs)
go through the NotificationGate defined here: a priority classifier plus a
persisted daily rate-limiter. P1 always interrupts; P4 always waits for the
next digest; P2/P3 interrupt only while the day's budget lasts. Sub-threshold
notifications queue in the `notifications` table and ride along with the next
scheduled digest send (see ScheduledOutputRouter), so a burst of low-priority
events becomes one digest entry instead of a ping per event.
"""

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import IntEnum

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.agents.runner import AgentRunResult
from jarvis.audit.logger import AuditLogger
from jarvis.channels.base import ChannelAdapter, OutboundMessage
from jarvis.core.streaming import RunStream
from jarvis.core.types import (
    AuditEvent,
    AuditEventType,
    ChannelKind,
    ChannelMessage,
    InvocationRequest,
)
from jarvis.persistence.repositories import NotificationRepo

_log = logging.getLogger(__name__)

# Channel kinds whose results don't go through an adapter.
_INTERNAL_KINDS: frozenset[ChannelKind] = frozenset(
    {
        ChannelKind.DASHBOARD,
        ChannelKind.SCHEDULED,  # scheduled runs route per their own output_mode (Plan 4)
        ChannelKind.EVENT,  # gated proactive delivery; dashboard-only when no gate is wired
    }
)


class Priority(IntEnum):
    """Notification priority tiers. Lower number = more urgent."""

    P1 = 1  # interrupt now, regardless of budget
    P2 = 2  # interrupt while the daily budget lasts
    P3 = 3  # interrupt while the daily budget lasts (default for unmarked text)
    P4 = 4  # never interrupt; deliver in the next digest


def classify_priority(text: str) -> tuple[Priority | None, str]:
    """Parse a leading priority marker; return (priority, text without marker).

    `[P1]`..`[P4]` set the tier explicitly; `[SILENT]` returns (None, "")
    meaning drop entirely (matching the scheduled-output convention). Unmarked
    text defaults to P3 so a classifier-less producer still respects the budget.
    """
    stripped = text.lstrip()
    upper = stripped.upper()
    if upper.startswith("[SILENT]"):
        return None, ""
    for priority in Priority:
        marker = f"[{priority.name}]"
        if upper.startswith(marker):
            return priority, stripped[len(marker) :].lstrip()
    return Priority.P3, text


def _day_start(now: datetime) -> datetime:
    """Start of `now`'s UTC calendar day, timezone-aware."""
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


_DIGEST_ITEM_MAX_CHARS = 200

_TRACE_ACTION_MAX_CHARS = 300


def _trace_action(text: str) -> str:
    """Terse single-line 'did X' summary of a run's final output."""
    flat = " ".join(text.split())
    if len(flat) > _TRACE_ACTION_MAX_CHARS:
        return flat[: _TRACE_ACTION_MAX_CHARS - 1] + "…"
    return flat


async def emit_autonomy_trace(
    audit: AuditLogger | None,
    *,
    result: AgentRunResult,
    source: str,
    reason: str,
    delivery: str,
) -> None:
    """Record a 'did X because Y' audit trace for an autonomous run.

    `delivery` says where the run's output actually went: 'discord',
    'digest', 'suppressed', 'dashboard_only', or 'undelivered'. The audit
    SSE feed tails this table, so emitting here is what makes autonomous
    activity visible without spending notification budget.
    """
    if audit is None:
        return
    await audit.emit(
        AuditEvent(
            type=AuditEventType.AUTONOMY_TRACE,
            conversation_id=result.conversation_id,
            trigger_id=result.trigger_id,
            payload={
                "source": source,
                "reason": reason,
                "action": _trace_action(result.final_output),
                "delivery": delivery,
            },
        )
    )


class NotificationGate:
    """Priority classifier + rolling daily rate-limiter for unsolicited sends.

    The budget is persisted: every immediate send is a `notifications` row with
    status 'sent', and the day's spend is counted from the table (UTC calendar
    day), so it survives restarts and resets at the day boundary. P1 bypasses
    the limit but still records a row — urgent pings spend the allowance for
    later P2/P3 traffic, keeping the *total* pings/day near the budget.
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        daily_budget: int = 5,
    ) -> None:
        self._session_factory = session_factory
        self._daily_budget = daily_budget

    async def admit(self, *, text: str, source: str) -> str | None:
        """Decide whether `text` may interrupt the user right now.

        Returns the marker-stripped text to send immediately, or None if the
        notification was dropped ([SILENT]) or queued for the next digest.
        """
        priority, cleaned = classify_priority(text)
        if priority is None:
            return None

        now = datetime.now(UTC)
        async with self._session_factory() as session:
            repo = NotificationRepo(session)
            if priority is Priority.P1:
                await repo.record_sent(priority=priority, source=source, text=cleaned, at=now)
                return cleaned
            if priority is Priority.P4:
                await repo.enqueue(priority=priority, source=source, text=cleaned, at=now)
                return None
            sent_today = await repo.count_sent_since(_day_start(now))
            if sent_today < self._daily_budget:
                await repo.record_sent(priority=priority, source=source, text=cleaned, at=now)
                return cleaned
            _log.info(
                "daily notification budget spent (%d/%d); queuing %s notification for digest",
                sent_today,
                self._daily_budget,
                priority.name,
            )
            await repo.enqueue(priority=priority, source=source, text=cleaned, at=now)
            return None

    async def drain_digest_section(self) -> str | None:
        """Claim all queued notifications and format them as one digest section.

        Notifications are coalesced per source (a burst of 20 events from one
        source becomes a single line with a count), so the section stays
        digest-sized no matter how noisy the interim was. Returns None when
        nothing is queued.
        """
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            rows = await NotificationRepo(session).claim_queued(at=now)
        if not rows:
            return None

        by_source: dict[str, list] = {}
        for row in rows:  # oldest-first from the repo
            by_source.setdefault(row.source, []).append(row)

        lines = []
        for source, items in by_source.items():
            latest = items[-1].text.strip().replace("\n", " ")
            if len(latest) > _DIGEST_ITEM_MAX_CHARS:
                latest = latest[: _DIGEST_ITEM_MAX_CHARS - 1] + "…"
            count = f" ({len(items)} updates, latest)" if len(items) > 1 else ""
            lines.append(f"- {source}{count}: {latest}")
        return "While you were away:\n" + "\n".join(lines)


class OutputRouter:
    def __init__(
        self,
        *,
        adapters: Iterable[ChannelAdapter],
        notification_gate: NotificationGate | None = None,
        event_notify_ref: str | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self._by_kind: dict[str, ChannelAdapter] = {a.kind: a for a in adapters}
        self._gate = notification_gate
        self._event_notify_ref = event_notify_ref
        self._audit = audit

    async def open_stream(self, request: InvocationRequest) -> RunStream | None:
        """Open a live output stream for a channel-message run, if the
        originating adapter supports it. Never raises — a stream is an
        enhancement; the plain route() send is the guaranteed path."""
        trigger = request.trigger
        if not isinstance(trigger, ChannelMessage):
            return None
        adapter = self._by_kind.get(trigger.channel_kind.value)
        opener = getattr(adapter, "open_stream", None)
        if opener is None:
            return None
        try:
            return await opener(trigger.channel_ref)
        except Exception:
            _log.exception("open_stream failed; run continues without streaming")
            return None

    async def route(self, result: AgentRunResult) -> None:
        if result.channel_kind is ChannelKind.EVENT:
            await self._route_event(result)
            return
        if result.channel_kind in _INTERNAL_KINDS:
            return
        adapter = self._by_kind.get(result.channel_kind.value)
        if adapter is None:
            raise LookupError(
                f"no channel adapter registered for kind {result.channel_kind.value!r}"
            )
        await adapter.send(
            OutboundMessage(
                channel_kind=result.channel_kind,
                channel_ref=result.channel_ref,
                text=result.final_output,
            )
        )

    async def _route_event(self, result: AgentRunResult) -> None:
        """Proactive delivery for event-triggered runs, subject to the gate.

        Without a gate, a notify target, and a Discord adapter, event results
        stay dashboard-only (the pre-gate behavior). Every path emits an
        autonomy trace so the run is never silent.
        """
        source = f"event:{result.channel_ref}"
        reason = f"inbound '{result.channel_ref}' event"

        async def _trace(delivery: str) -> None:
            await emit_autonomy_trace(
                self._audit, result=result, source=source, reason=reason, delivery=delivery
            )

        adapter = self._by_kind.get(ChannelKind.DISCORD.value)
        if self._gate is None or self._event_notify_ref is None or adapter is None:
            await _trace("dashboard_only")
            return
        priority, _ = classify_priority(result.final_output)
        if priority is None:
            await _trace("suppressed")
            return
        text = await self._gate.admit(text=result.final_output, source=source)
        if text is None:
            await _trace("digest")
            return
        await adapter.send(
            OutboundMessage(
                channel_kind=ChannelKind.DISCORD,
                channel_ref=self._event_notify_ref,
                text=f"⚙️ [{source}] {text}",
            )
        )
        await _trace("discord")
