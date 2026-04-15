"""Async config watcher: reloads on file changes, debounces, reports errors."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from watchfiles import awatch

from jarvis.config.loader import ConfigLoadError, LoadedConfig, load_config

_log = logging.getLogger(__name__)

OnChange = Callable[[LoadedConfig], Awaitable[None]]
OnError = Callable[[Exception], Awaitable[None]]


async def _noop_error(exc: Exception) -> None:
    return None


class ConfigWatcher:
    """Watches jarvis.yaml / channels.yaml / mcp-servers.yaml and reloads."""

    def __init__(
        self,
        config_dir: Path | str,
        *,
        on_change: OnChange,
        on_error: OnError | None = None,
        debounce_sec: float = 0.2,
    ) -> None:
        self._dir = Path(config_dir)
        self._on_change = on_change
        self._on_error = on_error or _noop_error
        self._debounce = debounce_sec
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("ConfigWatcher already started")
        self._stopping.clear()
        # Do an immediate load so callers have a baseline config.
        await self._try_load_and_emit()
        self._task = asyncio.create_task(self._run(), name="config-watcher")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _try_load_and_emit(self) -> None:
        try:
            cfg = load_config(self._dir)
        except ConfigLoadError as e:
            _log.warning("config reload failed: %s", e)
            await self._on_error(e)
            return
        await self._on_change(cfg)

    async def _run(self) -> None:
        try:
            async for _ in awatch(self._dir, debounce=int(self._debounce * 1000)):
                if self._stopping.is_set():
                    return
                await self._try_load_and_emit()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("watcher loop error")
