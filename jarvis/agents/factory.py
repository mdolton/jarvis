from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agents import Agent

from jarvis.config.schema import LLMConfig
from jarvis.core.types import ScheduledTrigger


def system_prompt() -> str:
    return (
        "You are Jarvis, a helpful personal assistant. "
        "Use the available MCP tools when they help answer the user. "
        "Be concise."
    )


def resolve_model(
    trigger,
    *,
    explicit,
    model_provider: Callable[[], str] | None,
    config_default: str,
):
    """Pick the model for a run.

    Precedence: explicit override, scheduled trigger pin, dynamic provider,
    config default.
    """
    if explicit is not None:
        return explicit
    if isinstance(trigger, ScheduledTrigger) and trigger.model:
        return trigger.model
    if model_provider is not None:
        return model_provider()
    return config_default


def build_agent(
    *,
    llm_config: LLMConfig,
    mcp_servers_provider: Callable[[], list],
    trigger=None,
    explicit_model: Any = None,
    model_provider: Callable[[], str] | None = None,
    model_override: str | None = None,
) -> tuple[Agent, str]:
    model = model_override or resolve_model(
        trigger,
        explicit=explicit_model,
        model_provider=model_provider,
        config_default=llm_config.model,
    )
    agent = Agent(
        name="jarvis",
        instructions=system_prompt(),
        mcp_servers=mcp_servers_provider(),
        model=model,
    )
    return agent, str(model)
