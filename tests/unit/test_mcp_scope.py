"""Per-schedule MCP scoping: which servers a run is allowed to use."""

import logging

from jarvis.agents.factory import build_agent, resolve_mcp_scope
from jarvis.config.schema import LLMConfig
from jarvis.core.types import ChannelKind, ChannelMessage, ScheduledTrigger


class _Server:
    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_Server({self.name!r})"


ALL = [_Server(n) for n in ("weather", "gmail", "calendar", "firecrawl", "markets")]


def _provider(only=None):
    """Stands in for MCPManager.agent_mcp_servers."""
    if only is None:
        return list(ALL)
    wanted = set(only)
    return [s for s in ALL if s.name in wanted]


def _scheduled(mcp_servers=None):
    return ScheduledTrigger(
        schedule_id="s", prompt="p", output_mode="discord", mcp_servers=mcp_servers
    )


def _build(trigger, provider=_provider):
    cfg = LLMConfig(base_url="http://x/v1", api_key="k", model="m")
    agent, _ = build_agent(llm_config=cfg, mcp_servers_provider=provider, trigger=trigger)
    return agent


def test_no_scope_means_every_server():
    assert resolve_mcp_scope(_scheduled(None)) is None
    assert len(_build(_scheduled(None)).mcp_servers) == len(ALL)


def test_empty_scope_is_treated_as_unset_not_as_zero_servers():
    """A schedule saved with nothing ticked must keep working, not lose every
    tool — the failure mode would be a silently useless run."""
    assert resolve_mcp_scope(_scheduled([])) is None
    assert len(_build(_scheduled([])).mcp_servers) == len(ALL)


def test_scope_narrows_to_the_named_servers():
    agent = _build(_scheduled(["weather", "calendar"]))
    assert [s.name for s in agent.mcp_servers] == ["weather", "calendar"]


def test_channel_messages_are_never_scoped():
    """Scoping is a scheduled-run concept; an interactive DM keeps every tool."""
    msg = ChannelMessage(
        channel_kind=ChannelKind.DISCORD, channel_ref="1", text="hi", external_id="x"
    )
    assert resolve_mcp_scope(msg) is None
    assert len(_build(msg).mcp_servers) == len(ALL)


def test_unknown_server_names_are_dropped_and_warned(caplog):
    """A schedule can outlive the server it pinned. Running with fewer tools
    than asked for is survivable, but it must not pass unremarked."""
    with caplog.at_level(logging.WARNING):
        agent = _build(_scheduled(["weather", "retired-server"]))

    assert [s.name for s in agent.mcp_servers] == ["weather"]
    assert "retired-server" in caplog.text


def test_a_fully_unmatched_scope_warns_rather_than_silently_running_toolless(caplog):
    with caplog.at_level(logging.WARNING):
        agent = _build(_scheduled(["gone-a", "gone-b"]))

    assert agent.mcp_servers == []
    assert "gone-a" in caplog.text and "gone-b" in caplog.text


def test_provider_is_called_without_arguments_when_unscoped():
    """Callers pass a plain zero-arg provider; only a real scope may add
    keyword arguments to it."""
    calls = []

    def provider(**kwargs):
        calls.append(kwargs)
        return list(ALL)

    _build(_scheduled(None), provider=provider)
    assert calls == [{}]
