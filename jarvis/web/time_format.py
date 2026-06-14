"""Dashboard datetime formatting."""

from datetime import UTC, datetime


def to_server_local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone()


def format_server_local(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    local = to_server_local(value)
    if local is None:
        return ""
    return local.strftime(fmt)
