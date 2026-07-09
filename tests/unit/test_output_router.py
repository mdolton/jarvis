from uuid import uuid4

import pytest

from jarvis.agents.runner import AgentRunResult
from jarvis.channels.base import OutboundMessage
from jarvis.core.output_router import OutputRouter, Priority, classify_priority
from jarvis.core.types import ChannelKind


class _RecordingAdapter:
    kind = ChannelKind.DISCORD.value

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def start(self, dispatcher) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


def _result(*, kind: ChannelKind, ref: str, text: str = "reply") -> AgentRunResult:
    return AgentRunResult(
        final_output=text,
        conversation_id=uuid4(),
        trigger_id=uuid4(),
        channel_kind=kind,
        channel_ref=ref,
    )


async def test_routes_discord_result_to_discord_adapter():
    adapter = _RecordingAdapter()
    router = OutputRouter(adapters=[adapter])

    await router.route(_result(kind=ChannelKind.DISCORD, ref="user-1", text="hi"))

    assert len(adapter.sent) == 1
    assert adapter.sent[0].channel_ref == "user-1"
    assert adapter.sent[0].text == "hi"


async def test_dashboard_result_is_silently_skipped():
    adapter = _RecordingAdapter()
    router = OutputRouter(adapters=[adapter])

    await router.route(_result(kind=ChannelKind.DASHBOARD, ref="cli"))

    assert adapter.sent == []


async def test_scheduled_result_is_silently_skipped():
    """Scheduled runs route per their own output_mode (Plan 4), not through
    the channel adapter system."""
    adapter = _RecordingAdapter()
    router = OutputRouter(adapters=[adapter])

    await router.route(_result(kind=ChannelKind.SCHEDULED, ref="sched-1"))

    assert adapter.sent == []


async def test_event_result_is_silently_skipped():
    """Event-triggered runs are internal; their output surfaces on the dashboard."""
    adapter = _RecordingAdapter()
    router = OutputRouter(adapters=[adapter])

    await router.route(_result(kind=ChannelKind.EVENT, ref="email"))

    assert adapter.sent == []


async def test_no_adapter_for_kind_raises():
    router = OutputRouter(adapters=[])

    with pytest.raises(LookupError, match="discord"):
        await router.route(_result(kind=ChannelKind.DISCORD, ref="u"))


async def test_empty_text_is_still_sent():
    adapter = _RecordingAdapter()
    router = OutputRouter(adapters=[adapter])

    await router.route(_result(kind=ChannelKind.DISCORD, ref="u", text=""))

    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == ""


def test_classify_priority_explicit_markers():
    for name, expected in (("P1", Priority.P1), ("P2", Priority.P2),
                           ("P3", Priority.P3), ("P4", Priority.P4)):
        priority, cleaned = classify_priority(f"[{name}] pay the bill")
        assert priority is expected
        assert cleaned == "pay the bill"


def test_classify_priority_is_case_insensitive_and_tolerates_whitespace():
    priority, cleaned = classify_priority("  [p1]  fire  ")
    assert priority is Priority.P1
    assert cleaned == "fire  "


def test_classify_priority_defaults_to_p3():
    priority, cleaned = classify_priority("no marker here")
    assert priority is Priority.P3
    assert cleaned == "no marker here"


def test_classify_priority_silent_drops():
    priority, cleaned = classify_priority("[SILENT] whatever")
    assert priority is None
    assert cleaned == ""


def test_classify_priority_marker_mid_text_is_ignored():
    priority, cleaned = classify_priority("see [P1] in the docs")
    assert priority is Priority.P3
    assert cleaned == "see [P1] in the docs"
