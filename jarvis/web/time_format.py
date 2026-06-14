"""Dashboard datetime formatting."""

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def configured_timezone(ctx: Any) -> ZoneInfo | None:
    name = getattr(getattr(getattr(ctx, "config", None), "jarvis", None), "timezone", None)
    if not isinstance(name, str) or not name:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None


def to_server_local(value: datetime | None, timezone: ZoneInfo | None = None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(timezone)


def format_server_local(
    value: datetime | None,
    fmt: str = "%Y-%m-%d %H:%M",
    *,
    timezone: ZoneInfo | None = None,
) -> str:
    local = to_server_local(value, timezone=timezone)
    if local is None:
        return ""
    return local.strftime(fmt)
