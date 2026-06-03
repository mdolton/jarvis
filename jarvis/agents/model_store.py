"""ModelStore — the active interactive model selection.

Persisted in the `settings` table under one key. `None` (or absent) means
"use the default" (the YAML config model). `current()` and `selection()` are
sync and read an in-memory cache so model resolution stays off the DB hot path;
`load()` primes the cache at boot and `set()` writes through.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jarvis.persistence.repositories import SettingsRepo

_KEY = "llm.active_model"


class ModelStore:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        default_model: str,
    ) -> None:
        self._session_factory = session_factory
        self._default = default_model
        self._selection: str | None = None

    async def load(self) -> None:
        async with self._session_factory() as session:
            value = await SettingsRepo(session).get(_KEY)
        self._selection = value if isinstance(value, str) else None

    def selection(self) -> str | None:
        """The raw stored override, or None when set to default."""
        return self._selection

    def current(self) -> str:
        """The resolved model: the override, or the config default."""
        return self._selection or self._default

    async def set(self, model: str | None) -> None:
        """Set the override; None clears it (removes the row) back to the default."""
        async with self._session_factory() as session:
            repo = SettingsRepo(session)
            if model is None:
                await repo.delete(_KEY)
            else:
                await repo.set(_KEY, model)
        self._selection = model
