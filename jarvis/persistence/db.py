"""SQLAlchemy async engine, session factory, and declarative Base."""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


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
        def _set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: ARG001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def session_factory(engine: AsyncEngine) -> Callable[[], AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
