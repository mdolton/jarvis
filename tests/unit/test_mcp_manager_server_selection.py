"""MCPManager server selection: filtering is keyed on the registry, not on
the SDK object's own `.name`."""

from jarvis.mcp.manager import MCPManager


class _SdkServer:
    """An upstream server object whose `.name` need not match the runtime key
    we registered it under (OAuth connections register by provider key)."""

    def __init__(self, name: str) -> None:
        self.name = name


def _manager_with(**servers) -> MCPManager:
    manager = MCPManager.__new__(MCPManager)  # no event loop / connections needed
    manager._sdk_servers = dict(servers)
    return manager


def test_no_filter_returns_every_server():
    manager = _manager_with(weather=_SdkServer("weather"), gmail=_SdkServer("gmail"))
    assert len(manager.agent_mcp_servers()) == 2


def test_filter_selects_by_runtime_key_not_by_object_name():
    """The whole reason this filter lives in the manager: an OAuth connection
    is registered under its provider key while the SDK object reports the
    upstream name. Filtering on `.name` would silently drop it."""
    manager = _manager_with(
        gmail=_SdkServer("Gmail MCP"),
        weather=_SdkServer("weather"),
    )

    selected = manager.agent_mcp_servers(only=["gmail"])

    assert [s.name for s in selected] == ["Gmail MCP"]


def test_unknown_names_are_ignored_rather_than_raising():
    manager = _manager_with(weather=_SdkServer("weather"))
    assert len(manager.agent_mcp_servers(only=["weather", "nope"])) == 1
    assert manager.agent_mcp_servers(only=["nope"]) == []


def test_empty_filter_selects_nothing():
    """Distinct from None: callers translate "unset" to None before this point,
    so an explicitly empty collection means empty."""
    manager = _manager_with(weather=_SdkServer("weather"))
    assert manager.agent_mcp_servers(only=[]) == []


def test_server_names_are_sorted_registry_keys():
    manager = _manager_with(
        weather=_SdkServer("w"), gmail=_SdkServer("g"), calendar=_SdkServer("c")
    )
    assert manager.server_names() == ["calendar", "gmail", "weather"]
