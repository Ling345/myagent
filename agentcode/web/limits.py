"""限流与并发控制：防止一个用户（或一台机器）把服务打满。

两层保护：

1. **频率**：滑动窗口，每个 key（用户或 IP）每分钟最多 N 次请求；
2. **并发**：同一个 key 同时最多 M 个任务在跑——一次 run 会调用模型十几次，
   只在开头检查每日额度是挡不住并发洪峰的。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

DEFAULT_PER_MINUTE = 30
DEFAULT_MAX_CONCURRENT = 2
WINDOW_SECONDS = 60.0


@dataclass(frozen=True)
class LimitDecision:
    """一次限流判断的结果。"""

    allowed: bool
    reason: str = ""
    retry_after: int = 0


class UsageGuard:
    """按 key 做的频率与并发闸门（线程安全）。"""

    def __init__(
        self,
        per_minute: int = DEFAULT_PER_MINUTE,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_minute = max(1, int(per_minute))
        self.max_concurrent = max(1, int(max_concurrent))
        self._clock = clock
        self._events: dict[str, deque[float]] = {}
        self._active: dict[str, int] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 频率

    def _window(self, key: str, now: float) -> deque[float]:
        """取该 key 的窗口，并丢掉过期的时间点。"""
        window = self._events.setdefault(key, deque())
        while window and now - window[0] > WINDOW_SECONDS:
            window.popleft()
        return window

    def check_request(self, key: str) -> LimitDecision:
        """只做频率检查（用于查询类接口）。"""
        now = self._clock()
        with self._lock:
            window = self._window(key, now)
            if len(window) >= self.per_minute:
                wait = max(1, int(WINDOW_SECONDS - (now - window[0])) + 1)
                return LimitDecision(
                    False,
                    f"请求过于频繁（每分钟最多 {self.per_minute} 次），请 {wait} 秒后再试。",
                    wait,
                )
            window.append(now)
            return LimitDecision(True)

    # ------------------------------------------------------------------ 并发

    def acquire(self, key: str) -> LimitDecision:
        """频率检查通过后，再占用一个并发名额。"""
        decision = self.check_request(key)
        if not decision.allowed:
            return decision
        with self._lock:
            active = self._active.get(key, 0)
            if active >= self.max_concurrent:
                return LimitDecision(
                    False,
                    f"你已有 {active} 个任务在运行（同时最多 {self.max_concurrent} 个），"
                    "等其中一个结束再试。",
                    5,
                )
            self._active[key] = active + 1
            return LimitDecision(True)

    def release(self, key: str) -> None:
        """释放并发名额。"""
        with self._lock:
            active = self._active.get(key, 0)
            if active <= 1:
                self._active.pop(key, None)
            else:
                self._active[key] = active - 1

    def active_count(self, key: str) -> int:
        """当前该 key 有几个任务在跑。"""
        with self._lock:
            return self._active.get(key, 0)

    def reset(self) -> None:
        """清空全部计数（测试用）。"""
        with self._lock:
            self._events.clear()
            self._active.clear()
