"""调用预算（工程上限，不是供应商配额）。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from .base import BudgetExhausted


@dataclass
class BudgetTracker:
    max_live_calls: int
    max_total_tokens: int
    used_live_calls: int = 0
    used_total_tokens: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def exhausted(self) -> bool:
        with self._lock:
            if self.max_live_calls > 0 and self.used_live_calls >= self.max_live_calls:
                return True
            return self.max_total_tokens > 0 and self.used_total_tokens >= self.max_total_tokens

    def check_and_reserve(self) -> None:
        """发起真实调用前检查并预留一次调用名额。max_live_calls<=0 表示不限次数。"""
        with self._lock:
            if 0 < self.max_live_calls <= self.used_live_calls:
                raise BudgetExhausted(
                    f"已达到单次运行真实调用上限 {self.max_live_calls}"
                )
            if self.max_total_tokens > 0 and self.used_total_tokens >= self.max_total_tokens:
                raise BudgetExhausted(
                    f"已达到单次运行 token 上限 {self.max_total_tokens}"
                )
            self.used_live_calls += 1

    def record_tokens(self, total_tokens: int | None) -> None:
        with self._lock:
            if total_tokens:
                self.used_total_tokens += total_tokens

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "max_live_calls": self.max_live_calls,
                "used_live_calls": self.used_live_calls,
                "max_total_tokens": self.max_total_tokens,
                "used_total_tokens": self.used_total_tokens,
            }
