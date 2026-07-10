# Autonomy Traces ("No Silent Autonomy") Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every autonomous (non-user-initiated) Jarvis run leaves a concise "did X because Y" trace: always on the existing audit stream (SSE feed + `/audit` page), and with a provenance marker on Discord when the result is noteworthy enough for the notification gate to admit it.

**Architecture:** A new `autonomy.trace` audit event type, emitted at the two places that already decide where autonomous output goes — `OutputRouter._route_event` (event-triggered runs) and `ScheduledOutputRouter.route` (scheduled runs). Because the `/events/stream` SSE endpoint and `/audit` page simply tail the `audit_events` table, emitting the event makes traces visible with zero new UI/channel work. Discord sends that the gate admits get a terse `⚙️ [source]` provenance prefix so the ping itself says why it happened. No new Discord traffic is created — the trace rides the existing gated send or stays on the audit feed, so the Goal-3 budget is respected by construction.

**Tech Stack:** Python 3.12, SQLAlchemy async + existing `AuditLogger`/`AuditRepo`, pytest (`asyncio_mode = auto`).

## Global Constraints

- Python 3.12 only; ruff line-length 100 (`ruff.toml`).
- Pytest runs with `asyncio_mode = auto` — write `async def test_*` with **no** `@pytest.mark.asyncio`.
- Persistence goes through repositories; audit writes go through `AuditLogger.emit` only.
- Reuse the existing audit stream — no new table, channel, or SSE endpoint.
- Traces must not add unsolicited Discord sends: Discord visibility comes only from sends the `NotificationGate` already admits.
- `make check` (ruff + pytest) must be green before the branch is done.
- Commits carry `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: `AUTONOMY_TRACE` event type + shared trace emitter

**Files:**
- Modify: `jarvis/core/types.py` (add enum member after `OUTPUT_SUPPRESSED`, ~line 51)
- Modify: `jarvis/core/output_router.py` (module-level helper)
- Test: `tests/unit/test_output_router.py`

**Interfaces:**
- Produces: `AuditEventType.AUTONOMY_TRACE = "autonomy.trace"`; `async def emit_autonomy_trace(audit, *, result, source, reason, delivery) -> None` in `jarvis.core.output_router` (audit may be `None` → no-op). Payload keys: `source`, `reason`, `action` (≤300-char single-line summary of `result.final_output`), `delivery`.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_output_router.py`)

```python
class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)


async def test_emit_autonomy_trace_builds_audit_event():
    audit = _RecordingAudit()
    result = _result(kind=ChannelKind.EVENT, ref="email", text="Filed the invoice.")

    await emit_autonomy_trace(
        audit,
        result=result,
        source="event:email",
        reason="inbound 'email' event",
        delivery="digest",
    )

    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.type is AuditEventType.AUTONOMY_TRACE
    assert event.conversation_id == result.conversation_id
    assert event.trigger_id == result.trigger_id
    assert event.payload == {
        "source": "event:email",
        "reason": "inbound 'email' event",
        "action": "Filed the invoice.",
        "delivery": "digest",
    }


async def test_emit_autonomy_trace_without_audit_is_a_noop():
    await emit_autonomy_trace(
        None,
        result=_result(kind=ChannelKind.EVENT, ref="email"),
        source="event:email",
        reason="inbound 'email' event",
        delivery="discord",
    )


async def test_emit_autonomy_trace_summarizes_long_multiline_output():
    audit = _RecordingAudit()
    result = _result(kind=ChannelKind.EVENT, ref="email", text="line one\nline two  " + "x" * 400)

    await emit_autonomy_trace(
        audit,
        result=result,
        source="event:email",
        reason="inbound 'email' event",
        delivery="suppressed",
    )

    action = audit.events[0].payload["action"]
    assert "\n" not in action
    assert action.startswith("line one line two")
    assert len(action) <= 300
    assert action.endswith("…")
```

Also extend the test module's imports:

```python
from jarvis.core.output_router import (
    OutputRouter,
    Priority,
    classify_priority,
    emit_autonomy_trace,
)
from jarvis.core.types import AuditEventType, ChannelKind
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_output_router.py -q`
Expected: ImportError — `emit_autonomy_trace` not defined.

- [ ] **Step 3: Implement**

In `jarvis/core/types.py`, after `OUTPUT_SUPPRESSED = "output.suppressed"`:

```python
    AUTONOMY_TRACE = "autonomy.trace"
```

In `jarvis/core/output_router.py`, extend imports:

```python
from jarvis.audit.logger import AuditLogger
from jarvis.core.types import AuditEvent, AuditEventType, ChannelKind
```

Add below `_DIGEST_ITEM_MAX_CHARS`:

```python
_TRACE_ACTION_MAX_CHARS = 300


def _trace_action(text: str) -> str:
    """Terse single-line 'did X' summary of a run's final output."""
    flat = " ".join(text.split())
    if len(flat) > _TRACE_ACTION_MAX_CHARS:
        return flat[: _TRACE_ACTION_MAX_CHARS - 1] + "…"
    return flat


async def emit_autonomy_trace(
    audit: AuditLogger | None,
    *,
    result: AgentRunResult,
    source: str,
    reason: str,
    delivery: str,
) -> None:
    """Record a 'did X because Y' audit trace for an autonomous run.

    `delivery` says where the run's output actually went: 'discord',
    'digest', 'suppressed', 'dashboard_only', or 'undelivered'. The audit
    SSE feed tails this table, so emitting here is what makes autonomous
    activity visible without spending notification budget.
    """
    if audit is None:
        return
    await audit.emit(
        AuditEvent(
            type=AuditEventType.AUTONOMY_TRACE,
            conversation_id=result.conversation_id,
            trigger_id=result.trigger_id,
            payload={
                "source": source,
                "reason": reason,
                "action": _trace_action(result.final_output),
                "delivery": delivery,
            },
        )
    )
```

Check `jarvis/audit/logger.py` for import cycles: `audit.logger` imports from `jarvis.core.types` and `jarvis.persistence.repositories` only, so `output_router → audit.logger` is safe.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_output_router.py -q`
Expected: PASS (all, including pre-existing).

- [ ] **Step 5: Commit**

```bash
git add jarvis/core/types.py jarvis/core/output_router.py tests/unit/test_output_router.py
git commit -m "feat: autonomy.trace audit event type and shared trace emitter"
```

---

### Task 2: Event-run traces in `OutputRouter._route_event`

**Files:**
- Modify: `jarvis/core/output_router.py` (`OutputRouter.__init__`, `_route_event`)
- Test: `tests/unit/test_output_router.py`

**Interfaces:**
- Consumes: `emit_autonomy_trace(...)` from Task 1.
- Produces: `OutputRouter(adapters=..., notification_gate=..., event_notify_ref=..., audit=None)` — new optional keyword `audit: AuditLogger | None`. Every `_route_event` call emits exactly one trace with `source=f"event:{result.channel_ref}"` and `delivery` ∈ {`discord`, `digest`, `suppressed`, `dashboard_only`}. Admitted Discord sends are prefixed `⚙️ [event:<ref>] `.

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_output_router.py`)

```python
class _FakeGate:
    """admit() mirrors NotificationGate's contract: cleaned text or None."""

    def __init__(self, admit_result) -> None:
        self._admit_result = admit_result
        self.calls: list[dict] = []

    async def admit(self, *, text: str, source: str):
        self.calls.append({"text": text, "source": source})
        return self._admit_result


async def test_admitted_event_sends_with_provenance_and_traces_discord():
    adapter = _RecordingAdapter()
    audit = _RecordingAudit()
    router = OutputRouter(
        adapters=[adapter],
        notification_gate=_FakeGate(admit_result="Filed the invoice."),
        event_notify_ref="user-1",
        audit=audit,
    )

    await router.route(_result(kind=ChannelKind.EVENT, ref="email", text="Filed the invoice."))

    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == "⚙️ [event:email] Filed the invoice."
    assert len(audit.events) == 1
    assert audit.events[0].payload["delivery"] == "discord"
    assert audit.events[0].payload["source"] == "event:email"


async def test_queued_event_traces_digest():
    adapter = _RecordingAdapter()
    audit = _RecordingAudit()
    router = OutputRouter(
        adapters=[adapter],
        notification_gate=_FakeGate(admit_result=None),
        event_notify_ref="user-1",
        audit=audit,
    )

    await router.route(_result(kind=ChannelKind.EVENT, ref="email", text="routine update"))

    assert adapter.sent == []
    assert audit.events[0].payload["delivery"] == "digest"


async def test_silent_event_traces_suppressed_without_consulting_gate():
    adapter = _RecordingAdapter()
    audit = _RecordingAudit()
    gate = _FakeGate(admit_result=None)
    router = OutputRouter(
        adapters=[adapter],
        notification_gate=gate,
        event_notify_ref="user-1",
        audit=audit,
    )

    await router.route(_result(kind=ChannelKind.EVENT, ref="email", text="[SILENT] nothing"))

    assert adapter.sent == []
    assert gate.calls == []
    assert audit.events[0].payload["delivery"] == "suppressed"


async def test_ungated_event_traces_dashboard_only():
    adapter = _RecordingAdapter()
    audit = _RecordingAudit()
    router = OutputRouter(adapters=[adapter], audit=audit)

    await router.route(_result(kind=ChannelKind.EVENT, ref="email", text="hi"))

    assert adapter.sent == []
    assert audit.events[0].payload["delivery"] == "dashboard_only"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_output_router.py -q`
Expected: FAIL — `OutputRouter.__init__` has no `audit` keyword.

- [ ] **Step 3: Implement**

Replace `OutputRouter.__init__` and `_route_event` in `jarvis/core/output_router.py`:

```python
class OutputRouter:
    def __init__(
        self,
        *,
        adapters: Iterable[ChannelAdapter],
        notification_gate: NotificationGate | None = None,
        event_notify_ref: str | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self._by_kind: dict[str, ChannelAdapter] = {a.kind: a for a in adapters}
        self._gate = notification_gate
        self._event_notify_ref = event_notify_ref
        self._audit = audit
```

```python
    async def _route_event(self, result: AgentRunResult) -> None:
        """Proactive delivery for event-triggered runs, subject to the gate.

        Without a gate, a notify target, and a Discord adapter, event results
        stay dashboard-only (the pre-gate behavior). Every path emits an
        autonomy trace so the run is never silent (goal: no silent autonomy).
        """
        source = f"event:{result.channel_ref}"
        reason = f"inbound '{result.channel_ref}' event"

        async def _trace(delivery: str) -> None:
            await emit_autonomy_trace(
                self._audit, result=result, source=source, reason=reason, delivery=delivery
            )

        adapter = self._by_kind.get(ChannelKind.DISCORD.value)
        if self._gate is None or self._event_notify_ref is None or adapter is None:
            await _trace("dashboard_only")
            return
        priority, _ = classify_priority(result.final_output)
        if priority is None:
            await _trace("suppressed")
            return
        text = await self._gate.admit(text=result.final_output, source=source)
        if text is None:
            await _trace("digest")
            return
        await adapter.send(
            OutboundMessage(
                channel_kind=ChannelKind.DISCORD,
                channel_ref=self._event_notify_ref,
                text=f"⚙️ [{source}] {text}",
            )
        )
        await _trace("discord")
```

Note the behavior-preserving refactor: the adapter lookup moves above the gate check so the misconfigured cases collapse into one `dashboard_only` trace; `[SILENT]` is now classified before `admit` (the gate would have dropped it anyway — no rows were written for SILENT before, and none are now).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_output_router.py tests/integration/test_notification_gate.py -q`
Expected: PASS. `test_notification_gate.py` exercises the real gate through `_route_event`; its assertions on adapter sends must still hold — the provenance prefix only affects tests asserting exact sent text, which live in that file (`test_p1_delivers_immediately_even_when_budget_exhausted`, etc.). If any assert exact text, update them to expect the `⚙️ [event:<ref>] ` prefix — that changed behavior is the point of this task.

- [ ] **Step 5: Commit**

```bash
git add jarvis/core/output_router.py tests/unit/test_output_router.py tests/integration/test_notification_gate.py
git commit -m "feat: emit autonomy trace for every event-triggered run"
```

---

### Task 3: Scheduled-run traces in `ScheduledOutputRouter.route`

**Files:**
- Modify: `jarvis/scheduler/scheduled_output.py`
- Test: `tests/unit/test_scheduled_output.py`

**Interfaces:**
- Consumes: `emit_autonomy_trace` from Task 1 (already importable from `jarvis.core.output_router`, which `scheduled_output.py` imports today).
- Produces: `ScheduledOutputRouter(discord_adapter=..., notification_gate=None, audit=None)` — new optional keyword `audit: AuditLogger | None`. Every `route()` call emits exactly one trace with the caller-provided `source` (e.g. `schedule:morning-brief`) and `delivery` ∈ {`discord`, `digest`, `suppressed`, `dashboard_only`, `undelivered`}. `discord_if_noteworthy` sends get the `⚙️ [<source>] ` prefix; plain `discord` digests do not (they are the expected, user-configured message).

- [ ] **Step 1: Write the failing tests** (append to `tests/unit/test_scheduled_output.py`)

```python
class _RecordingAudit:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event) -> None:
        self.events.append(event)


def _trace_payloads(audit: _RecordingAudit) -> list[dict]:
    return [e.payload for e in audit.events if e.type is AuditEventType.AUTONOMY_TRACE]


async def test_discord_mode_traces_discord_delivery():
    adapter = _RecordingAdapter()
    audit = _RecordingAudit()
    router = ScheduledOutputRouter(discord_adapter=adapter, audit=audit)

    await router.route(
        result=_result(text="morning brief"),
        output_mode="discord",
        discord_user_id="111",
        source="schedule:morning-brief",
    )

    assert adapter.sent[0].text == "morning brief"  # expected digest: no prefix
    payloads = _trace_payloads(audit)
    assert payloads == [
        {
            "source": "schedule:morning-brief",
            "reason": "scheduled run (schedule:morning-brief)",
            "action": "morning brief",
            "delivery": "discord",
        }
    ]


async def test_dashboard_only_mode_traces_dashboard_only():
    audit = _RecordingAudit()
    router = ScheduledOutputRouter(discord_adapter=_RecordingAdapter(), audit=audit)

    await router.route(
        result=_result(text="some data"),
        output_mode="dashboard_only",
        discord_user_id="111",
        source="schedule:s1",
    )

    assert _trace_payloads(audit)[0]["delivery"] == "dashboard_only"


async def test_noteworthy_send_gets_provenance_prefix_and_discord_trace():
    adapter = _RecordingAdapter()
    audit = _RecordingAudit()
    router = ScheduledOutputRouter(discord_adapter=adapter, audit=audit)

    await router.route(
        result=_result(text="[NOTEWORTHY] server is down"),
        output_mode="discord_if_noteworthy",
        discord_user_id="111",
        source="schedule:watchdog",
    )

    assert adapter.sent[0].text == "⚙️ [schedule:watchdog] server is down"
    assert _trace_payloads(audit)[0]["delivery"] == "discord"


async def test_silent_noteworthy_result_traces_suppressed():
    adapter = _RecordingAdapter()
    audit = _RecordingAudit()
    router = ScheduledOutputRouter(discord_adapter=adapter, audit=audit)

    await router.route(
        result=_result(text="[SILENT] all quiet"),
        output_mode="discord_if_noteworthy",
        discord_user_id="111",
        source="schedule:watchdog",
    )

    assert adapter.sent == []
    assert _trace_payloads(audit)[0]["delivery"] == "suppressed"


async def test_undeliverable_send_traces_undelivered():
    audit = _RecordingAudit()
    router = ScheduledOutputRouter(discord_adapter=None, audit=audit)

    await router.route(
        result=_result(text="morning brief"),
        output_mode="discord",
        discord_user_id="111",
        source="schedule:s1",
    )

    assert _trace_payloads(audit)[0]["delivery"] == "undelivered"


async def test_unknown_output_mode_traces_dashboard_only():
    audit = _RecordingAudit()
    router = ScheduledOutputRouter(discord_adapter=_RecordingAdapter(), audit=audit)

    await router.route(
        result=_result(text="x"),
        output_mode="bogus",
        discord_user_id="111",
        source="schedule:s1",
    )

    assert _trace_payloads(audit)[0]["delivery"] == "dashboard_only"
```

Add the import at the top of the test module:

```python
from jarvis.core.types import AuditEventType, ChannelKind
```

Also check the gate-queued path in the existing integration file (`tests/integration/test_notification_gate.py::test_noteworthy_scheduled_send_is_gated_over_budget`) still passes — over-budget noteworthy sends must trace `digest` (assert added there in step 3 if convenient, otherwise the unit tests above suffice).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_scheduled_output.py -q`
Expected: FAIL — `ScheduledOutputRouter.__init__` has no `audit` keyword.

- [ ] **Step 3: Implement**

Rewrite `jarvis/scheduler/scheduled_output.py`'s class (docstring stays, plus a line noting the autonomy trace):

```python
from jarvis.audit.logger import AuditLogger
from jarvis.core.output_router import NotificationGate, emit_autonomy_trace


class ScheduledOutputRouter:
    def __init__(
        self,
        *,
        discord_adapter: ChannelAdapter | None,
        notification_gate: NotificationGate | None = None,
        audit: AuditLogger | None = None,
    ) -> None:
        self._discord = discord_adapter
        self._gate = notification_gate
        self._audit = audit

    async def route(
        self,
        *,
        result: AgentRunResult,
        output_mode: str,
        discord_user_id: str,
        source: str = "scheduled",
    ) -> None:
        delivery = await self._route(
            result=result,
            output_mode=output_mode,
            discord_user_id=discord_user_id,
            source=source,
        )
        await emit_autonomy_trace(
            self._audit,
            result=result,
            source=source,
            reason=f"scheduled run ({source})",
            delivery=delivery,
        )

    async def _route(
        self,
        *,
        result: AgentRunResult,
        output_mode: str,
        discord_user_id: str,
        source: str,
    ) -> str:
        """Route per output_mode; return the delivery outcome for the trace."""
        if output_mode == "dashboard_only":
            return "dashboard_only"

        if output_mode == "discord_if_noteworthy":
            text = result.final_output
            upper = text.lstrip().upper()
            if upper.startswith("[SILENT]"):
                return "suppressed"
            if upper.startswith("[NOTEWORTHY]"):
                text = text.lstrip()
                text = text[len("[NOTEWORTHY]") :].lstrip()
            if not self._deliverable(discord_user_id):
                await self._send_discord(text, discord_user_id)  # keep the warnings
                return "undelivered"
            # Consult the gate only when the send can actually happen —
            # otherwise budget would be spent on a message that never leaves.
            if self._gate is not None:
                admitted = await self._gate.admit(text=text, source=source)
                if admitted is None:
                    return "digest"
                text = admitted
            await self._send_discord(f"⚙️ [{source}] {text}", discord_user_id)
            return "discord"

        if output_mode == "discord":
            text = result.final_output
            if not self._deliverable(discord_user_id):
                await self._send_discord(text, discord_user_id)  # keep the warnings
                return "undelivered"
            # Drain only when the send can actually happen — claiming queued
            # notifications for a message that never leaves would lose them.
            if self._gate is not None:
                section = await self._gate.drain_digest_section()
                if section is not None:
                    text = f"{text}\n\n{section}"
            await self._send_discord(text, discord_user_id)
            return "discord"

        _log.warning("unknown output_mode %r; treating as dashboard_only", output_mode)
        return "dashboard_only"
```

`_deliverable`, `send_error`, and `_send_discord` are unchanged. Behavior notes: the gate/drain guards previously read `self._gate is not None and self._deliverable(...)`; the undeliverable case now short-circuits earlier with the same net effect (no gate spend, no drain, warning still logged by `_send_discord`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_scheduled_output.py tests/integration/test_notification_gate.py tests/integration/test_scheduler.py -q`
Expected: PASS. If `test_notification_gate.py` asserts exact sent text for noteworthy scheduled sends, update those asserts for the `⚙️ [<source>] ` prefix.

- [ ] **Step 5: Commit**

```bash
git add jarvis/scheduler/scheduled_output.py tests/unit/test_scheduled_output.py tests/integration/test_notification_gate.py
git commit -m "feat: emit autonomy trace for every scheduled run"
```

---

### Task 4: Wire audit into the production routers + end-to-end integration test

**Files:**
- Modify: `jarvis/main.py:208` (OutputRouter construction)
- Modify: `jarvis/scheduler/scheduler.py:86` (ScheduledOutputRouter construction)
- Test: `tests/integration/test_autonomy_trace.py` (new)

**Interfaces:**
- Consumes: `OutputRouter(audit=...)` from Task 2, `ScheduledOutputRouter(audit=...)` from Task 3.
- Produces: production wiring — every event-triggered and scheduled run writes an `autonomy.trace` row, which the `/events/stream` SSE endpoint and `/audit` page pick up automatically. (`ActionService`'s `ScheduledOutputRouter` stays unwired: resumed actions follow an explicit user approval, so they are not silent autonomy.)

- [ ] **Step 1: Write the failing integration test**

Create `tests/integration/test_autonomy_trace.py`. Model the fixture on `tests/integration/test_notification_gate.py` (real SQLite factory + real `NotificationGate`); copy its `factory` fixture pattern and `_RecordingAdapter`. The point of this test over the unit ones: a real `AuditLogger` flush cycle lands the trace in the `audit_events` table where the SSE feed reads it.

```python
"""End-to-end: an event-triggered run leaves an autonomy.trace audit row."""

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from jarvis.agents.runner import AgentRunResult
from jarvis.audit.logger import AuditLogger
from jarvis.channels.base import OutboundMessage
from jarvis.core.output_router import NotificationGate, OutputRouter
from jarvis.core.types import AuditEventType, ChannelKind
from jarvis.persistence.db import Base
from jarvis.persistence.repositories import AuditRepo


class _RecordingAdapter:
    kind = ChannelKind.DISCORD.value

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def start(self, dispatcher) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


@pytest.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _event_result(text: str) -> AgentRunResult:
    return AgentRunResult(
        final_output=text,
        conversation_id=None,
        trigger_id=None,
        channel_kind=ChannelKind.EVENT,
        channel_ref="email",
    )


async def test_event_run_lands_autonomy_trace_in_audit_table(factory):
    audit = AuditLogger(session_factory=factory)
    await audit.start()
    adapter = _RecordingAdapter()
    router = OutputRouter(
        adapters=[adapter],
        notification_gate=NotificationGate(session_factory=factory, daily_budget=5),
        event_notify_ref="user-1",
        audit=audit,
    )

    await router.route(_event_result("[P4] filed a receipt"))  # queued, not sent
    await audit.stop()  # drains the queue → row is flushed

    assert adapter.sent == []
    async with factory() as session:
        rows = await AuditRepo(session).recent(types=[AuditEventType.AUTONOMY_TRACE], limit=10)
    assert len(rows) == 1
    assert rows[0].payload["delivery"] == "digest"
    assert rows[0].payload["source"] == "event:email"
```

Before finalizing, cross-check the `factory` fixture and `AuditRepo.recent` signature against `tests/integration/test_notification_gate.py` and `tests/integration/test_audit_logger.py` — reuse their exact idioms (e.g. if the gate test file builds the schema differently, copy that).

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/integration/test_autonomy_trace.py -q`
Expected: with Tasks 1–2 done this passes already — that is acceptable; it is a wiring-shaped regression test. If it passes, continue (the failing/passing gate for this task is the smoke test in step 4).

- [ ] **Step 3: Wire production construction sites**

`jarvis/main.py` (~line 208):

```python
    output_router = OutputRouter(
        adapters=channel_adapters,
        notification_gate=notification_gate,
        event_notify_ref=event_notify_ref,
        audit=audit,
    )
```

`jarvis/scheduler/scheduler.py` (~line 86):

```python
        self._output_router = ScheduledOutputRouter(
            discord_adapter=discord_adapter,
            notification_gate=notification_gate,
            audit=audit,
        )
```

(`audit` is already a constructor parameter of `Scheduler`; use it before assigning `self._audit` or reorder so `self._audit = audit` comes first — either way, pass the same `audit` object.)

- [ ] **Step 4: Run integration smoke + full suite**

Run: `uv run pytest tests/integration/test_autonomy_trace.py tests/integration/test_main_smoke.py tests/integration/test_scheduler.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add jarvis/main.py jarvis/scheduler/scheduler.py tests/integration/test_autonomy_trace.py
git commit -m "feat: wire autonomy traces into production routers"
```

---

### Task 5: Full verification + PR

**Files:**
- None new; whole branch.

- [ ] **Step 1: Run the full gate**

Run: `make check` (ruff + full pytest)
Expected: lint clean, all tests pass. Use `uv run pytest -q 2>&1 | tail -5` for the summary if needed.

- [ ] **Step 2: Verify the done-criteria against the goal**

- Every event-triggered autonomous action produces a visible after-the-fact trace → `_route_event` emits on all four paths (Task 2 tests).
- Traces respect the notification budget → traces are audit rows, never new Discord sends; Discord visibility piggybacks only on gate-admitted sends (Tasks 2–3).
- `make check` green → step 1.

- [ ] **Step 3: Open PR**

```bash
git push -u origin feat/autonomy-traces
gh pr create --title "feat: autonomy traces — no silent autonomy" --body "..."
```

PR body: summarize the trace event, delivery outcomes, provenance prefix, and budget interaction; note `ActionService` deliberately unwired (user-approved resumptions aren't silent). End with the standard generated-with footer.
