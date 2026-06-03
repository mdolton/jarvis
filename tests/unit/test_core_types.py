from datetime import UTC
from uuid import UUID

import pytest
from pydantic import ValidationError

from jarvis.core.types import (
    AuditEvent,
    AuditEventType,
    ChannelKind,
    ChannelMessage,
    InvocationRequest,
    ManualTrigger,
    ScheduledTrigger,
    TriggerKind,
)


def test_audit_event_type_values():
    # Must at least include these canonical types from §7 of the spec.
    required = {
        "trigger.received",
        "schedule.fired",
        "llm.request",
        "llm.response",
        "llm.error",
        "tool.call",
        "tool.result",
        "tool.error",
        "channel.sent",
        "output.suppressed",
        "config.reload_failed",
    }
    assert required.issubset({t.value for t in AuditEventType})


def test_audit_event_required_fields():
    ev = AuditEvent(
        type=AuditEventType.TRIGGER_RECEIVED,
        payload={"x": 1},
    )
    assert isinstance(ev.id, UUID)
    assert ev.created_at.tzinfo is UTC
    assert ev.conversation_id is None
    assert ev.trigger_id is None


def test_channel_message_fields():
    msg = ChannelMessage(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-123",
        text="hello",
        external_id="discord-msg-1",
    )
    assert msg.channel_kind == ChannelKind.DISCORD
    assert msg.text == "hello"


def test_scheduled_trigger_requires_prompt():
    with pytest.raises(ValidationError):
        ScheduledTrigger(schedule_id="s1", output_mode="discord")  # type: ignore[call-arg]


def test_invocation_request_accepts_all_trigger_kinds():
    for t in [
        ChannelMessage(
            channel_kind=ChannelKind.DISCORD,
            channel_ref="u1",
            text="hi",
            external_id="m1",
        ),
        ScheduledTrigger(schedule_id="s1", prompt="summarize", output_mode="discord"),
        ManualTrigger(user="mark", prompt="run it"),
    ]:
        req = InvocationRequest(trigger=t)
        assert req.trigger.kind in {k.value for k in TriggerKind}


def test_invocation_request_has_uuid_and_time():
    req = InvocationRequest(trigger=ManualTrigger(user="mark", prompt="hi"))
    assert isinstance(req.id, UUID)
    assert req.created_at.tzinfo is UTC


def test_scheduled_trigger_model_defaults_none_and_accepts_value():
    from jarvis.core.types import ScheduledTrigger

    t = ScheduledTrigger(schedule_id="s1", prompt="p", output_mode="discord")
    assert t.model is None

    t2 = ScheduledTrigger(
        schedule_id="s1", prompt="p", output_mode="discord", model="gpt-4o"
    )
    assert t2.model == "gpt-4o"


def test_model_audit_event_types_exist():
    from jarvis.core.types import AuditEventType

    assert AuditEventType.MODEL_CHANGED.value == "model.changed"
    assert AuditEventType.MODEL_FALLBACK.value == "model.fallback"
