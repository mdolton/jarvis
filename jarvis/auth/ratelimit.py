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


class ExponentialBackoff:
    """Per-key lockout that doubles with every failure past `free_failures`.

    The token buckets above cap sustained REQUEST volume; this punishes
    repeated login FAILURES specifically: the first `free_failures` misses
    cost nothing (typos happen), then each further miss blocks the key for
    base_delay_sec * 2^n, capped at max_delay_sec. A success resets the key.

    allowed(key) is a pure read — a request arriving during a lockout is
    denied without extending it (only real failures escalate).
    """

    def __init__(
        self,
        *,
        free_failures: int = 3,
        base_delay_sec: float = 1.0,
        max_delay_sec: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 4096,
    ) -> None:
        self._free_failures = free_failures
        self._base_delay = base_delay_sec
        self._max_delay = max_delay_sec
        self._clock = clock
        self._max_keys = max_keys
        # key -> (consecutive_failures, blocked_until_timestamp)
        self._entries: dict[str, tuple[int, float]] = {}

    def allowed(self, key: str) -> bool:
        entry = self._entries.get(key)
        return entry is None or self._clock() >= entry[1]

    def record_failure(self, key: str) -> None:
        failures = self._entries.get(key, (0, 0.0))[0] + 1
        over = failures - self._free_failures
        delay = 0.0 if over <= 0 else min(self._base_delay * 2 ** (over - 1), self._max_delay)
        self._entries[key] = (failures, self._clock() + delay)
        if len(self._entries) > self._max_keys:
            self._prune(self._clock())

    def reset(self, key: str) -> None:
        self._entries.pop(key, None)

    def _prune(self, now: float) -> None:
        """Drop keys whose lockout expired more than a full max delay ago."""
        self._entries = {
            key: (failures, blocked_until)
            for key, (failures, blocked_until) in self._entries.items()
            if now - blocked_until < self._max_delay
        }
