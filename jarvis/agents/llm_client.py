"""LLM client builder.

Wraps the `openai.AsyncOpenAI` constructor with config-driven values and
installs the client as the Agents SDK default so every Runner.run() call
uses the configured endpoint without per-call plumbing.
"""

from agents import set_default_openai_client, set_tracing_disabled
from openai import AsyncOpenAI

from jarvis.config.schema import LLMConfig


def build_llm_client(cfg: LLMConfig) -> AsyncOpenAI:
    """Build an AsyncOpenAI client pointed at the configured endpoint.

    Does NOT install it globally — use `install_as_default` for that. This
    split lets tests build a client without clobbering the process-wide
    Agents SDK default.
    """
    return AsyncOpenAI(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        timeout=cfg.request_timeout_sec,
    )


def install_as_default(client: AsyncOpenAI) -> None:
    """Install `client` as the Agents SDK's default OpenAI client.

    Also disables the default OpenAI tracing exporter, which would try to
    POST trace spans to platform.openai.com and fail with 401 against a
    local LLM endpoint. Our custom tracer (Task 11) still receives events.
    """
    set_default_openai_client(client)
    set_tracing_disabled(True)
