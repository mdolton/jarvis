"""ModelCatalog — lists models from the LLM endpoint's /v1/models.

Fetched on-demand only, with a short TTL cache so dashboard renders and
Discord autocomplete keystrokes don't hammer the endpoint. Successful results
are cached; failures are not (so recovery is immediate). The `ok` flag lets
callers distinguish "model confirmed absent" from "couldn't reach the endpoint"
— load-bearing for the hybrid stale-model fallback.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Catalog:
    models: list[str]
    ok: bool


class ModelCatalog:
    def __init__(
        self,
        client,
        *,
        ttl_sec: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = ttl_sec
        self._clock = clock
        self._cached: Catalog | None = None
        self._cached_at: float = 0.0

    async def list_models(self) -> Catalog:
        now = self._clock()
        if self._cached is not None and (now - self._cached_at) < self._ttl:
            return self._cached
        try:
            resp = await self._client.models.list()
            ids = sorted(m.id for m in resp.data)
            cat = Catalog(models=ids, ok=True)
        except Exception:
            _log.warning("failed to list models from endpoint", exc_info=True)
            return Catalog(models=[], ok=False)
        self._cached = cat
        self._cached_at = now
        return cat
