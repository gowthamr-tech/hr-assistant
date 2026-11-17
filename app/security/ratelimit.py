"""Per-employee request rate limit (sliding window, in process memory).

Every chat turn can cost up to three LLM calls, so a single caller looping on
/chat is both a cost and an availability problem. This limiter is per process;
with several replicas, set the limit per replica or enforce it at the gateway.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Callable

WINDOW_S = 60.0
_MAX_KEYS = 50_000  # bound memory if many distinct callers appear


class RateLimiter:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute = per_minute
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> float:
        """Record a hit for ``key``. Returns 0 if allowed, else seconds until a slot frees up."""
        if self.per_minute <= 0:
            return 0.0
        now = self._clock()
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                if len(self._hits) >= _MAX_KEYS:
                    self._evict(now)
                hits = self._hits[key] = deque()
            while hits and now - hits[0] >= WINDOW_S:
                hits.popleft()
            if len(hits) >= self.per_minute:
                return max(0.1, WINDOW_S - (now - hits[0]))
            hits.append(now)
            return 0.0

    def _evict(self, now: float) -> None:
        stale = [k for k, h in self._hits.items() if not h or now - h[-1] >= WINDOW_S]
        for k in stale:
            del self._hits[k]
        if len(self._hits) >= _MAX_KEYS:  # still full: drop the oldest half
            for k in sorted(self._hits, key=lambda k: self._hits[k][-1])[: _MAX_KEYS // 2]:
                del self._hits[k]
