from uuid import uuid4

from jarvis.memory.prompt import assemble_memory_prompt
from jarvis.memory.types import MemoryContext, RecalledMemory


def test_assemble_memory_prompt_orders_preferences_context_and_current_prompt():
    memory_id = uuid4()
    ctx = MemoryContext(
        preferences=["Prefer concise answers."],
        recalled=[
            RecalledMemory(
                memory_entry_id=memory_id,
                summary="We discussed PR #18 Action Inbox deploy validation.",
                topics=["jarvis"],
                entities=["PR #18"],
                evidence=[
                    {
                        "kind": "pull_request",
                        "label": "PR #18",
                        "content": "PR #18",
                    }
                ],
                score=0.91,
                rank=1,
            )
        ],
        recall_available=True,
    )

    prompt = assemble_memory_prompt(
        memory_context=ctx,
        trigger_context="Schedule context:\n- Local date: 2026-06-04\n\n",
        current_prompt="What did we ship?",
    )

    assert prompt.index("Standing preferences") < prompt.index("Relevant prior context")
    assert prompt.index("Relevant prior context") < prompt.index("Schedule context")
    assert prompt.endswith("What did we ship?")
    assert "Prefer concise answers." in prompt
    assert "Use this as possibly relevant prior context" in prompt
    assert "PR #18" in prompt


def test_assemble_memory_prompt_places_runtime_context_before_recalled_memory():
    memory_id = uuid4()
    ctx = MemoryContext(
        preferences=[],
        recalled=[
            RecalledMemory(
                memory_entry_id=memory_id,
                summary="The assistant said it did not have access to YNAB.",
                topics=["MCP"],
                entities=["YNAB"],
                evidence=[
                    {
                        "label": "YNAB access status",
                        "content": "No YNAB MCP access.",
                    }
                ],
                score=0.92,
                rank=1,
            )
        ],
        recall_available=True,
    )

    prompt = assemble_memory_prompt(
        memory_context=ctx,
        runtime_context="Current MCP servers:\n- ynab: list_accounts, get_month",
        trigger_context="",
        current_prompt="do you have access to the ynab mcp server?",
    )

    assert prompt.index("Current MCP servers") < prompt.index("Relevant prior context")
    assert "Use current runtime context as the source of truth" in prompt
    assert prompt.endswith("do you have access to the ynab mcp server?")


def test_assemble_memory_prompt_places_user_context_first():
    ctx = MemoryContext(
        preferences=["Prefer concise answers."],
        recalled=[],
        recall_available=True,
    )

    prompt = assemble_memory_prompt(
        memory_context=ctx,
        user_context="- Home location: Austin, Texas",
        runtime_context="Current MCP servers:\n- weather: weather_forecast",
        trigger_context="",
        current_prompt="Prepare my daily brief.",
    )

    assert prompt.index("User context") < prompt.index("Standing preferences")
    assert "- Home location: Austin, Texas" in prompt
    assert prompt.endswith("Prepare my daily brief.")


def test_assemble_memory_prompt_omits_blank_user_context():
    ctx = MemoryContext(preferences=[], recalled=[], recall_available=True)

    prompt = assemble_memory_prompt(
        memory_context=ctx,
        user_context="   ",
        trigger_context="",
        current_prompt="hello",
    )

    assert prompt == "hello"


def test_assemble_memory_prompt_without_memory_returns_current_prompt_only():
    ctx = MemoryContext(preferences=[], recalled=[], recall_available=True)

    prompt = assemble_memory_prompt(
        memory_context=ctx,
        runtime_context="",
        trigger_context="",
        current_prompt="hello",
    )

    assert prompt == "hello"
