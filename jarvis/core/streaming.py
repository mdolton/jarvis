"""RunStream — channel-agnostic contract for streaming agent output.

A RunStream is opened per run by the OutputRouter (adapter-specific), fed by
the AgentRunner while the run progresses, and finalized by the dispatcher.
Implementations must be failure-proof: no method may raise into the run —
streaming is best-effort decoration on top of the plain final send.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class RunStream(Protocol):
    """Live output surface for one agent run."""

    delivered: bool
    """True only after finish() successfully placed the final text; the
    caller falls back to a plain adapter send when False."""

    async def update(self, text: str) -> None:
        """Replace the in-progress text (full accumulated text, not a delta)."""
        ...

    async def status(self, label: str | None) -> None:
        """Show (or clear, with None) a transient activity label, e.g. a tool name."""
        ...

    async def finish(self, final_text: str) -> None:
        """Deliver the final text and stop all in-progress affordances."""
        ...

    async def close(self) -> None:
        """Idempotent cleanup; must always stop the typing indicator."""
        ...
