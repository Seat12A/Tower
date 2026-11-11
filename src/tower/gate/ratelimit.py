"""In-memory per-key token bucket (per replica, never persisted)."""

from __future__ import annotations

import time
from collections import OrderedDict

from starlette.requests import Request


class TokenBucketLimiter:
    """Burst of ``capacity`` requests, then ``refill_per_second``. LRU-bounded memory."""

    def __init__(
        self, capacity: float = 20, refill_per_second: float = 0.2, max_entries: int = 10_000
    ) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.max_entries = max_entries
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._buckets)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        entry = self._buckets.get(key)
        if entry is None:
            tokens = self.capacity
        else:
            tokens = min(
                self.capacity, entry[0] + max(0.0, now - entry[1]) * self.refill_per_second
            )
        allowed = tokens >= 1
        if allowed:
            tokens -= 1
        self._buckets[key] = (tokens, now)
        self._buckets.move_to_end(key)
        while len(self._buckets) > self.max_entries:
            self._buckets.popitem(last=False)
        return allowed


def client_ip(request: Request) -> str:
    """Last ``X-Forwarded-For`` entry (added by the trusted proxy), else the peer address.

    Used only as an in-memory limiter key: never logged, never stored.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        last = forwarded.split(",")[-1].strip()
        if last:
            return last
    return request.client.host if request.client else "unknown"
