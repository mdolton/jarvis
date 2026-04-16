import pytest
from pydantic import ValidationError

from jarvis.mcp.descriptor import MCPToolDescriptor


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
