from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from agents import Agent, ModelRetrySettings, ModelSettings, RetryPolicyContext

from jarvis.config.schema import LLMConfig
from jarvis.core.types import ScheduledTrigger

_log = logging.getLogger(__name__)

# One resample after a server-side failure, then give up. The failure this
# guards is a bad *sample*, not a bad request, so a second attempt is cheap
# and usually enough; a third would just burn another full generation.
_MODEL_MAX_RETRIES = 1


def retry_on_server_error(ctx: RetryPolicyContext) -> bool:
    """Retry 5xx from the LLM endpoint, never a timeout.

    A local endpoint returns 500 when it cannot parse the tool call the model
    just generated — e.g. llama.cpp rejecting Qwen3-Coder-style XML
    (`<function=…><parameter=…>`) where its parser wants a JSON payload. That
    is stochastic format drift: the same prompt resampled almost always comes
    back well-formed, so one retry rescues an otherwise dead scheduled run.

    Timeouts are excluded deliberately. They are not transient here — a turn
    that blew the request budget will blow it again, and each attempt costs a
    full generation (see llm_client.build_llm_client, which disables the
    OpenAI client's own retries for the same reason).
    """
    err = ctx.normalized
    if err.is_timeout or err.is_network_error:
        return False
    return err.status_code is not None and err.status_code >= 500


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


def resolve_mcp_scope(trigger) -> tuple[str, ...] | None:
    """Which MCP servers this run may use; None means all of them.

    Only scheduled triggers carry a scope. An empty list is treated as "no
    scope set" rather than "no servers": a schedule saved with nothing ticked
    should keep working, not silently lose every tool.
    """
    if isinstance(trigger, ScheduledTrigger) and trigger.mcp_servers:
        return tuple(trigger.mcp_servers)
    return None


def _scoped_servers(provider: Callable[..., list], scope: tuple[str, ...] | None) -> list:
    if scope is None:
        return provider()
    servers = provider(only=scope)
    missing = set(scope) - {getattr(s, "name", None) for s in servers}
    if len(servers) < len(scope):
        # Named servers that no longer exist are dropped rather than fatal, but
        # running with a smaller tool surface than the schedule asked for is
        # exactly the kind of thing that should not pass unremarked.
        _log.warning(
            "scheduled run scoped to %d MCP server(s) but only %d are connected%s",
            len(scope),
            len(servers),
            f" (unmatched: {sorted(n for n in missing if n)})" if missing else "",
        )
    return servers


def build_agent(
    *,
    llm_config: LLMConfig,
    mcp_servers_provider: Callable[..., list],
    trigger=None,
    explicit_model: Any = None,
    model_provider: Callable[[], str] | None = None,
    model_override: str | None = None,
    tools: list | None = None,
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
        mcp_servers=_scoped_servers(mcp_servers_provider, resolve_mcp_scope(trigger)),
        model=model,
        tools=list(tools or []),
        model_settings=ModelSettings(
            retry=ModelRetrySettings(
                max_retries=_MODEL_MAX_RETRIES,
                policy=retry_on_server_error,
            )
        ),
    )
    return agent, str(model)
