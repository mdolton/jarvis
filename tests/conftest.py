"""Shared pytest fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _reset_agents_sdk_globals():
    """Reset Agents SDK process-global state between tests.

    Several tests install a custom trace processor via `set_trace_processors`
    and/or replace the default OpenAI client via `set_default_openai_client`
    (transitively, via `bootstrap` and `install_as_default`). Without an
    autouse cleanup, the state leaks across tests and can cause order-
    dependent flakiness — e.g., a tracer installed in test A may receive
    events from test B and silently fail because B's audit logger is gone.
    """
    yield
    # Clear any installed trace processors so the next test starts clean.
    from agents import set_trace_processors

    set_trace_processors([])
