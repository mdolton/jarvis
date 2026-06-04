from types import SimpleNamespace

from jarvis.actions.serialization import approval_item_to_json


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
