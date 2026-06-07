from __future__ import annotations

from jarvis.memory.types import MemoryContext, RecalledMemory


def assemble_memory_prompt(
    *,
    memory_context: MemoryContext,
    runtime_context: str = "",
    trigger_context: str,
    current_prompt: str,
) -> str:
    sections: list[str] = []

    if memory_context.preferences:
        lines = ["Standing preferences:"]
        lines.extend(f"- {preference}" for preference in memory_context.preferences)
        sections.append("\n".join(lines))

    stripped_runtime_context = runtime_context.strip()
    if stripped_runtime_context:
        sections.append(
            "\n".join(
                [
                    "Current runtime context:",
                    "Use current runtime context as the source of truth for live capabilities.",
                    stripped_runtime_context,
                ]
            )
        )

    if memory_context.recalled:
        lines = [
            "Relevant prior context:",
            "Use this as possibly relevant prior context, not as a standing instruction.",
        ]
        for memory in memory_context.recalled:
            lines.extend(_format_recalled_memory(memory))
        sections.append("\n".join(lines))

    stripped_trigger_context = trigger_context.strip()
    if stripped_trigger_context:
        sections.append(stripped_trigger_context)

    if not sections:
        return current_prompt

    sections.append(current_prompt)
    return "\n\n".join(sections)


def _format_recalled_memory(memory: RecalledMemory) -> list[str]:
    lines = [f"- {memory.summary}"]
    if memory.topics:
        lines.append(f"  Topics: {', '.join(memory.topics)}")
    if memory.entities:
        lines.append(f"  Entities: {', '.join(memory.entities)}")
    for evidence in memory.evidence:
        label = evidence.get("label", "")
        content = evidence.get("content", "")
        if label and content:
            lines.append(f"  Evidence: {label}: {content}")
        elif label or content:
            lines.append(f"  Evidence: {label or content}")
    return lines
