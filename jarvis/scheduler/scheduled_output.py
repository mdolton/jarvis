"""ScheduledOutputRouter — per-schedule output_mode routing.

After a scheduled agent run completes, this router decides where the
result goes based on the schedule's `output_mode`:

  - "discord": send via the Discord adapter to a specific user. These are the
    digest vehicles: when a NotificationGate is wired, any queued sub-threshold
    notifications are drained and appended, so they ride along with an already
    scheduled message instead of costing extra pings.
  - "dashboard_only": no outbound message; result stays in DB for dashboard.
  - "discord_if_noteworthy": check for [NOTEWORTHY] or [SILENT] prefix.
    If noteworthy (or no prefix — fail-open), send to Discord; if [SILENT],
    suppress. These sends are unsolicited, so when a gate is wired they also
    pass through it: over-budget results queue for the next digest instead of
    pinging (a [P1] marker in the text still interrupts immediately).

Whatever the mode, every routed run also emits an `autonomy.trace` audit
event recording what ran and where its output went (no silent autonomy).
"""

import logging

from jarvis.agents.runner import AgentRunResult
from jarvis.audit.logger import AuditLogger
from jarvis.channels.base import ChannelAdapter, OutboundMessage
from jarvis.core.output_router import NotificationGate, emit_autonomy_trace
from jarvis.core.types import ChannelKind

_log = logging.getLogger(__name__)


class ScheduledOutputRouter:
    def __init__(
        self,
        *,
        discord_adapter: ChannelAdapter | None,
        notification_gate: NotificationGate | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self._discord = discord_adapter
        self._gate = notification_gate
        self._audit = audit

    async def route(
        self,
        *,
        result: AgentRunResult,
        output_mode: str,
        discord_user_id: str,
        source: str = "scheduled",
    ) -> None:
        delivery = await self._route(
            result=result,
            output_mode=output_mode,
            discord_user_id=discord_user_id,
            source=source,
        )
        # Scheduled runs are autonomous; every one leaves an audit trace
        # saying what ran and where its output went (no silent autonomy).
        await emit_autonomy_trace(
            self._audit,
            result=result,
            source=source,
            reason=f"scheduled run ({source})",
            delivery=delivery,
        )

    async def _route(
        self,
        *,
        result: AgentRunResult,
        output_mode: str,
        discord_user_id: str,
        source: str,
    ) -> str:
        """Route per output_mode; return the delivery outcome for the trace."""
        if output_mode == "dashboard_only":
            return "dashboard_only"

        if output_mode == "discord_if_noteworthy":
            text = result.final_output
            upper = text.lstrip().upper()
            if upper.startswith("[SILENT]"):
                return "suppressed"
            if upper.startswith("[NOTEWORTHY]"):
                text = text.lstrip()
                text = text[len("[NOTEWORTHY]") :].lstrip()
            if not self._deliverable(discord_user_id):
                await self._send_discord(text, discord_user_id)  # logs the warning
                return "undelivered"
            # Consult the gate only when the send can actually happen —
            # otherwise budget would be spent on a message that never leaves.
            if self._gate is not None:
                admitted = await self._gate.admit(text=text, source=source)
                if admitted is None:
                    return "digest"
                text = admitted
            await self._send_discord(f"⚙️ [{source}] {text}", discord_user_id)
            return "discord"

        if output_mode == "discord":
            text = result.final_output
            if not self._deliverable(discord_user_id):
                await self._send_discord(text, discord_user_id)  # logs the warning
                return "undelivered"
            # Drain only when the send can actually happen — claiming queued
            # notifications for a message that never leaves would lose them.
            if self._gate is not None:
                section = await self._gate.drain_digest_section()
                if section is not None:
                    text = f"{text}\n\n{section}"
            await self._send_discord(text, discord_user_id)
            return "discord"

        _log.warning("unknown output_mode %r; treating as dashboard_only", output_mode)
        return "dashboard_only"

    def _deliverable(self, user_id: str) -> bool:
        return bool(user_id) and self._discord is not None

    async def send_error(self, *, text: str, discord_user_id: str) -> None:
        await self._send_discord(text, discord_user_id)

    async def _send_discord(self, text: str, user_id: str) -> None:
        if not user_id:
            _log.warning("scheduled run wants to send to Discord but no user id is configured")
            return
        if self._discord is None:
            _log.warning("scheduled run wants to send to Discord but no adapter is running")
            return
        await self._discord.send(
            OutboundMessage(
                channel_kind=ChannelKind.DISCORD,
                channel_ref=user_id,
                text=text,
            )
        )
