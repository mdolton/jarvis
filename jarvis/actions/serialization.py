from __future__ import annotations

import json
from typing import Any

from agents.items import ToolApprovalItem
from agents.run_state import RunState


def run_state_to_json(state: RunState) -> dict[str, Any]:
    return state.to_json()


async def run_state_from_json(agent, payload: dict[str, Any]) -> RunState:
    return await RunState.from_json(agent, payload)


def approval_item_to_json(approval_item: ToolApprovalItem | Any) -> dict[str, Any]:
    """Return durable display/audit metadata for an approval interruption.

    This JSON is not the canonical SDK approval item. Task 4 should reconstruct
    canonical approval items from RunState.from_json(...).get_interruptions().
    """
    raw = getattr(approval_item, "raw_item", approval_item)
    if hasattr(raw, "model_dump"):
        raw_payload = raw.model_dump(exclude_unset=True)
    elif isinstance(raw, dict):
        raw_payload = dict(raw)
    else:
        raw_payload = dict(getattr(raw, "__dict__", {}))

    tool_name = (
        _get_attr(approval_item, "tool_name")
        or _get_attr(approval_item, "name")
        or raw_payload.get("name")
        or raw_payload.get("tool_name")
        or getattr(approval_item, "tool_name", None)
        or "unknown"
    )
    call_id = (
        _get_attr(approval_item, "call_id") or raw_payload.get("call_id") or raw_payload.get("id")
    )
    tool_namespace = _get_attr(approval_item, "tool_namespace")
    server_name = (
        tool_namespace
        or raw_payload.get("server_label")
        or raw_payload.get("server_name")
        or raw_payload.get("server")
        or raw_payload.get("namespace")
        or "unknown"
    )
    arguments = _first_present(
        _get_attr(approval_item, "arguments"),
        raw_payload.get("arguments"),
        raw_payload.get("arguments_json"),
        default={},
    )
    if isinstance(arguments, str):
        try:
            arguments_json = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            arguments_json = {"raw": arguments}
    elif isinstance(arguments, dict):
        arguments_json = arguments
    else:
        arguments_json = {"value": arguments}

    payload = {
        "raw_item": raw_payload,
        "server_name": str(server_name),
        "tool_name": str(tool_name),
        "tool_call_id": str(call_id) if call_id is not None else None,
        "arguments_json": arguments_json,
    }
    if tool_namespace is not None:
        payload["tool_namespace"] = str(tool_namespace)

    tool_lookup_key = _get_attr(approval_item, "tool_lookup_key")
    if tool_lookup_key is not None:
        payload["tool_lookup_key"] = _json_compatible(tool_lookup_key)

    return payload


def approval_item_from_json(agent, payload: dict[str, Any]) -> ToolApprovalItem:
    """Best-effort compatibility helper for older tests and tooling.

    Production resume semantics should use approval items returned by a
    deserialized RunState's get_interruptions(), not this display/audit JSON.
    """
    raw = payload.get("raw_item", payload)
    return ToolApprovalItem(agent=agent, raw_item=raw)


def _get_attr(obj: Any, name: str) -> Any:
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _first_present(*values: Any, default: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return default


def _json_compatible(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError):
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if hasattr(value, "__dict__"):
            return {
                str(k): _json_compatible(v)
                for k, v in vars(value).items()
                if not str(k).startswith("_")
            }
        return repr(value)
