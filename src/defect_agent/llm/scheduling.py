"""Shared, synchronous request admission and provider cooldown.

One scheduler is shared by clients in the same provider rate-limit domain.
Slots cover actual HTTP requests only, including every retry and format repair.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from collections.abc import Iterator

from .base import TransientError


class RequestScheduler:
    def __init__(self, max_concurrency: int = 2, min_interval_seconds: float = 0.0):
        if max_concurrency < 1 or min_interval_seconds < 0:
            raise ValueError("request concurrency must be positive and interval non-negative")
        self.max_concurrency = max_concurrency
        self.min_interval_seconds = min_interval_seconds
        self._slots = threading.BoundedSemaphore(max_concurrency)
        self._lock = threading.Lock()
        self._last_started: float | None = None
        self._cooldown_until = 0.0

    def defer(self, seconds: float) -> None:
        """Extend the cooldown for all workers (including other pending requests)."""
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + max(0.0, seconds))

    @contextmanager
    def slot(self, deadline: float | None = None) -> Iterator[None]:
        timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not self._slots.acquire(timeout=timeout):
            raise TransientError("请求等待并发槽时超过总时限")
        try:
            while True:
                with self._lock:
                    now = time.monotonic()
                    ready = self._cooldown_until
                    if self._last_started is not None:
                        ready = max(ready, self._last_started + self.min_interval_seconds)
                    if deadline is not None and (now >= deadline or ready >= deadline):
                        raise TransientError("请求节流或退避等待超过总时限")
                    wait = ready - now
                    if wait <= 0:
                        self._last_started = now
                        break
                # Never hold the timing lock while sleeping: 429 may extend cooldown.
                time.sleep(wait)
            yield
        finally:
            self._slots.release()
