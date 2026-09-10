"""In-memory token bucket keyed by client IP (spec §3.3: 30 resolves / 5 min)."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from fastapi import Request


@dataclass
class _Bucket:
    tokens: float
    updated: float


@dataclass
class TokenBucketLimiter:
    capacity: int
    window_seconds: int
    buckets: dict[str, _Bucket] = field(default_factory=dict)

    @property
    def rate(self) -> float:
        return self.capacity / self.window_seconds

    def check(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """Consume one token. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic() if now is None else now
        b = self.buckets.get(key)
        if b is None:
            b = _Bucket(tokens=float(self.capacity), updated=now)
            self.buckets[key] = b
        else:
            b.tokens = min(self.capacity, b.tokens + (now - b.updated) * self.rate)
            b.updated = now
        if b.tokens >= 1:
            b.tokens -= 1
            return True, 0
        return False, max(1, math.ceil((1 - b.tokens) / self.rate))

    def prune(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        stale = [k for k, b in self.buckets.items() if now - b.updated > self.window_seconds * 2]
        for k in stale:
            del self.buckets[k]


def client_ip(request: Request) -> str:
    """Caddy sits in front and sets X-Forwarded-For; trust the first hop."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
