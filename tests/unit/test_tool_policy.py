from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.mcp.tool_policy import RuntimeToolDecision, ToolPolicy, classify, runtime_decision


def _desc(**kwargs) -> MCPToolDescriptor:
    defaults = {"name": "x", "input_schema": {}}
    defaults.update(kwargs)
    return MCPToolDescriptor(**defaults)


def test_user_override_wins():
    t = _desc(name="whatever", destructive_hint=True)
    assert classify(t, override="auto") == ToolPolicy.AUTO
    assert classify(t, override="confirm") == ToolPolicy.CONFIRM


def test_read_only_hint_auto():
    assert classify(_desc(name="fetch", read_only_hint=True)) == ToolPolicy.AUTO


def test_destructive_hint_confirm():
    assert classify(_desc(name="list_events", destructive_hint=True)) == ToolPolicy.CONFIRM


def test_destructive_wins_over_read_only():
    """If a tool is both read_only AND destructive, destructive wins."""
    t = _desc(name="x", read_only_hint=True, destructive_hint=True)
    assert classify(t) == ToolPolicy.CONFIRM


def test_heuristic_read_prefixes():
    for name in ("get_thing", "list_things", "read_item", "search_docs", "fetch_url"):
        assert classify(_desc(name=name)) == ToolPolicy.AUTO, name


def test_heuristic_unknown_defaults_to_confirm():
    for name in ("send_email", "delete_event", "execute_query", "do_thing"):
        assert classify(_desc(name=name)) == ToolPolicy.CONFIRM, name


def test_heuristic_case_insensitive():
    assert classify(_desc(name="GET_Thing")) == ToolPolicy.AUTO
    assert classify(_desc(name="List_Things")) == ToolPolicy.AUTO


def test_override_accepts_none():
    """override=None falls through to annotation/heuristic."""
    assert classify(_desc(name="get_x"), override=None) == ToolPolicy.AUTO


def test_runtime_override_allow_confirm_deny():
    t = _desc(name="delete_event", destructive_hint=True)
    assert runtime_decision(t, override="allow") == RuntimeToolDecision.ALLOW
    assert runtime_decision(t, override="confirm") == RuntimeToolDecision.CONFIRM
    assert runtime_decision(t, override="deny") == RuntimeToolDecision.DENY


def test_runtime_auto_detect_maps_classifier():
    assert runtime_decision(_desc(name="list_events"), override=None) == RuntimeToolDecision.ALLOW
    assert runtime_decision(_desc(name="send_email"), override=None) == RuntimeToolDecision.CONFIRM


def test_non_user_source_denies_side_effect_tools():
    from jarvis.core.types import TriggerSource

    for source in (TriggerSource.SCHEDULED, TriggerSource.EVENT):
        for name in ("send_email", "delete_event", "create_draft", "do_thing"):
            assert (
                runtime_decision(_desc(name=name), trigger_source=source)
                == RuntimeToolDecision.DENY
            ), (source, name)


def test_non_user_source_denies_even_with_allow_or_confirm_override():
    """A per-tool override is set without trigger-source context; restriction wins."""
    from jarvis.core.types import TriggerSource

    t = _desc(name="send_email")
    for override in ("allow", "auto", "confirm"):
        assert (
            runtime_decision(t, override=override, trigger_source=TriggerSource.EVENT)
            == RuntimeToolDecision.DENY
        ), override


def test_non_user_source_denies_destructive_read_only():
    from jarvis.core.types import TriggerSource

    t = _desc(name="list_events", read_only_hint=True, destructive_hint=True)
    assert runtime_decision(t, trigger_source=TriggerSource.EVENT) == RuntimeToolDecision.DENY


def test_non_user_source_allows_read_only_tools():
    from jarvis.core.types import TriggerSource

    for source in (TriggerSource.SCHEDULED, TriggerSource.EVENT):
        assert (
            runtime_decision(_desc(name="list_events"), trigger_source=source)
            == RuntimeToolDecision.ALLOW
        )
        assert (
            runtime_decision(_desc(name="whoami", read_only_hint=True), trigger_source=source)
            == RuntimeToolDecision.ALLOW
        )


def test_non_user_source_keeps_deny_override():
    from jarvis.core.types import TriggerSource

    t = _desc(name="list_events")
    assert (
        runtime_decision(t, override="deny", trigger_source=TriggerSource.EVENT)
        == RuntimeToolDecision.DENY
    )


def test_user_source_behavior_unchanged():
    from jarvis.core.types import TriggerSource

    t = _desc(name="send_email")
    assert runtime_decision(t, trigger_source=TriggerSource.USER) == RuntimeToolDecision.CONFIRM
    assert (
        runtime_decision(t, override="allow", trigger_source=TriggerSource.USER)
        == RuntimeToolDecision.ALLOW
    )
