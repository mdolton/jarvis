from uuid import uuid4

from jarvis.agents.runner import AgentRunResult
from jarvis.channels.base import OutboundMessage
from jarvis.core.types import AuditEventType, ChannelKind
from jarvis.scheduler.scheduled_output import ScheduledOutputRouter


def _result(text: str = "summary") -> AgentRunResult:
    return AgentRunResult(
        final_output=text,
        conversation_id=uuid4(),
        trigger_id=uuid4(),
        channel_kind=ChannelKind.SCHEDULED,
        channel_ref="sched-1",
    )


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


async def test_discord_mode_sends_to_discord():
    adapter = _RecordingAdapter()
    router = ScheduledOutputRouter(discord_adapter=adapter)

    await router.route(
        result=_result(text="your morning email summary"),
        output_mode="discord",
        discord_user_id="111",
    )

    assert len(adapter.sent) == 1
    assert adapter.sent[0].channel_ref == "111"
    assert adapter.sent[0].text == "your morning email summary"


async def test_dashboard_only_mode_sends_nothing():
    adapter = _RecordingAdapter()
    router = ScheduledOutputRouter(discord_adapter=adapter)

    await router.route(
        result=_result(text="some data"),
        output_mode="dashboard_only",
        discord_user_id="111",
    )

    assert adapter.sent == []


async def test_noteworthy_mode_sends_when_prefixed():
    adapter = _RecordingAdapter()
    router = ScheduledOutputRouter(discord_adapter=adapter)

    await router.route(
        result=_result(text="[NOTEWORTHY] You have 3 urgent emails"),
        output_mode="discord_if_noteworthy",
        discord_user_id="111",
    )

    assert len(adapter.sent) == 1
    assert "[NOTEWORTHY]" not in adapter.sent[0].text
    assert "3 urgent emails" in adapter.sent[0].text


async def test_noteworthy_mode_silent_when_prefixed():
    adapter = _RecordingAdapter()
    router = ScheduledOutputRouter(discord_adapter=adapter)

    await router.route(
        result=_result(text="[SILENT] Nothing new"),
        output_mode="discord_if_noteworthy",
        discord_user_id="111",
    )

    assert adapter.sent == []


async def test_noteworthy_mode_sends_when_no_prefix():
    """If the agent didn't use a prefix, default to sending (fail-open)."""
    adapter = _RecordingAdapter()
    router = ScheduledOutputRouter(discord_adapter=adapter)

    await router.route(
        result=_result(text="Here is your summary"),
        output_mode="discord_if_noteworthy",
        discord_user_id="111",
    )

    assert len(adapter.sent) == 1


async def test_no_discord_adapter_silently_skips():
    """If no Discord adapter is running, scheduled outputs to discord
    are silently dropped (not raised)."""
    router = ScheduledOutputRouter(discord_adapter=None)

    await router.route(
        result=_result(),
        output_mode="discord",
        discord_user_id="111",
    )


async def test_discord_mode_without_user_id_skips():
    adapter = _RecordingAdapter()
    router = ScheduledOutputRouter(discord_adapter=adapter)

    await router.route(
        result=_result(),
        output_mode="discord",
        discord_user_id="",
    )

    assert adapter.sent == []


class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)


def _trace_payloads(audit: _RecordingAudit) -> list[dict]:
    return [e.payload for e in audit.events if e.type is AuditEventType.AUTONOMY_TRACE]


async def test_discord_mode_traces_discord_delivery():
    adapter = _RecordingAdapter()
    audit = _RecordingAudit()
    router = ScheduledOutputRouter(discord_adapter=adapter, audit=audit)

    await router.route(
        result=_result(text="morning brief"),
        output_mode="discord",
        discord_user_id="111",
        source="schedule:morning-brief",
    )

    assert adapter.sent[0].text == "morning brief"  # expected digest: no prefix
    payloads = _trace_payloads(audit)
    assert payloads == [
        {
            "source": "schedule:morning-brief",
            "reason": "scheduled run (schedule:morning-brief)",
            "action": "morning brief",
            "delivery": "discord",
        }
    ]


async def test_dashboard_only_mode_traces_dashboard_only():
    audit = _RecordingAudit()
    router = ScheduledOutputRouter(discord_adapter=_RecordingAdapter(), audit=audit)

    await router.route(
        result=_result(text="some data"),
        output_mode="dashboard_only",
        discord_user_id="111",
        source="schedule:s1",
    )

    assert _trace_payloads(audit)[0]["delivery"] == "dashboard_only"


async def test_noteworthy_send_gets_provenance_prefix_and_discord_trace():
    adapter = _RecordingAdapter()
    audit = _RecordingAudit()
    router = ScheduledOutputRouter(discord_adapter=adapter, audit=audit)

    await router.route(
        result=_result(text="[NOTEWORTHY] server is down"),
        output_mode="discord_if_noteworthy",
        discord_user_id="111",
        source="schedule:watchdog",
    )

    assert adapter.sent[0].text == "⚙️ [schedule:watchdog] server is down"
    assert _trace_payloads(audit)[0]["delivery"] == "discord"


async def test_silent_noteworthy_result_traces_suppressed():
    adapter = _RecordingAdapter()
    audit = _RecordingAudit()
    router = ScheduledOutputRouter(discord_adapter=adapter, audit=audit)

    await router.route(
        result=_result(text="[SILENT] all quiet"),
        output_mode="discord_if_noteworthy",
        discord_user_id="111",
        source="schedule:watchdog",
    )

    assert adapter.sent == []
    assert _trace_payloads(audit)[0]["delivery"] == "suppressed"


async def test_undeliverable_send_traces_undelivered():
    audit = _RecordingAudit()
    router = ScheduledOutputRouter(discord_adapter=None, audit=audit)

    await router.route(
        result=_result(text="morning brief"),
        output_mode="discord",
        discord_user_id="111",
        source="schedule:s1",
    )

    assert _trace_payloads(audit)[0]["delivery"] == "undelivered"


async def test_unknown_output_mode_traces_dashboard_only():
    audit = _RecordingAudit()
    router = ScheduledOutputRouter(discord_adapter=_RecordingAdapter(), audit=audit)

    await router.route(
        result=_result(text="x"),
        output_mode="bogus",
        discord_user_id="111",
        source="schedule:s1",
    )

    assert _trace_payloads(audit)[0]["delivery"] == "dashboard_only"
