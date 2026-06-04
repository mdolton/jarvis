from types import SimpleNamespace

from agents import Agent
from agents.items import ToolApprovalItem

from jarvis.actions import serialization
from jarvis.actions.serialization import approval_item_to_json, run_state_from_json


def test_approval_item_to_json_extracts_tool_metadata():
    approval = SimpleNamespace(
        raw_item=SimpleNamespace(
            name="send_email",
            call_id="call-1",
            arguments='{"to":"me@example.com"}',
            server_label="gmail",
        )
    )

    payload = approval_item_to_json(approval)

    assert payload["tool_name"] == "send_email"
    assert payload["tool_call_id"] == "call-1"
    assert payload["arguments_json"] == {"to": "me@example.com"}
    assert payload["server_name"] == "gmail"


def test_approval_item_to_json_prefers_sdk_properties():
    approval = ToolApprovalItem(
        agent=Agent(name="jarvis", instructions="test"),
        raw_item=SimpleNamespace(
            name="raw_send_email",
            call_id="call-1",
            arguments='{"ignored":true}',
        ),
        tool_name="send_email",
        tool_namespace="gmail",
        tool_lookup_key=("namespaced", "gmail", "send_email"),
    )

    payload = approval_item_to_json(approval)

    assert payload["server_name"] == "gmail"
    assert payload["tool_namespace"] == "gmail"
    assert payload["tool_name"] == "send_email"
    assert payload["tool_call_id"] == "call-1"
    assert payload["arguments_json"] == {"ignored": True}
    assert payload["tool_lookup_key"] == ["namespaced", "gmail", "send_email"]


async def test_run_state_from_json_awaits_sdk_deserializer(monkeypatch):
    expected = object()
    agent = object()
    payload = {"state": "serialized"}

    async def fake_from_json(got_agent, got_payload):
        assert got_agent is agent
        assert got_payload == payload
        return expected

    monkeypatch.setattr(
        serialization.RunState,
        "from_json",
        staticmethod(fake_from_json),
    )

    assert await run_state_from_json(agent, payload) is expected
