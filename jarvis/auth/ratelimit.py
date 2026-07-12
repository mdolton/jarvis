"""In-memory token buckets for the login endpoints.

Hand-rolled on purpose: this is a single-process uvicorn app, so shared
state is just a dict — a Redis-shaped dependency would buy nothing. The
per-IP buckets are only meaningful if the client IP is real: uvicorn must
trust X-Forwarded-For from the reverse proxy's IP ONLY (forwarded_allow_ips
in jarvis.yaml — the schema rejects "*").
"""

from __future__ import annotations

import time
from collections.abc import Callable


class RateLimiter:
    """Keyed token buckets: `capacity` burst, refilling at `refill_per_sec`.

    allow(key) spends one token and reports whether one was available. The
    clock is injectable for tests (monotonic seconds).
    """

    def __init__(
        self,
        *,
        capacity: float,
        refill_per_sec: float,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 4096,
    ) -> None:
        self._capacity = capacity
        self._refill_per_sec = refill_per_sec
        self._clock = clock
        self._max_keys = max_keys
        # key -> (tokens, last_refill_timestamp)
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        tokens, updated = self._buckets.get(key, (self._capacity, now))
        tokens = min(self._capacity, tokens + (now - updated) * self._refill_per_sec)
        allowed = tokens >= 1.0
        if allowed:
            tokens -= 1.0
        self._buckets[key] = (tokens, now)
        if len(self._buckets) > self._max_keys:
            self._prune(now)
        return allowed

    def _prune(self, now: float) -> None:
        """Drop buckets that have fully refilled — forgetting them is lossless."""
        full_after = self._capacity / self._refill_per_sec
        self._buckets = {
            key: (tokens, updated)
            for key, (tokens, updated) in self._buckets.items()
            if now - updated < full_after
        }
