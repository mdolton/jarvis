import pytest
from pydantic import ValidationError

from jarvis.core.types import TriggerSource
from jarvis.mcp.descriptor import MCPToolDescriptor
from jarvis.mcp.manager import _force_read_only
from jarvis.mcp.tool_policy import RuntimeToolDecision, runtime_decision


def test_mcp_tool_descriptor_minimal():
    t = MCPToolDescriptor(name="list_events", input_schema={"type": "object"})
    assert t.name == "list_events"
    assert t.description == ""
    assert t.read_only_hint is None
    assert t.destructive_hint is None


def test_mcp_tool_descriptor_full():
    t = MCPToolDescriptor(
        name="send_email",
        description="Send an email",
        input_schema={"type": "object", "properties": {"to": {"type": "string"}}},
        read_only_hint=False,
        destructive_hint=False,
    )
    assert t.description == "Send an email"
    assert t.read_only_hint is False


def test_mcp_tool_descriptor_rejects_extra_fields():
    with pytest.raises(ValidationError):
        MCPToolDescriptor(
            name="x",
            input_schema={},
            policy_override="confirm",  # not a field — confirm flow is repo-managed
        )  # type: ignore[call-arg]


def test_mcp_tool_descriptor_requires_input_schema():
    with pytest.raises(ValidationError):
        MCPToolDescriptor(name="x")  # type: ignore[call-arg]


def test_force_read_only_fills_missing_hint_and_unblocks_scheduled_turns():
    t = MCPToolDescriptor(name="weather_forecast", input_schema={})
    forced = _force_read_only(t)
    assert forced.read_only_hint is True
    # The point of the flag: the tool becomes callable on non-user turns.
    decision = runtime_decision(forced, trigger_source=TriggerSource.SCHEDULED)
    assert decision == RuntimeToolDecision.ALLOW


def test_force_read_only_never_overrides_server_hints():
    explicit_false = MCPToolDescriptor(name="do_thing", input_schema={}, read_only_hint=False)
    assert _force_read_only(explicit_false).read_only_hint is False

    destructive = MCPToolDescriptor(name="wipe_all", input_schema={}, destructive_hint=True)
    assert _force_read_only(destructive).read_only_hint is None
