"""DiscordMessageStream — streams agent output by live-editing a draft DM.

Lifecycle: start() enters the typing context (discord.py re-triggers it every
few seconds, so the indicator persists through silent tool phases). update()/
status() render into one draft message, throttled to at most one edit per
`min_edit_interval` seconds (Discord's per-channel edit bucket is ~5 req/5s —
dropped intermediate frames are fine because the next render or finish()
carries the full text). finish() bypasses the throttle, chunks text over the
2000-char limit, and sets `delivered`. close() is the idempotent safety net:
it always exits the typing context.

Failure policy: streaming is best-effort decoration. Every Discord error is
caught and logged; after _MAX_FAILURES consecutive render failures the stream
goes inert. `delivered` is True only when finish() actually placed the final
text, so the caller can fall back to a plain send.
"""

import asyncio
import logging
from collections.abc import Callable

_log = logging.getLogger(__name__)

_MESSAGE_LIMIT = 2000
_PREVIEW_LIMIT = 1900  # streaming cap; leaves room for the status line


def _chunks(text: str, limit: int = _MESSAGE_LIMIT) -> list[str]:
    text = text.strip()
    return [text[i : i + limit] for i in range(0, len(text), limit)]


class DiscordMessageStream:
    _MAX_FAILURES = 3

    def __init__(
        self,
        *,
        channel,
        min_edit_interval: float = 1.5,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._channel = channel
        self._interval = min_edit_interval
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._typing = None
        self._draft = None
        self._text = ""
        self._status: str | None = None
        self._last_edit = float("-inf")
        self._failures = 0
        self._closed = False
        self.delivered = False

    async def start(self) -> None:
        try:
            typing = self._channel.typing()
            await typing.__aenter__()
            self._typing = typing
        except Exception:
            _log.exception("discord stream: could not start typing indicator")

    async def update(self, text: str) -> None:
        self._text = text
        await self._render()

    async def status(self, label: str | None) -> None:
        self._status = label
        await self._render()

    async def finish(self, final_text: str) -> None:
        if self._closed:
            return
        self._closed = True
        await self._stop_typing()
        chunks = _chunks(final_text)
        if not chunks:
            return
        try:
            if self._draft is None:
                await self._channel.send(chunks[0])
            else:
                await self._draft.edit(content=chunks[0])
            for extra in chunks[1:]:
                await self._channel.send(extra)
        except Exception:
            _log.exception("discord stream: final edit failed; caller will fall back")
            return
        self.delivered = True

    async def close(self) -> None:
        self._closed = True
        await self._stop_typing()

    async def _stop_typing(self) -> None:
        typing, self._typing = self._typing, None
        if typing is None:
            return
        try:
            await typing.__aexit__(None, None, None)
        except Exception:
            _log.exception("discord stream: failed to clear typing indicator")

    async def _render(self) -> None:
        if self._closed or self._failures >= self._MAX_FAILURES:
            return
        content = self._compose()
        if not content:
            return
        now = self._clock()
        if self._draft is not None and (now - self._last_edit) < self._interval:
            return  # dropped frame; the next render or finish() carries the text
        try:
            if self._draft is None:
                self._draft = await self._channel.send(content)
            else:
                await self._draft.edit(content=content)
        except Exception:
            self._failures += 1
            _log.warning(
                "discord stream: draft render failed (%d/%d)",
                self._failures,
                self._MAX_FAILURES,
                exc_info=True,
            )
            return
        self._last_edit = now
        self._failures = 0

    def _compose(self) -> str:
        text = self._text.strip()
        if len(text) > _PREVIEW_LIMIT:
            text = text[:_PREVIEW_LIMIT] + " …"
        if self._status:
            line = f"⚙️ *{self._status}…*"
            return f"{text}\n\n{line}" if text else line
        return text
