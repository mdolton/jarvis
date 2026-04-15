import asyncio
from pathlib import Path

import pytest

from jarvis.config.watcher import ConfigWatcher


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
