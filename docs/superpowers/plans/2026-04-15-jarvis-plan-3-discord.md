# Jarvis Plan 3 — Discord Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Jarvis as a long-lived process that connects to Discord, accepts DMs from allow-listed users, dispatches them through the agent pipeline, and replies in the same DM thread. At the end of this plan, `python -m jarvis serve` (a new CLI subcommand) connects to Discord and stays online.

**Architecture:** A `ChannelAdapter` protocol (formalizing what the dispatcher already informally expects), a `DiscordAdapter` implementation backed by `discord.py`, and an `OutputRouter` that sends agent replies back through the originating channel adapter. `bootstrap()` starts and stops adapters as part of `AppContext` lifecycle. CLI gains a `serve` command that bootstraps and waits for shutdown signals.

**Tech Stack:** `discord.py>=2.3` (async, Gateway-based; integrates with our event loop via `await client.start(token)`). Existing Plan 1+2 stack unchanged.

**Design spec this plan implements:** `docs/superpowers/specs/2026-04-14-jarvis-agent-service-design.md` — sections covered: §5.3 DiscordAdapter, §5.4 ChannelAdapter protocol, §5.10 OutputRouter (Discord path; dashboard path is Plan 5), §6.1 Discord flow end-to-end.

**Plan 2 followups addressed here:**
- `MCPToolRepo.replace_for_server` preserves `policy_override` across reconnects.
- `ChannelAdapter` protocol defined; `allowed_refs` resolution clarified (adapter owns it, reads from config).

---

## File Structure

New modules:

```
jarvis/
  channels/
    base.py             # ChannelAdapter protocol + OutboundMessage type
    discord_adapter.py  # DiscordAdapter implements ChannelAdapter via discord.py
  core/
    output_router.py    # OutputRouter routes AgentRunResult → originating channel
```

Files modified:
- `jarvis/persistence/repositories.py` — `MCPToolRepo.replace_for_server` preserves `policy_override`.
- `jarvis/agents/runner.py` — `AgentRunner.run` returns enough info for `OutputRouter` to know where to send the reply (already has `conversation_id`; needs `channel_kind` + `channel_ref` on the result).
- `jarvis/main.py` — `bootstrap()` starts adapters; `AppContext` carries the adapter list and `OutputRouter`.
- `jarvis/cli.py` — add `serve` command.
- `pyproject.toml` — add `discord.py` dep.
- `tests/conftest.py` — extend the autouse cleanup to also reset any installed channel adapters between tests.

---

## Task 1: Add `discord.py` dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Edit `pyproject.toml`**

In the `[project]` `dependencies` list, add `discord.py>=2.3` (keep existing entries):

```toml
dependencies = [
  "pydantic>=2.7",
  "sqlalchemy[asyncio]>=2.0.30",
  "aiosqlite>=0.20",
  "alembic>=1.13",
  "pyyaml>=6.0",
  "watchfiles>=0.22",
  "openai-agents>=0.7",
  "openai>=1.68",
  "typer>=0.12",
  "discord.py>=2.3",
]
```

- [ ] **Step 2: Sync dependencies**

Run: `uv sync`

Expected: resolves and installs `discord.py` plus its transitive deps (aiohttp, etc.).

- [ ] **Step 3: Verify imports work**

Run:
```bash
uv run python -c "
import discord
from discord import Client, Intents, Message
print('discord.py version:', discord.__version__)
print('imports OK')
"
```

Expected: a version >= 2.3.x and "imports OK".

- [ ] **Step 4: Run full suite**

Run: `uv run pytest`
Expected: 90 passed (no regression).

- [ ] **Step 5: ruff**

Run: `uv run ruff check . && uv run ruff format --check .`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "add discord.py dependency"
```

---

## Task 2: Preserve `policy_override` across MCP reconnects

Plan 2 followup (I-1 in the end-of-plan review). `MCPToolRepo.replace_for_server` currently nukes user-set policy overrides on every reconnect — fine today (no writers exist), but a latent bug for Plan 5's dashboard. Fix it before Plan 3's reconnect-prone runtime exposes the bug under load.

**Files:**
- Modify: `jarvis/persistence/repositories.py`
- Modify: `tests/integration/test_repositories_mcp_settings.py` (add one test)

- [ ] **Step 1: Write failing test**

Append to `tests/integration/test_repositories_mcp_settings.py`:

```python


async def test_mcp_tool_replace_preserves_policy_override(session):
    srepo = MCPServerRepo(session)
    trepo = MCPToolRepo(session)
    server = await srepo.upsert(name="gcal", transport="stdio")

    # Initial replace establishes the tool with no override.
    await trepo.replace_for_server(
        server.id,
        tools=[
            MCPToolDescriptor(
                name="list_events",
                input_schema={},
                read_only_hint=True,
                destructive_hint=False,
            ),
        ],
    )

    # Simulate the user setting an override directly in the DB.
    from sqlalchemy import update

    from jarvis.persistence.models import MCPToolRow

    await session.execute(
        update(MCPToolRow)
        .where(MCPToolRow.server_id == server.id, MCPToolRow.name == "list_events")
        .values(policy_override="confirm")
    )
    await session.commit()

    # Reconnect — replace_for_server with the same descriptor should preserve
    # the override (because nothing about the tool definition itself changed).
    await trepo.replace_for_server(
        server.id,
        tools=[
            MCPToolDescriptor(
                name="list_events",
                input_schema={},
                read_only_hint=True,
                destructive_hint=False,
            ),
        ],
    )

    rows = await trepo.list_for_server(server.id)
    assert len(rows) == 1
    assert rows[0].policy_override == "confirm"


async def test_mcp_tool_replace_drops_policy_when_tool_disappears(session):
    """If the server stops advertising a tool, the override goes with it."""
    srepo = MCPServerRepo(session)
    trepo = MCPToolRepo(session)
    server = await srepo.upsert(name="gcal", transport="stdio")

    await trepo.replace_for_server(
        server.id,
        tools=[
            MCPToolDescriptor(name="old_tool", input_schema={}),
        ],
    )

    from sqlalchemy import update

    from jarvis.persistence.models import MCPToolRow

    await session.execute(
        update(MCPToolRow)
        .where(MCPToolRow.server_id == server.id, MCPToolRow.name == "old_tool")
        .values(policy_override="auto")
    )
    await session.commit()

    # Replace with a different tool — old_tool's row (and its override) is gone.
    await trepo.replace_for_server(
        server.id,
        tools=[
            MCPToolDescriptor(name="new_tool", input_schema={}),
        ],
    )

    rows = await trepo.list_for_server(server.id)
    assert {r.name for r in rows} == {"new_tool"}
    assert rows[0].policy_override is None
```

- [ ] **Step 2: Run — verify the first new test fails**

Run: `uv run pytest tests/integration/test_repositories_mcp_settings.py::test_mcp_tool_replace_preserves_policy_override -v`
Expected: fails — `policy_override` is `None` after replace because the current code wipes it.

- [ ] **Step 3: Update `MCPToolRepo.replace_for_server`**

In `jarvis/persistence/repositories.py`, replace the `MCPToolRepo.replace_for_server` method body with this version:

```python
    async def replace_for_server(
        self,
        server_id: UUID,
        *,
        tools: list[MCPToolDescriptor],
    ) -> None:
        """Replace the tool set for a server atomically (full overwrite),
        preserving per-tool `policy_override` user-set state across the swap.
        """
        # Snapshot existing overrides keyed by tool name so we can re-apply
        # them after the delete-then-insert.
        existing = await self._session.execute(
            select(MCPToolRow).where(MCPToolRow.server_id == server_id)
        )
        existing_rows = list(existing.scalars())
        overrides: dict[str, str] = {
            r.name: r.policy_override
            for r in existing_rows
            if r.policy_override is not None
        }

        for row in existing_rows:
            await self._session.delete(row)

        for tool in tools:
            self._session.add(
                MCPToolRow(
                    server_id=server_id,
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    read_only_hint=tool.read_only_hint,
                    destructive_hint=tool.destructive_hint,
                    policy_override=overrides.get(tool.name),
                )
            )
        await self._session.commit()
```

The change: snapshot `{name → policy_override}` before the delete; re-apply for tools that still exist after the swap. Tools that the server stopped advertising have their overrides discarded along with the row.

- [ ] **Step 4: Run both new tests — verify pass**

Run: `uv run pytest tests/integration/test_repositories_mcp_settings.py -v`
Expected: 6 passed (4 existing + 2 new).

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 92 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/persistence/repositories.py tests/integration/test_repositories_mcp_settings.py
git commit -m "preserve policy_override across MCPToolRepo.replace_for_server"
```

---

## Task 3: `ChannelAdapter` protocol + `OutboundMessage` type

Spec §5.4. Adapter contract + the message envelope that `OutputRouter` hands to it.

**Files:**
- Create: `jarvis/channels/base.py`
- Create: `tests/unit/test_channel_base.py`

- [ ] **Step 1: Write failing test**

Write `tests/unit/test_channel_base.py`:

```python
import pytest
from pydantic import ValidationError

from jarvis.channels.base import ChannelAdapter, OutboundMessage
from jarvis.core.types import ChannelKind


def test_outbound_message_minimal():
    msg = OutboundMessage(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        text="hello",
    )
    assert msg.text == "hello"


def test_outbound_message_rejects_extra_fields():
    with pytest.raises(ValidationError):
        OutboundMessage(
            channel_kind=ChannelKind.DISCORD,
            channel_ref="u",
            text="t",
            extra="nope",  # type: ignore[call-arg]
        )


def test_channel_adapter_is_a_protocol():
    """Sanity: ChannelAdapter is a Protocol — anything with the required
    members satisfies it without explicit inheritance.
    """
    import typing

    assert typing.get_origin(typing.runtime_checkable(ChannelAdapter)) is None
    # Must declare these attributes/methods as part of the protocol surface.
    assert hasattr(ChannelAdapter, "kind")
    assert hasattr(ChannelAdapter, "start")
    assert hasattr(ChannelAdapter, "stop")
    assert hasattr(ChannelAdapter, "send")


async def test_protocol_is_satisfied_by_a_minimal_class():
    """Define a minimal class that satisfies the protocol, instantiate it,
    and call its methods. This catches signature mismatches at test time.
    """

    class _NoopAdapter:
        kind = "noop"

        async def start(self, dispatcher) -> None:  # noqa: ARG002
            return None

        async def stop(self) -> None:
            return None

        async def send(self, msg: OutboundMessage) -> None:  # noqa: ARG002
            return None

    adapter: ChannelAdapter = _NoopAdapter()  # type-check: structural match
    await adapter.start(dispatcher=None)
    await adapter.send(
        OutboundMessage(
            channel_kind=ChannelKind.DISCORD,
            channel_ref="u",
            text="t",
        )
    )
    await adapter.stop()
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/unit/test_channel_base.py -v`
Expected: `ModuleNotFoundError` on `jarvis.channels.base`.

- [ ] **Step 3: Write `jarvis/channels/base.py`**

```python
"""ChannelAdapter protocol + OutboundMessage envelope.

A ChannelAdapter is the bridge between an external chat platform (Discord,
Slack, etc.) and Jarvis's TriggerDispatcher. Adapters:
  - Subscribe to inbound events from their platform.
  - Filter to allow-listed senders (the adapter owns its own allow-list,
    typically read from config — keeps the dispatcher channel-agnostic).
  - Build a ChannelMessage and call dispatcher.dispatch_channel_message().
  - Receive outbound messages via send() and deliver them to the platform.

This module is pure type / protocol definitions; no I/O.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from jarvis.core.types import ChannelKind


class OutboundMessage(BaseModel):
    """A reply Jarvis wants delivered through a channel adapter.

    `channel_ref` is the platform-specific destination (Discord user ID,
    Slack channel ID, etc.) — opaque to everything except the adapter that
    produced the originating ChannelMessage.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel_kind: ChannelKind
    channel_ref: str
    text: str


@runtime_checkable
class ChannelAdapter(Protocol):
    """The contract every channel implementation satisfies.

    Lifecycle:
      - start(dispatcher): connect to the platform and begin pushing
        inbound events into the dispatcher. Must be safe to await.
      - stop(): disconnect cleanly. Must be idempotent — bootstrap may
        call stop() during partial-failure cleanup.
      - send(msg): deliver `msg` through the platform. Must accept any
        OutboundMessage whose channel_kind == self.kind.
    """

    kind: str  # ChannelKind value as a string

    async def start(self, dispatcher) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, msg: OutboundMessage) -> None: ...
```

Note on `dispatcher` parameter typing: avoiding `TriggerDispatcher` import here keeps `channels/base.py` free of dependencies on `core/`. The protocol method takes `dispatcher` untyped; concrete adapters import the real type.

- [ ] **Step 4: Run new tests — verify pass**

Run: `uv run pytest tests/unit/test_channel_base.py -v`
Expected: 4 passed.

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 96 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/channels/base.py tests/unit/test_channel_base.py
git commit -m "add ChannelAdapter protocol and OutboundMessage type"
```

---

## Task 4: Extend `AgentRunResult` with channel routing info

`OutputRouter` (Task 5) needs to know which channel kind / ref to send the reply to. The `AgentRunner` already knows this when extracting from the trigger — it just doesn't surface it on `AgentRunResult` today.

**Files:**
- Modify: `jarvis/agents/runner.py`
- Modify: `tests/integration/test_agent_runner.py` (extend assertions)

- [ ] **Step 1: Update the existing test to assert on new fields**

In `tests/integration/test_agent_runner.py`, find `test_agent_runner_persists_user_and_assistant_messages`. Add these assertions at the end of the test:

```python
    # Routing fields are populated for the OutputRouter (Task 5).
    assert result.channel_kind == ChannelKind.DASHBOARD
    assert result.channel_ref == "mark"
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_agent_runner.py::test_agent_runner_persists_user_and_assistant_messages -v`
Expected: fails — `AgentRunResult` has no `channel_kind` field.

- [ ] **Step 3: Add the fields to `AgentRunResult` and populate them**

In `jarvis/agents/runner.py`, find the `AgentRunResult` dataclass (near the top of the file). Replace it with:

```python
@dataclass(slots=True)
class AgentRunResult:
    final_output: str
    conversation_id: UUID
    trigger_id: UUID
    channel_kind: ChannelKind
    channel_ref: str
```

Add `from uuid import UUID` and ensure `from jarvis.core.types import ChannelKind` is in the imports (`ChannelKind` is already used elsewhere in this file).

Then update `AgentRunner.run` to set the new fields when constructing the result. Find the `return AgentRunResult(...)` call at the end of `run` and replace with:

```python
        return AgentRunResult(
            final_output=final_text,
            conversation_id=conv_id,
            trigger_id=trigger_id,
            channel_kind=channel_kind,
            channel_ref=channel_ref,
        )
```

`channel_kind` and `channel_ref` are already extracted from the trigger at the top of `run` via `_extract_from_trigger` — they're available as locals.

- [ ] **Step 4: Run tests — verify pass**

Run: `uv run pytest tests/integration/test_agent_runner.py -v`
Expected: 2 passed.

Also update the CLI fake-result construction in `tests/integration/test_cli.py`. Find the `_fake_run` function in `test_invoke_requires_config_and_db` and update:

```python
    async def _fake_run(self, request):
        from jarvis.core.types import ChannelKind
        from uuid import uuid4
        return runner_mod.AgentRunResult(
            final_output="FAKE-CLI-OUTPUT",
            conversation_id=uuid4(),
            trigger_id=uuid4(),
            channel_kind=ChannelKind.DASHBOARD,
            channel_ref="cli",
        )
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/integration/test_cli.py -v`
Expected: 2 passed.

- [ ] **Step 6: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 96 passed, clean.

- [ ] **Step 7: Commit**

```bash
git add jarvis/agents/runner.py tests/integration/test_agent_runner.py tests/integration/test_cli.py
git commit -m "expose channel_kind and channel_ref on AgentRunResult for routing"
```

---

## Task 5: `OutputRouter`

Routes an `AgentRunResult` to the originating channel adapter (or skips for dashboard/manual triggers, where the CLI/web layer prints the reply itself).

**Files:**
- Create: `jarvis/core/output_router.py`
- Create: `tests/unit/test_output_router.py`

- [ ] **Step 1: Write failing tests**

Write `tests/unit/test_output_router.py`:

```python
from uuid import uuid4

import pytest

from jarvis.agents.runner import AgentRunResult
from jarvis.channels.base import OutboundMessage
from jarvis.core.output_router import OutputRouter
from jarvis.core.types import ChannelKind


class _RecordingAdapter:
    kind = ChannelKind.DISCORD.value

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def start(self, dispatcher) -> None:  # noqa: ARG002
        return None

    async def stop(self) -> None:
        return None

    async def send(self, msg: OutboundMessage) -> None:
        self.sent.append(msg)


def _result(*, kind: ChannelKind, ref: str, text: str = "reply") -> AgentRunResult:
    return AgentRunResult(
        final_output=text,
        conversation_id=uuid4(),
        trigger_id=uuid4(),
        channel_kind=kind,
        channel_ref=ref,
    )


async def test_routes_discord_result_to_discord_adapter():
    adapter = _RecordingAdapter()
    router = OutputRouter(adapters=[adapter])

    await router.route(_result(kind=ChannelKind.DISCORD, ref="user-1", text="hi"))

    assert len(adapter.sent) == 1
    assert adapter.sent[0].channel_ref == "user-1"
    assert adapter.sent[0].text == "hi"


async def test_dashboard_result_is_silently_skipped():
    """ManualTrigger / dashboard runs print their own output (CLI / web).
    The router has nowhere to send and must not raise."""
    adapter = _RecordingAdapter()
    router = OutputRouter(adapters=[adapter])

    await router.route(_result(kind=ChannelKind.DASHBOARD, ref="cli"))

    assert adapter.sent == []


async def test_no_adapter_for_kind_raises():
    """If a Discord-triggered run has no Discord adapter wired, that's a
    misconfiguration we want to surface, not silently swallow."""
    router = OutputRouter(adapters=[])

    with pytest.raises(LookupError, match="discord"):
        await router.route(_result(kind=ChannelKind.DISCORD, ref="u"))


async def test_empty_text_is_still_sent():
    """A trivially empty agent reply is still delivered — let the user see
    'no reply' rather than swallow it."""
    adapter = _RecordingAdapter()
    router = OutputRouter(adapters=[adapter])

    await router.route(_result(kind=ChannelKind.DISCORD, ref="u", text=""))

    assert len(adapter.sent) == 1
    assert adapter.sent[0].text == ""
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/unit/test_output_router.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `jarvis/core/output_router.py`**

```python
"""OutputRouter — sends agent results to the originating channel.

The router holds a list of channel adapters and dispatches by channel_kind.
Dashboard / manual triggers (where the CLI or web app prints the reply
itself) are explicitly no-ops — the router has nowhere to send and must
not raise.

A Discord-kind result with no Discord adapter wired is a misconfiguration:
the run produced output that the user expected to receive, and silently
dropping it is worse than raising. We surface a LookupError so the bug is
visible in the audit log.
"""

from collections.abc import Iterable

from jarvis.agents.runner import AgentRunResult
from jarvis.channels.base import ChannelAdapter, OutboundMessage
from jarvis.core.types import ChannelKind

# Channel kinds whose results don't go through an adapter — the producing
# UI (CLI / dashboard) renders the reply directly.
_INTERNAL_KINDS: frozenset[ChannelKind] = frozenset({
    ChannelKind.DASHBOARD,
    ChannelKind.SCHEDULED,  # scheduled runs route per their own output_mode (Plan 4)
})


class OutputRouter:
    def __init__(self, *, adapters: Iterable[ChannelAdapter]) -> None:
        self._by_kind: dict[str, ChannelAdapter] = {a.kind: a for a in adapters}

    async def route(self, result: AgentRunResult) -> None:
        if result.channel_kind in _INTERNAL_KINDS:
            return
        adapter = self._by_kind.get(result.channel_kind.value)
        if adapter is None:
            raise LookupError(
                f"no channel adapter registered for kind {result.channel_kind.value!r}"
            )
        await adapter.send(
            OutboundMessage(
                channel_kind=result.channel_kind,
                channel_ref=result.channel_ref,
                text=result.final_output,
            )
        )
```

- [ ] **Step 4: Run new tests**

Run: `uv run pytest tests/unit/test_output_router.py -v`
Expected: 4 passed.

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 100 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/core/output_router.py tests/unit/test_output_router.py
git commit -m "add OutputRouter dispatching results to channel adapters"
```

---

## Task 6: `DiscordAdapter` — message receive path

Implements `ChannelAdapter` using `discord.py`. This task focuses on the receive side: connecting, filtering DMs from allow-listed users, and pushing them into the dispatcher. Sending replies comes in Task 7.

**Files:**
- Create: `jarvis/channels/discord_adapter.py`
- Create: `tests/integration/test_discord_adapter_receive.py`

The integration test does NOT actually connect to Discord — it patches the `discord.Client` to inject synthetic messages and asserts the adapter dispatches them correctly.

- [ ] **Step 1: Write failing test**

Write `tests/integration/test_discord_adapter_receive.py`:

```python
"""DiscordAdapter receive-path tests using a stubbed discord.Client."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis.channels.discord_adapter import DiscordAdapter
from jarvis.core.types import ChannelKind


class _StubDispatcher:
    """Captures dispatch_channel_message calls."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def dispatch_channel_message(self, msg, *, allowed_refs):
        self.calls.append((msg, allowed_refs))
        return None  # we only care about the call, not the result


def _make_dm_message(*, content: str, author_id: int, message_id: int) -> MagicMock:
    """Build a discord.Message stand-in with the fields the adapter reads."""
    msg = MagicMock()
    msg.content = content
    msg.id = message_id
    msg.author = MagicMock()
    msg.author.id = author_id
    msg.author.bot = False
    # DM channel: discord.DMChannel — we just need the channel.type check to pass.
    import discord

    msg.channel = MagicMock(spec=discord.DMChannel)
    return msg


async def test_dm_from_allowed_user_dispatches():
    dispatcher = _StubDispatcher()
    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})
    # Don't actually start the discord client; call the message handler directly.
    adapter._dispatcher = dispatcher  # injected by start() in production

    msg = _make_dm_message(content="hello", author_id=111, message_id=42)
    await adapter._on_message(msg)

    assert len(dispatcher.calls) == 1
    channel_msg, allowed_refs = dispatcher.calls[0]
    assert channel_msg.channel_kind == ChannelKind.DISCORD
    assert channel_msg.channel_ref == "111"
    assert channel_msg.text == "hello"
    assert channel_msg.external_id == "42"
    assert allowed_refs == {"111"}


async def test_dm_from_disallowed_user_is_filtered_at_adapter():
    """Belt-and-suspenders: even though the dispatcher would also reject,
    we don't bother dispatching messages from non-allow-listed users at all.
    """
    dispatcher = _StubDispatcher()
    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})
    adapter._dispatcher = dispatcher

    msg = _make_dm_message(content="hi", author_id=999, message_id=1)
    await adapter._on_message(msg)

    assert dispatcher.calls == []


async def test_message_from_self_is_ignored():
    dispatcher = _StubDispatcher()
    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})
    adapter._dispatcher = dispatcher

    msg = _make_dm_message(content="hi", author_id=111, message_id=1)
    msg.author.bot = True  # bots ignored

    await adapter._on_message(msg)

    assert dispatcher.calls == []


async def test_non_dm_messages_are_ignored():
    """Server-channel messages (TextChannel) should not trigger Jarvis."""
    dispatcher = _StubDispatcher()
    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})
    adapter._dispatcher = dispatcher

    import discord

    msg = MagicMock()
    msg.content = "hello in a server"
    msg.id = 99
    msg.author = MagicMock()
    msg.author.id = 111
    msg.author.bot = False
    msg.channel = MagicMock(spec=discord.TextChannel)  # NOT a DMChannel

    await adapter._on_message(msg)

    assert dispatcher.calls == []
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_discord_adapter_receive.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `jarvis/channels/discord_adapter.py`** (receive path only — `send` is implemented in Task 7 but defined here as a stub for the protocol).

```python
"""DiscordAdapter — implements ChannelAdapter via discord.py.

Lifecycle:
  - start(dispatcher): set up intents, instantiate discord.Client, register
    on_message handler, schedule client.start(token) on the event loop, return
    after the client is logged in (await ready event).
  - stop(): close the client and await the background task.
  - send(msg): fetch the user by ID and call user.send(text).

We deliberately do NOT subscribe to channel events — only DMs. The
on_message handler filters anything that isn't a DM from an allow-listed
non-bot user.
"""

import asyncio
import logging

import discord

from jarvis.channels.base import OutboundMessage
from jarvis.core.types import ChannelKind, ChannelMessage

_log = logging.getLogger(__name__)


class DiscordAdapter:
    kind = ChannelKind.DISCORD.value

    def __init__(self, *, token: str, allowed_user_ids: set[str]) -> None:
        self._token = token
        self._allowed = set(allowed_user_ids)
        self._client: discord.Client | None = None
        self._task: asyncio.Task | None = None
        self._dispatcher = None  # set by start()
        self._ready = asyncio.Event()

    async def start(self, dispatcher) -> None:
        if self._client is not None:
            raise RuntimeError("DiscordAdapter already started")
        self._dispatcher = dispatcher
        self._ready.clear()

        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready() -> None:
            _log.info("discord adapter ready as %s", client.user)
            self._ready.set()

        @client.event
        async def on_message(message: discord.Message) -> None:
            await self._on_message(message)

        self._client = client
        # Run client.start() as a background task so start() can return after
        # the gateway is ready.
        self._task = asyncio.create_task(
            client.start(self._token), name="discord-adapter"
        )
        # Wait for ready (or for the task to fail at login).
        ready_task = asyncio.create_task(self._ready.wait())
        done, pending = await asyncio.wait(
            {ready_task, self._task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if self._task in done and not self._ready.is_set():
            # client.start() exited before ready — login failed.
            for p in pending:
                p.cancel()
            exc = self._task.exception()
            raise RuntimeError(f"discord login failed: {exc!r}") from exc
        # Ready fired; let the client task keep running.
        for p in pending:
            if p is not self._task:
                p.cancel()

    async def stop(self) -> None:
        if self._client is None:
            return
        await self._client.close()
        if self._task is not None:
            try:
                await self._task
            except Exception:
                _log.exception("discord client task ended with error")
        self._client = None
        self._task = None

    async def send(self, msg: OutboundMessage) -> None:
        # Filled in by Task 7. For now, define the method so the class
        # structurally satisfies ChannelAdapter even before send works.
        raise NotImplementedError("DiscordAdapter.send arrives in Task 7")

    async def _on_message(self, message: discord.Message) -> None:
        # Filter: only DMs, only non-bot, only allow-listed.
        if not isinstance(message.channel, discord.DMChannel):
            return
        if message.author.bot:
            return
        author_id = str(message.author.id)
        if author_id not in self._allowed:
            return
        if self._dispatcher is None:
            _log.warning("discord on_message before dispatcher set; dropping")
            return

        ch_msg = ChannelMessage(
            channel_kind=ChannelKind.DISCORD,
            channel_ref=author_id,
            text=message.content,
            external_id=str(message.id),
        )
        try:
            await self._dispatcher.dispatch_channel_message(
                ch_msg, allowed_refs=self._allowed
            )
        except Exception:
            _log.exception("discord dispatch failed")
```

- [ ] **Step 4: Run new tests**

Run: `uv run pytest tests/integration/test_discord_adapter_receive.py -v`
Expected: 4 passed.

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 104 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/channels/discord_adapter.py tests/integration/test_discord_adapter_receive.py
git commit -m "add DiscordAdapter receive path with allow-list filtering"
```

---

## Task 7: `DiscordAdapter.send`

Implement the outbound path. `OutputRouter` (Task 5) will call this.

**Files:**
- Modify: `jarvis/channels/discord_adapter.py`
- Create: `tests/integration/test_discord_adapter_send.py`

- [ ] **Step 1: Write failing test**

Write `tests/integration/test_discord_adapter_send.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from jarvis.channels.base import OutboundMessage
from jarvis.channels.discord_adapter import DiscordAdapter
from jarvis.core.types import ChannelKind


async def test_send_fetches_user_and_calls_send():
    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})

    # Stub the client + the user it would fetch.
    fake_user = MagicMock()
    fake_user.send = AsyncMock()
    fake_client = MagicMock()
    fake_client.fetch_user = AsyncMock(return_value=fake_user)
    adapter._client = fake_client

    await adapter.send(
        OutboundMessage(
            channel_kind=ChannelKind.DISCORD,
            channel_ref="111",
            text="hello back",
        )
    )

    fake_client.fetch_user.assert_awaited_once_with(111)
    fake_user.send.assert_awaited_once_with("hello back")


async def test_send_before_start_raises():
    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})

    with pytest.raises(RuntimeError, match="not started"):
        await adapter.send(
            OutboundMessage(
                channel_kind=ChannelKind.DISCORD,
                channel_ref="111",
                text="x",
            )
        )


async def test_send_rejects_non_integer_channel_ref():
    """Discord user IDs are integers in string form. A non-numeric ref
    indicates a mis-routed message — fail loudly."""
    adapter = DiscordAdapter(token="tok", allowed_user_ids={"111"})
    adapter._client = MagicMock()

    with pytest.raises(ValueError, match="not a Discord user id"):
        await adapter.send(
            OutboundMessage(
                channel_kind=ChannelKind.DISCORD,
                channel_ref="not-a-number",
                text="x",
            )
        )
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_discord_adapter_send.py -v`
Expected: 3 failures (all hit the `NotImplementedError` from Task 6's stub OR raise the wrong error).

- [ ] **Step 3: Implement `send` in `discord_adapter.py`**

In `jarvis/channels/discord_adapter.py`, replace the `send` method body:

```python
    async def send(self, msg: OutboundMessage) -> None:
        if self._client is None:
            raise RuntimeError("DiscordAdapter not started")
        try:
            user_id = int(msg.channel_ref)
        except ValueError as e:
            raise ValueError(
                f"channel_ref {msg.channel_ref!r} is not a Discord user id"
            ) from e
        user = await self._client.fetch_user(user_id)
        await user.send(msg.text)
```

- [ ] **Step 4: Run new tests**

Run: `uv run pytest tests/integration/test_discord_adapter_send.py -v`
Expected: 3 passed.

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 107 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/channels/discord_adapter.py tests/integration/test_discord_adapter_send.py
git commit -m "implement DiscordAdapter.send via fetch_user"
```

---

## Task 8: Wire output routing into `TriggerDispatcher._run`

Right now `TriggerDispatcher._run` just calls `runner.run(request)` and returns the result. Channel-triggered runs need their result piped through `OutputRouter` so the user actually receives the reply. Manual/scheduled runs don't (CLI prints; scheduler routes per its own config in Plan 4).

**Files:**
- Modify: `jarvis/core/dispatcher.py`
- Modify: `tests/integration/test_dispatcher.py` (extend existing tests)

- [ ] **Step 1: Write a test that captures the current gap**

Append to `tests/integration/test_dispatcher.py`:

```python


async def test_dispatch_channel_message_routes_reply_to_adapter(infra):
    _, factory, audit = infra
    model = _CountingFakeModel()
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers=[],
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model=model,
    )

    # Recording channel adapter.
    from jarvis.channels.base import OutboundMessage
    from jarvis.core.output_router import OutputRouter

    sent_messages: list[OutboundMessage] = []

    class _Recorder:
        kind = ChannelKind.DISCORD.value

        async def start(self, dispatcher) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def send(self, msg: OutboundMessage) -> None:
            sent_messages.append(msg)

    router = OutputRouter(adapters=[_Recorder()])
    dispatcher = TriggerDispatcher(runner=runner, audit=audit, output_router=router)

    msg = ChannelMessage(
        channel_kind=ChannelKind.DISCORD,
        channel_ref="user-1",
        text="hello jarvis",
        external_id="msg-routing-1",
    )
    result = await dispatcher.dispatch_channel_message(msg, allowed_refs={"user-1"})
    assert result is not None
    assert "reply-1" in result.final_output

    # The reply was routed back through the recording adapter.
    assert len(sent_messages) == 1
    assert sent_messages[0].channel_ref == "user-1"
    assert sent_messages[0].text == result.final_output


async def test_dispatch_manual_does_not_route(infra):
    """Manual triggers go through the CLI/dashboard path, not channel routing."""
    _, factory, audit = infra
    model = _CountingFakeModel()
    runner = AgentRunner(
        session_factory=factory,
        audit=audit,
        mcp_servers=[],
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        model=model,
    )

    from jarvis.channels.base import OutboundMessage
    from jarvis.core.output_router import OutputRouter

    sent_messages: list[OutboundMessage] = []

    class _Recorder:
        kind = ChannelKind.DISCORD.value

        async def start(self, dispatcher) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def send(self, msg: OutboundMessage) -> None:
            sent_messages.append(msg)

    router = OutputRouter(adapters=[_Recorder()])
    dispatcher = TriggerDispatcher(runner=runner, audit=audit, output_router=router)

    await dispatcher.dispatch_manual(user="mark", prompt="hi")

    assert sent_messages == []
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_dispatcher.py::test_dispatch_channel_message_routes_reply_to_adapter -v`
Expected: fails — `TriggerDispatcher` doesn't accept `output_router` kwarg yet.

- [ ] **Step 3: Update `TriggerDispatcher`**

In `jarvis/core/dispatcher.py`, update the constructor and `_run` method:

```python
class TriggerDispatcher:
    def __init__(
        self,
        *,
        runner: AgentRunner,
        audit: AuditLogger,
        output_router: "OutputRouter | None" = None,
        max_concurrent: int = 3,
        dedup_window: int = 256,
    ) -> None:
        self._runner = runner
        self._audit = audit
        self._output_router = output_router
        self._sem = asyncio.Semaphore(max_concurrent)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_cap = dedup_window
```

Add the import at the top of the file:

```python
from jarvis.core.output_router import OutputRouter
```

(Then drop the string-quoted forward reference — use `OutputRouter | None = None` directly.)

Update `_run` to route the result if a router is configured:

```python
    async def _run(self, request: InvocationRequest) -> AgentRunResult:
        async with self._sem:
            result = await self._runner.run(request)
        if self._output_router is not None:
            await self._output_router.route(result)
        return result
```

The router is called OUTSIDE the semaphore so a slow `send` (Discord rate-limit, etc.) doesn't block the next agent run.

- [ ] **Step 4: Run new tests**

Run: `uv run pytest tests/integration/test_dispatcher.py -v`
Expected: 6 passed (4 existing + 2 new).

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 109 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/core/dispatcher.py tests/integration/test_dispatcher.py
git commit -m "wire OutputRouter into TriggerDispatcher for channel reply routing"
```

---

## Task 9: Wire it all into `bootstrap()`

`AppContext` gains `channel_adapters` and `output_router`. `bootstrap()` reads `cfg.channels.discord` (if present and enabled), constructs a `DiscordAdapter`, builds an `OutputRouter`, hands it to the dispatcher, and starts the adapter.

**Files:**
- Modify: `jarvis/main.py`
- Modify: `tests/integration/test_main_smoke.py` (add a test for the adapter wiring path)

- [ ] **Step 1: Write a test for the new wiring**

Append to `tests/integration/test_main_smoke.py`:

```python


async def test_bootstrap_starts_discord_adapter_when_configured(
    tmp_path, monkeypatch
):
    """When channels.yaml has discord, bootstrap should construct and start
    a DiscordAdapter. We patch DiscordAdapter.start to avoid a real network
    connection."""
    from jarvis.channels import discord_adapter as da_mod

    started = []

    async def _fake_start(self, dispatcher):
        started.append(self)
        # Mark as if connected so stop() can be called cleanly.
        from unittest.mock import AsyncMock, MagicMock

        self._client = MagicMock()
        self._client.close = AsyncMock()

    async def _fake_stop(self):
        self._client = None

    monkeypatch.setattr(da_mod.DiscordAdapter, "start", _fake_start)
    monkeypatch.setattr(da_mod.DiscordAdapter, "stop", _fake_stop)

    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "jarvis.yaml").write_text(
        "llm:\n  base_url: http://x/v1\n  api_key: x\n  model: m\n"
    )
    (config_dir / "channels.yaml").write_text(
        'discord:\n  token: tok\n  allowed_user_ids: ["111"]\n'
    )
    (config_dir / "mcp-servers.yaml").write_text("servers: []")

    db_path = tmp_path / "jarvis.db"
    ctx = await bootstrap(
        config_dir=config_dir,
        db_url=f"sqlite+aiosqlite:///{db_path}",
    )
    try:
        assert len(ctx.channel_adapters) == 1
        assert ctx.output_router is not None
        assert len(started) == 1
    finally:
        await ctx.shutdown()


async def test_bootstrap_no_discord_when_unconfigured(tmp_path, config_dir):
    """The existing channels.yaml fixture has no discord — verify we don't
    spawn an adapter."""
    db_path = tmp_path / "jarvis.db"
    ctx = await bootstrap(
        config_dir=config_dir,
        db_url=f"sqlite+aiosqlite:///{db_path}",
    )
    try:
        assert ctx.channel_adapters == []
    finally:
        await ctx.shutdown()
```

- [ ] **Step 2: Run — verify the new tests fail**

Run: `uv run pytest tests/integration/test_main_smoke.py -v`
Expected: failures because `AppContext` has no `channel_adapters` or `output_router` field.

- [ ] **Step 3: Update `bootstrap()` and `AppContext`**

In `jarvis/main.py`, update the file. Add imports:

```python
from jarvis.channels.base import ChannelAdapter
from jarvis.channels.discord_adapter import DiscordAdapter
from jarvis.core.output_router import OutputRouter
```

Update `AppContext`:

```python
@dataclass(slots=True)
class AppContext:
    config: LoadedConfig
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    audit: AuditLogger
    mcp_manager: MCPManager
    agent_runner: AgentRunner
    dispatcher: TriggerDispatcher
    channel_adapters: list[ChannelAdapter]
    output_router: OutputRouter

    async def shutdown(self) -> None:
        # Stop adapters first so no new triggers arrive while we tear down.
        for adapter in self.channel_adapters:
            try:
                await adapter.stop()
            except Exception:
                _log.exception("error stopping channel adapter")
        await self.mcp_manager.stop()
        await self.audit.stop()
        await self.engine.dispose()
```

Update `bootstrap()`. Replace the section that constructs the dispatcher with this expanded block. Find the line `dispatcher = TriggerDispatcher(...)` and replace from there to the `return AppContext(...)`:

```python
    # Channel adapters (currently just Discord).
    channel_adapters: list[ChannelAdapter] = []
    if (
        cfg.channels.discord is not None
        and cfg.channels.discord.enabled
    ):
        discord_adapter = DiscordAdapter(
            token=cfg.channels.discord.token,
            allowed_user_ids=set(cfg.channels.discord.allowed_user_ids),
        )
        channel_adapters.append(discord_adapter)

    # Output router knows how to send replies through any of the adapters.
    output_router = OutputRouter(adapters=channel_adapters)

    # Dispatcher gets a reference to the router so channel-triggered runs
    # automatically reply through the originating adapter.
    dispatcher = TriggerDispatcher(
        runner=agent_runner,
        audit=audit,
        output_router=output_router,
        max_concurrent=cfg.jarvis.max_concurrent_agents,
    )

    # Now that the dispatcher exists, start each adapter (they'll begin
    # pushing inbound events to the dispatcher).
    for adapter in channel_adapters:
        await adapter.start(dispatcher)

    _log.info("jarvis bootstrap complete")
    return AppContext(
        config=cfg,
        engine=engine,
        session_factory=factory,
        audit=audit,
        mcp_manager=mcp_manager,
        agent_runner=agent_runner,
        dispatcher=dispatcher,
        channel_adapters=channel_adapters,
        output_router=output_router,
    )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/integration/test_main_smoke.py -v`
Expected: 4 passed (2 existing + 2 new).

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 111 passed, clean.

- [ ] **Step 6: Commit**

```bash
git add jarvis/main.py tests/integration/test_main_smoke.py
git commit -m "wire DiscordAdapter and OutputRouter into bootstrap"
```

---

## Task 10: `jarvis serve` CLI command

A long-running command that bootstraps and waits for SIGINT/SIGTERM, then shuts down cleanly.

**Files:**
- Modify: `jarvis/cli.py`
- Modify: `tests/integration/test_cli.py` (add a test that exercises the serve loop briefly)

- [ ] **Step 1: Write the test**

Append to `tests/integration/test_cli.py`:

```python


def test_serve_starts_and_stops_cleanly(config_dir, tmp_path, monkeypatch):
    """The serve command bootstraps, waits, and shuts down on signal.

    We patch DiscordAdapter.start/stop to skip the network, and we trigger
    shutdown by setting an event from a background thread shortly after
    the command starts.
    """
    from jarvis.channels import discord_adapter as da_mod
    from jarvis.cli import _serve_async

    started, stopped = [], []

    async def _fake_start(self, dispatcher):
        from unittest.mock import AsyncMock, MagicMock
        self._client = MagicMock()
        self._client.close = AsyncMock()
        started.append(self)

    async def _fake_stop(self):
        stopped.append(self)
        self._client = None

    monkeypatch.setattr(da_mod.DiscordAdapter, "start", _fake_start)
    monkeypatch.setattr(da_mod.DiscordAdapter, "stop", _fake_stop)

    # Add a discord channel config so an adapter actually gets created.
    (config_dir / "channels.yaml").write_text(
        'discord:\n  token: tok\n  allowed_user_ids: ["111"]\n'
    )

    db_path = tmp_path / "jarvis.db"

    import asyncio

    async def _drive() -> None:
        # Run _serve_async with a stop event we'll set after a brief delay.
        stop_event = asyncio.Event()

        async def _trigger_stop() -> None:
            await asyncio.sleep(0.05)
            stop_event.set()

        async with asyncio.TaskGroup() as tg:
            tg.create_task(_trigger_stop())
            tg.create_task(
                _serve_async(
                    config_dir=config_dir,
                    db_url=f"sqlite+aiosqlite:///{db_path}",
                    stop_event=stop_event,
                )
            )

    asyncio.run(_drive())

    assert len(started) == 1
    assert len(stopped) == 1
```

- [ ] **Step 2: Run — verify failure**

Run: `uv run pytest tests/integration/test_cli.py -v`
Expected: fails — `_serve_async` doesn't exist.

- [ ] **Step 3: Add the `serve` command to `jarvis/cli.py`**

Append to `jarvis/cli.py`:

```python
import signal


@app.command("serve")
def serve_command(
    config_dir: Path = typer.Option(
        _DEFAULT_CONFIG, "--config-dir", "-c", help="Directory with jarvis.yaml etc."
    ),
    db_url: str = typer.Option(
        _DEFAULT_DB, "--db-url", help="SQLAlchemy DB URL"
    ),
) -> None:
    """Run Jarvis as a long-lived process (Discord, scheduler, etc.)."""
    asyncio.run(_serve_async(config_dir=config_dir, db_url=db_url))


async def _serve_async(
    *,
    config_dir: Path,
    db_url: str,
    stop_event: asyncio.Event | None = None,
) -> None:
    """The serve loop. Bootstraps, waits for stop_event, shuts down.

    `stop_event` is injectable for tests; production gets one wired to
    SIGINT and SIGTERM by `_install_signal_handlers`.
    """
    ctx = await bootstrap(config_dir=config_dir, db_url=db_url)
    try:
        if stop_event is None:
            stop_event = asyncio.Event()
            _install_signal_handlers(stop_event)
        typer.echo("jarvis serving (Ctrl-C to stop)")
        await stop_event.wait()
        typer.echo("shutting down...")
    finally:
        await ctx.shutdown()


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler — fall back.
            signal.signal(sig, lambda *_: stop_event.set())
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/integration/test_cli.py -v`
Expected: 3 passed.

- [ ] **Step 5: Full suite + ruff**

Run: `uv run pytest && uv run ruff check . && uv run ruff format --check .`
Expected: 112 passed, clean.

- [ ] **Step 6: Manual smoke test (optional)**

Skip this in CI but useful locally if you have a Discord bot token:

```bash
cd /Users/mdolton/dev/jarvis
mkdir -p /tmp/jarvis-serve/config /tmp/jarvis-serve/data
cat > /tmp/jarvis-serve/config/jarvis.yaml <<'EOF'
llm:
  base_url: http://localhost:1234/v1
  api_key: dummy
  model: test-model
EOF
cat > /tmp/jarvis-serve/config/channels.yaml <<'EOF'
discord:
  token: ${DISCORD_TOKEN}
  allowed_user_ids: ["YOUR_DISCORD_USER_ID"]
EOF
echo "servers: []" > /tmp/jarvis-serve/config/mcp-servers.yaml

DISCORD_TOKEN=your_real_token uv run python -m jarvis serve \
  --config-dir /tmp/jarvis-serve/config \
  --db-url "sqlite+aiosqlite:////tmp/jarvis-serve/data/jarvis.db"
```

DM the bot from Discord; it should reply.

- [ ] **Step 7: Commit**

```bash
git add jarvis/cli.py tests/integration/test_cli.py
git commit -m "add jarvis serve CLI command for long-lived operation"
```

---

## Plan 3 complete — summary

At the end of Plan 3:

- `python -m jarvis serve` runs Jarvis as a Discord bot (plus everything from Plans 1+2). DM the bot from an allow-listed user, get a reply.
- `ChannelAdapter` protocol is in place so Plan 3+ can add Slack/WhatsApp adapters without touching the dispatcher.
- `OutputRouter` routes channel-triggered replies; manual/dashboard triggers stay no-ops (the CLI/web prints directly).
- Plan 2 followups landed: `policy_override` preserved across MCPManager reconnects.

**Known debt carried into Plan 4:**
- No retry on transient Discord send failures (rate limits, transient gateway disconnects). discord.py auto-reconnects the gateway, so receive resilience is handled — outbound is still naive.
- No `channel.sent` audit event. (Plan 2 followup; folds in naturally with Plan 4 or 5.)
- `MCPManager` background reconnect loop still missing.

**Still to come:** Plan 4 Scheduler, Plan 5 Dashboard, Plan 6 Docker.
