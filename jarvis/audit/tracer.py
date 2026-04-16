"""Agents SDK trace processor that forwards spans to our AuditLogger.

The SDK emits typed spans for: agent start/end, LLM generations (request +
response), tool calls, and tool results. Each span becomes one AuditEvent
with the span's structured data as payload.

We install this via `set_trace_processors([JarvisTraceProcessor(...)])` in
the bootstrap, which replaces the default OpenAI-backend exporter. When
paired with `set_tracing_disabled(True)` in llm_client.install_as_default,
no traces leak to OpenAI.
"""

import asyncio
import json
import logging
from typing import Any

from agents.tracing import Span, Trace, TracingProcessor

from jarvis.audit.logger import AuditLogger
from jarvis.core.types import AuditEvent, AuditEventType

_log = logging.getLogger(__name__)

# Map Agents SDK span types to our audit event types. The SDK's span data
# classes live in `agents.tracing.span_data` — we match by class name string
# to avoid importing every symbol (and to tolerate SDK version drift).
_SPAN_TYPE_TO_AUDIT: dict[str, AuditEventType] = {
    "GenerationSpanData": AuditEventType.LLM_RESPONSE,
    "ResponseSpanData": AuditEventType.LLM_RESPONSE,
    "FunctionSpanData": AuditEventType.TOOL_CALL,
    "MCPListToolsSpanData": AuditEventType.MCP_CONNECTED,
}


class JarvisTraceProcessor(TracingProcessor):
    """Forwards every SDK span to AuditLogger as an AuditEvent."""

    def __init__(self, logger: AuditLogger) -> None:
        self._logger = logger
        # Holds strong references to in-flight emit tasks so the event loop
        # can't silently drop them. Tasks remove themselves on completion.
        self._pending: set[asyncio.Task[None]] = set()

    def on_trace_start(self, trace: Trace) -> None:
        # Trace-level events aren't in our AuditEventType enum; we emit them
        # as LLM_REQUEST at trace start to mark the invocation boundary.
        self._emit(
            AuditEventType.LLM_REQUEST,
            payload={
                "trace_id": getattr(trace, "trace_id", None),
                "workflow_name": getattr(trace, "name", None),
                "phase": "start",
            },
        )

    def on_trace_end(self, trace: Trace) -> None:
        return

    def on_span_start(self, span: Span[Any]) -> None:
        return

    def on_span_end(self, span: Span[Any]) -> None:
        span_type_name = type(span.span_data).__name__
        audit_type = _SPAN_TYPE_TO_AUDIT.get(span_type_name)
        if audit_type is None:
            return
        payload: dict[str, Any] = {
            "span_type": span_type_name,
            "trace_id": span.trace_id,
            "span_id": span.span_id,
        }
        # Best-effort: serialize span_data's public attributes.
        data = span.span_data
        for attr in dir(data):
            if attr.startswith("_"):
                continue
            val = getattr(data, attr, None)
            if callable(val):
                continue
            try:
                _json_safe(val)
            except (TypeError, ValueError):
                continue
            payload[attr] = val
        self._emit(audit_type, payload=payload)

    def shutdown(self) -> None:
        return

    def force_flush(self) -> None:
        return

    def _emit(self, audit_type: AuditEventType, *, payload: dict) -> None:
        event = AuditEvent(type=audit_type, payload=_json_safe_dict(payload))
        # `on_*` are sync callbacks; schedule the async emit.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _log.debug("no running loop; dropping trace event %s", audit_type)
            return
        task = loop.create_task(self._logger.emit(event))
        self._pending.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        self._pending.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.warning("audit emit failed in tracer: %r", exc)


def _json_safe(value: Any) -> None:
    """Raise TypeError/ValueError if `value` is not JSON-serializable."""
    json.dumps(value, default=str)


def _json_safe_dict(d: dict) -> dict:
    return json.loads(json.dumps(d, default=str))
