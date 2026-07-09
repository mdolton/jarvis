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
"""

import logging

from jarvis.agents.runner import AgentRunResult
from jarvis.channels.base import ChannelAdapter, OutboundMessage
from jarvis.core.output_router import NotificationGate
from jarvis.core.types import ChannelKind

_log = logging.getLogger(__name__)


class ScheduledOutputRouter:
    def __init__(
        self,
        *,
        discord_adapter: ChannelAdapter | None,
        notification_gate: NotificationGate | None = None,
    ) -> None:
        self._discord = discord_adapter
        self._gate = notification_gate

    async def route(
        self,
        *,
        result: AgentRunResult,
        output_mode: str,
        discord_user_id: str,
        source: str = "scheduled",
    ) -> None:
        if output_mode == "dashboard_only":
            return

        if output_mode == "discord_if_noteworthy":
            text = result.final_output
            upper = text.lstrip().upper()
            if upper.startswith("[SILENT]"):
                return
            if upper.startswith("[NOTEWORTHY]"):
                text = text.lstrip()
                text = text[len("[NOTEWORTHY]") :].lstrip()
            # Consult the gate only when the send can actually happen —
            # otherwise budget would be spent on a message that never leaves.
            if self._gate is not None and self._deliverable(discord_user_id):
                admitted = await self._gate.admit(text=text, source=source)
                if admitted is None:
                    return
                text = admitted
            await self._send_discord(text, discord_user_id)
            return

        if output_mode == "discord":
            text = result.final_output
            # Drain only when the send can actually happen — claiming queued
            # notifications for a message that never leaves would lose them.
            if self._gate is not None and self._deliverable(discord_user_id):
                section = await self._gate.drain_digest_section()
                if section is not None:
                    text = f"{text}\n\n{section}"
            await self._send_discord(text, discord_user_id)
            return

        _log.warning("unknown output_mode %r; treating as dashboard_only", output_mode)

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
