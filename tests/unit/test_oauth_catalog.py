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


def test_gmail_entry_present():
    entry = OAUTH_CATALOG["gmail"]
    assert isinstance(entry, ProviderEntry)
    assert entry.auth_mode == AuthMode.MANUAL
    assert entry.mcp_url == "https://gmailmcp.googleapis.com/mcp/v1"
    assert entry.oauth_metadata_url == (
        "https://accounts.google.com/.well-known/openid-configuration"
    )
    assert entry.client_id_env == "GOOGLE_OAUTH_CLIENT_ID"
    assert entry.client_secret_env == "GOOGLE_OAUTH_CLIENT_SECRET"
    assert entry.scopes == (
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
    )
    assert entry.extra_auth_params == {"access_type": "offline", "prompt": "consent"}
    assert entry.send_resource_indicator is True


def test_calendar_entry_present():
    entry = OAUTH_CATALOG["calendar"]
    assert isinstance(entry, ProviderEntry)
    assert entry.auth_mode == AuthMode.MANUAL
    assert entry.mcp_url == "https://calendarmcp.googleapis.com/mcp/v1"
    assert entry.oauth_metadata_url == (
        "https://accounts.google.com/.well-known/openid-configuration"
    )
    # Reuses the shared Google OAuth client credentials.
    assert entry.client_id_env == "GOOGLE_OAUTH_CLIENT_ID"
    assert entry.client_secret_env == "GOOGLE_OAUTH_CLIENT_SECRET"
    assert entry.scopes == (
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        "https://www.googleapis.com/auth/calendar.events.freebusy",
        "https://www.googleapis.com/auth/calendar.events.readonly",
    )
    assert entry.extra_auth_params == {"access_type": "offline", "prompt": "consent"}
    assert entry.send_resource_indicator is True


def test_provider_entry_defaults_for_manual_fields():
    entry = OAUTH_CATALOG["fastmail"]
    assert entry.client_id_env is None
    assert entry.client_secret_env is None
    assert entry.send_resource_indicator is True


def test_fastmail_entry_still_present():
    entry = OAUTH_CATALOG["fastmail"]
    assert entry.auth_mode == AuthMode.DCR
