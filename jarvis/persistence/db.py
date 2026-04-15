"""SQLAlchemy async engine, session factory, and declarative Base."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, TypeDecorator
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class TZDateTime(TypeDecorator[datetime]):
    """DateTime that stores values as naive UTC and returns them as aware UTC.

    aiosqlite strips tzinfo on roundtrip; this decorator normalizes by
    converting to UTC on INSERT and re-attaching UTC on SELECT. Use this
    wherever a Mapped[datetime] column holds a timezone-aware timestamp.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("TZDateTime requires timezone-aware datetimes; got naive value")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)


def create_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    """Build an async engine. SQLite gets WAL mode via pragma on connect."""
    connect_args: dict = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}

    engine = create_async_engine(
        url,
        echo=echo,
        connect_args=connect_args,
        future=True,
    )

    if url.startswith("sqlite"):
        from sqlalchemy import event

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
