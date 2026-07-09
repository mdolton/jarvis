"""Per-run trigger-source scope.

`AgentRunner` sets the scope for the duration of an agent run; the MCP
approval policy reads it when the SDK asks for tool filtering/approval. A
contextvar is used because the SDK server objects (and their policy
callbacks) are long-lived and shared across runs, so the trigger source
cannot be closed over at construction time. Contextvars propagate to tasks
spawned inside the run, and the default (USER) applies to out-of-run
callers such as connect-time tool discovery.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from jarvis.core.types import TriggerSource

current_trigger_source: ContextVar[TriggerSource] = ContextVar(
    "current_trigger_source", default=TriggerSource.USER
)


@contextmanager
def trigger_scope(source: TriggerSource) -> Iterator[None]:
    token = current_trigger_source.set(source)
    try:
        yield
    finally:
        current_trigger_source.reset(token)
