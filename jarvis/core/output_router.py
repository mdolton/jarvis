"""OutputRouter — sends agent results to the originating channel.

The router holds a list of channel adapters and dispatches by channel_kind.
Dashboard / manual triggers (where the CLI or web app prints the reply
itself) and scheduled triggers (which have their own output routing in
Plan 4) are explicitly no-ops.

A Discord-kind result with no Discord adapter wired is a misconfiguration:
the run produced output that the user expected to receive, and silently
dropping it is worse than raising. We surface a LookupError.
"""

from collections.abc import Iterable

from jarvis.agents.runner import AgentRunResult
from jarvis.channels.base import ChannelAdapter, OutboundMessage
from jarvis.core.types import ChannelKind

# Channel kinds whose results don't go through an adapter.
_INTERNAL_KINDS: frozenset[ChannelKind] = frozenset(
    {
        ChannelKind.DASHBOARD,
        ChannelKind.SCHEDULED,  # scheduled runs route per their own output_mode (Plan 4)
        ChannelKind.EVENT,  # event-triggered runs surface on the dashboard only
    }
)


class OutputRouter:
    def __init__(self, *, adapters: Iterable[ChannelAdapter]) -> None:
        self._by_kind: dict[str, ChannelAdapter] = {a.kind: a for a in adapters}

    async def route(self, result: AgentRunResult) -> None:
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
