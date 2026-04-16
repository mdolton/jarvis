import pytest
from pydantic import ValidationError

from jarvis.channels.base import ChannelAdapter, OutboundMessage
from jarvis.core.types import ChannelKind


def test_outbound_message_minimal():
    msg = OutboundMessage(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        text="hello",
    )
    assert msg.text == "hello"


def test_outbound_message_rejects_extra_fields():
    with pytest.raises(ValidationError):
        OutboundMessage(
            channel_kind=ChannelKind.DISCORD,
            channel_ref="u",
            text="t",
            extra="nope",  # type: ignore[call-arg]
        )


def test_channel_adapter_is_a_protocol():
    """Sanity: ChannelAdapter is a Protocol — anything with the required
    members satisfies it without explicit inheritance.
    """
    import typing

    assert typing.get_origin(typing.runtime_checkable(ChannelAdapter)) is None
    assert hasattr(ChannelAdapter, "kind")
    assert hasattr(ChannelAdapter, "start")
    assert hasattr(ChannelAdapter, "stop")
    assert hasattr(ChannelAdapter, "send")


async def test_protocol_is_satisfied_by_a_minimal_class():
    """Define a minimal class that satisfies the protocol, instantiate it,
    and call its methods. This catches signature mismatches at test time.
    """

    class _NoopAdapter:
        kind = "noop"

        async def start(self, dispatcher) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def send(self, msg: OutboundMessage) -> None:
            return None

    adapter: ChannelAdapter = _NoopAdapter()
    await adapter.start(dispatcher=None)
    await adapter.send(
        OutboundMessage(
            channel_kind=ChannelKind.DISCORD,
            channel_ref="u",
            text="t",
        )
    )
    await adapter.stop()
