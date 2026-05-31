import asyncio
from pathlib import Path

import pytest

from jarvis.config.watcher import ConfigWatcher

# JARVIS_SECRETS_KEY is set for every integration test by the autouse
# `_set_secrets_key` fixture in tests/integration/conftest.py.


def _write(p: Path, s: str) -> None:
    p.write_text(s)


@pytest.fixture
def config_dir(tmp_path):
    _write(
        tmp_path / "jarvis.yaml",
        """
llm:
  base_url: http://x/v1
  api_key: x
  model: m
""",
    )
    _write(tmp_path / "channels.yaml", "{}")
    _write(tmp_path / "mcp-servers.yaml", "servers: []")
    return tmp_path


async def test_initial_load_fires_once(config_dir):
    calls: list = []

    async def on_change(cfg):
        calls.append(cfg)

    watcher = ConfigWatcher(config_dir, on_change=on_change)
    await watcher.start()
    await asyncio.sleep(0.05)  # allow initial load to fire
    await watcher.stop()

    assert len(calls) == 1


async def test_edit_fires_reload(config_dir):
    calls: list = []

    async def on_change(cfg):
        calls.append(cfg)

    watcher = ConfigWatcher(config_dir, on_change=on_change, debounce_sec=0.05)
    await watcher.start()
    await asyncio.sleep(0.1)

    # Edit a file.
    _write(
        config_dir / "jarvis.yaml",
        """
llm:
  base_url: http://x/v1
  api_key: x
  model: CHANGED
""",
    )
    # Give watcher + debounce time to observe.
    await asyncio.sleep(0.5)
    await watcher.stop()

    assert len(calls) >= 2
    assert calls[-1].jarvis.llm.model == "CHANGED"


async def test_bad_edit_reports_error_and_keeps_old(config_dir):
    errors: list = []
    calls: list = []

    async def on_change(cfg):
        calls.append(cfg)

    async def on_error(exc):
        errors.append(exc)

    watcher = ConfigWatcher(
        config_dir,
        on_change=on_change,
        on_error=on_error,
        debounce_sec=0.05,
    )
    await watcher.start()
    await asyncio.sleep(0.1)

    # Write invalid YAML.
    _write(config_dir / "jarvis.yaml", "llm: [not valid")
    await asyncio.sleep(0.5)
    await watcher.stop()

    assert len(errors) >= 1
    # Last successful config still callable (the one from the initial load).
    assert calls[-1].jarvis.llm.model == "m"


async def test_restart_after_stop_still_detects_changes(config_dir):
    """stop() must not permanently latch the watcher into stopped state."""
    calls: list = []

    async def on_change(cfg):
        calls.append(cfg)

    watcher = ConfigWatcher(config_dir, on_change=on_change, debounce_sec=0.05)

    # First lifecycle.
    await watcher.start()
    await asyncio.sleep(0.05)
    await watcher.stop()
    first_lifecycle_calls = len(calls)

    # Second lifecycle — must still observe edits.
    await watcher.start()
    await asyncio.sleep(0.05)
    _write(
        config_dir / "jarvis.yaml",
        """
llm:
  base_url: http://x/v1
  api_key: x
  model: AFTER_RESTART
""",
    )
    await asyncio.sleep(0.5)
    await watcher.stop()

    # At minimum: baseline initial load after restart + the edit reload.
    assert len(calls) >= first_lifecycle_calls + 2
    assert calls[-1].jarvis.llm.model == "AFTER_RESTART"


async def test_double_start_raises(config_dir):
    watcher = ConfigWatcher(config_dir, on_change=lambda cfg: asyncio.sleep(0))
    await watcher.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            await watcher.start()
    finally:
        await watcher.stop()


async def test_watcher_survives_awatch_exception(config_dir, monkeypatch):
    """An exception out of awatch must NOT silently kill the watcher —
    it should log, back off, and restart the watch loop.
    """
    from jarvis.config import watcher as watcher_module

    calls: list = []

    async def on_change(cfg):
        calls.append(cfg)

    # Patch awatch to raise once, then fall through to the real watcher.
    real_awatch = watcher_module.awatch
    failures_left = [1]

    def fake_awatch(*args, **kwargs):
        if failures_left[0] > 0:
            failures_left[0] -= 1

            async def _gen():
                raise RuntimeError("simulated fs watcher crash")
                yield  # pragma: no cover — unreachable

            return _gen()
        return real_awatch(*args, **kwargs)

    monkeypatch.setattr(watcher_module, "awatch", fake_awatch)

    watcher = ConfigWatcher(config_dir, on_change=on_change, debounce_sec=0.05)
    await watcher.start()
    # Let the first awatch raise, backoff fire, and the real awatch engage.
    await asyncio.sleep(1.3)

    _write(
        config_dir / "jarvis.yaml",
        """
llm:
  base_url: http://x/v1
  api_key: x
  model: SURVIVED
""",
    )
    await asyncio.sleep(0.5)
    await watcher.stop()

    # Initial load fired at start time. After the simulated crash + backoff,
    # the real awatch resumes and detects the edit — at minimum, that edit
    # must have produced an on_change call with the new model name.
    assert any(c.jarvis.llm.model == "SURVIVED" for c in calls), (
        f"expected post-recovery call with model=SURVIVED, got {[c.jarvis.llm.model for c in calls]}"
    )
