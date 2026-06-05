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


def test_assemble_memory_prompt_without_memory_returns_current_prompt_only():
    ctx = MemoryContext(preferences=[], recalled=[], recall_available=True)

    prompt = assemble_memory_prompt(
        memory_context=ctx,
        trigger_context="",
        current_prompt="hello",
    )

    assert prompt == "hello"
