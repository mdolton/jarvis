from __future__ import annotations

import json
from typing import Any

from agents.items import ToolApprovalItem
from agents.run_state import RunState


def run_state_to_json(state: RunState) -> dict[str, Any]:
    return state.to_json()


async def run_state_from_json(agent, payload: dict[str, Any]) -> RunState:
    return RunState.from_json(agent, payload)


def approval_item_to_json(approval_item: ToolApprovalItem | Any) -> dict[str, Any]:
    raw = getattr(approval_item, "raw_item", approval_item)
    if hasattr(raw, "model_dump"):
        raw_payload = raw.model_dump(exclude_unset=True)
    elif isinstance(raw, dict):
        raw_payload = dict(raw)
    else:
        raw_payload = dict(getattr(raw, "__dict__", {}))

    tool_name = (
        raw_payload.get("name")
        or raw_payload.get("tool_name")
        or getattr(approval_item, "tool_name", None)
        or "unknown"
    )
    call_id = raw_payload.get("call_id") or raw_payload.get("id")
    server_name = (
        raw_payload.get("server_label")
        or raw_payload.get("server_name")
        or raw_payload.get("server")
        or raw_payload.get("namespace")
        or "unknown"
    )
    arguments = raw_payload.get("arguments") or raw_payload.get("arguments_json") or {}
    if isinstance(arguments, str):
        try:
            arguments_json = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            arguments_json = {"raw": arguments}
    elif isinstance(arguments, dict):
        arguments_json = arguments
    else:
        arguments_json = {"value": arguments}

    return {
        "raw_item": raw_payload,
        "server_name": str(server_name),
        "tool_name": str(tool_name),
        "tool_call_id": str(call_id) if call_id is not None else None,
        "arguments_json": arguments_json,
    }
