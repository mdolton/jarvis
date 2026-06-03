from jarvis.agents.runner import resolve_model
from jarvis.core.types import ChannelKind, ChannelMessage, ScheduledTrigger


def _channel():
    return ChannelMessage(
        channel_kind=ChannelKind.DISCORD, channel_ref="1", text="hi", external_id="x"
    )


def _scheduled(model=None):
    return ScheduledTrigger(schedule_id="s", prompt="p", output_mode="discord", model=model)


def test_explicit_override_wins():
    sentinel = object()
    got = resolve_model(
        _scheduled("pinned"), explicit=sentinel, model_provider=lambda: "prov", config_default="cfg"
    )
    assert got is sentinel


def test_scheduled_trigger_model_used_when_set():
    got = resolve_model(
        _scheduled("pinned"), explicit=None, model_provider=lambda: "prov", config_default="cfg"
    )
    assert got == "pinned"


def test_scheduled_without_model_uses_provider():
    got = resolve_model(
        _scheduled(None), explicit=None, model_provider=lambda: "prov", config_default="cfg"
    )
    assert got == "prov"


def test_channel_trigger_uses_provider():
    got = resolve_model(
        _channel(), explicit=None, model_provider=lambda: "prov", config_default="cfg"
    )
    assert got == "prov"


def test_falls_back_to_config_default_without_provider():
    got = resolve_model(_channel(), explicit=None, model_provider=None, config_default="cfg")
    assert got == "cfg"
