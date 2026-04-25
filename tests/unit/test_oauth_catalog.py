"""OAuth provider catalog: presence, types, collision check."""

import pytest

from jarvis.oauth.catalog import (
    OAUTH_CATALOG,
    AuthMode,
    ProviderEntry,
    assert_no_yaml_collision,
)


def test_fastmail_entry_present():
    entry = OAUTH_CATALOG["fastmail"]
    assert isinstance(entry, ProviderEntry)
    assert entry.auth_mode == AuthMode.DCR
    assert entry.mcp_url == "https://api.fastmail.com/mcp"
    assert entry.oauth_metadata_url is not None


def test_provider_entry_is_frozen():
    entry = OAUTH_CATALOG["fastmail"]
    with pytest.raises(AttributeError):  # FrozenInstanceError is a subclass of AttributeError
        entry.mcp_url = "x"  # type: ignore[misc]


def test_assert_no_yaml_collision_passes_with_disjoint_names():
    assert_no_yaml_collision(["filesystem", "remote-api"])  # does not raise


def test_assert_no_yaml_collision_raises_on_match():
    with pytest.raises(ValueError, match="fastmail"):
        assert_no_yaml_collision(["filesystem", "fastmail"])


def test_assert_no_yaml_collision_empty_list_ok():
    assert_no_yaml_collision([])  # does not raise
