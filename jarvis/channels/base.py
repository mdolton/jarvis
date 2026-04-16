"""ChannelAdapter protocol + OutboundMessage envelope.

A ChannelAdapter is the bridge between an external chat platform (Discord,
Slack, etc.) and Jarvis's TriggerDispatcher. Adapters:
  - Subscribe to inbound events from their platform.
  - Filter to allow-listed senders (the adapter owns its own allow-list,
    typically read from config — keeps the dispatcher channel-agnostic).
  - Build a ChannelMessage and call dispatcher.dispatch_channel_message().
  - Receive outbound messages via send() and deliver them to the platform.

This module is pure type / protocol definitions; no I/O.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from jarvis.core.types import ChannelKind


class OutboundMessage(BaseModel):
    """A reply Jarvis wants delivered through a channel adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel_kind: ChannelKind
    channel_ref: str
    text: str


@runtime_checkable
class ChannelAdapter(Protocol):
    """The contract every channel implementation satisfies."""

    kind: str = ""

    async def start(self, dispatcher) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, msg: OutboundMessage) -> None: ...
