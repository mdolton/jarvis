from jarvis.auth.ratelimit import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_burst_capacity_then_denied():
    clock = FakeClock()
    limiter = RateLimiter(capacity=3, refill_per_sec=1 / 300, clock=clock)
    assert [limiter.allow("k") for _ in range(3)] == [True, True, True]
    assert limiter.allow("k") is False


def test_refill_restores_tokens_over_time():
    clock = FakeClock()
    limiter = RateLimiter(capacity=2, refill_per_sec=1 / 60, clock=clock)
    assert limiter.allow("k") and limiter.allow("k")
    assert limiter.allow("k") is False
    clock.advance(59)
    assert limiter.allow("k") is False
    clock.advance(2)  # crosses one full token
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False


def test_keys_are_independent():
    clock = FakeClock()
    limiter = RateLimiter(capacity=1, refill_per_sec=1 / 60, clock=clock)
    assert limiter.allow("alice") is True
    assert limiter.allow("alice") is False
    assert limiter.allow("bob") is True


def test_refill_never_exceeds_capacity():
    clock = FakeClock()
    limiter = RateLimiter(capacity=2, refill_per_sec=1.0, clock=clock)
    assert limiter.allow("k")
    clock.advance(3600)
    assert [limiter.allow("k") for _ in range(3)] == [True, True, False]


def test_prune_drops_only_refilled_buckets():
    clock = FakeClock()
    limiter = RateLimiter(capacity=1, refill_per_sec=1 / 10, clock=clock, max_keys=2)
    limiter.allow("old-1")
    clock.advance(11)  # old-1 fully refilled -> prunable
    limiter.allow("fresh-1")
    limiter.allow("fresh-2")
    limiter.allow("fresh-3")  # crosses max_keys, triggers prune
    assert "old-1" not in limiter._buckets
    # A drained bucket inside its refill window survives the prune.
    assert "fresh-1" in limiter._buckets
