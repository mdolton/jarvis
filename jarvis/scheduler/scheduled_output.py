"""ScheduledOutputRouter — per-schedule output_mode routing.

After a scheduled agent run completes, this router decides where the
result goes based on the schedule's `output_mode`:

  - "discord": send via the Discord adapter to a specific user.
  - "dashboard_only": no outbound message; result stays in DB for dashboard.
  - "discord_if_noteworthy": check for [NOTEWORTHY] or [SILENT] prefix.
    If noteworthy (or no prefix — fail-open), send to Discord; if [SILENT], suppress.
"""

import logging

from jarvis.agents.runner import AgentRunResult
from jarvis.channels.base import ChannelAdapter, OutboundMessage
from jarvis.core.types import ChannelKind

_log = logging.getLogger(__name__)


class ScheduledOutputRouter:
    def __init__(self, *, discord_adapter: ChannelAdapter | None) -> None:
        self._discord = discord_adapter

    async def route(
        self,
        *,
        result: AgentRunResult,
        output_mode: str,
        discord_user_id: str,
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
            await self._send_discord(text, discord_user_id)
            return

        if output_mode == "discord":
            await self._send_discord(result.final_output, discord_user_id)
            return

        _log.warning("unknown output_mode %r; treating as dashboard_only", output_mode)

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
