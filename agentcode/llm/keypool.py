"""API key 池与熔断器。

为什么需要：单个 key 是**单点故障**——额度打满、被限流、被风控，
整个服务立刻不可用。一个 key 池能把这些故障摊掉：

- **轮换**：请求在多个 key 之间轮流用，不会把一个 key 打爆；
- **熔断**：某个 key 连续失败到阈值就先摘掉，冷却期内不再碰它；
- **自动恢复**：冷却结束后放回池子，成功一次就彻底复位。

设计取舍：这是**进程内**熔断，不跨进程共享。单机部署够用；
将来多实例部署把状态挪到 Redis 即可，这里的接口不用改。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from agentcode.core.errors import ConfigError
from agentcode.metrics import KEY_TRIPS

#: 连续失败多少次就把这个 key 摘掉
DEFAULT_FAILURE_THRESHOLD = 3
#: 摘掉之后冷却多少秒再放回来试
DEFAULT_COOLDOWN_SECONDS = 60.0


def mask_key(key: str) -> str:
    """脱敏展示：保留首尾各 4 位。"""
    if not key:
        return "（未配置）"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * 6}{key[-4:]}"


@dataclass
class KeyState:
    """单个 key 的健康状态。"""

    key: str
    index: int
    failures: int = 0
    successes: int = 0
    #: 熔断到期的时间戳；为 0 表示没被熔断
    opened_until: float = 0.0

    def is_open(self, now: float) -> bool:
        """当前是否处于熔断冷却中。"""
        return self.opened_until > now

    def to_dict(self, now: float) -> dict[str, object]:
        """给日志/接口看的快照。"""
        return {
            "index": self.index,
            "key": mask_key(self.key),
            "failures": self.failures,
            "successes": self.successes,
            "state": "open" if self.is_open(now) else "closed",
            "cooldown_remaining": max(0.0, round(self.opened_until - now, 1)),
        }


class KeyPool:
    """一组 API key，负责挑一个健康的出来用，并记录成败。

    线程安全：智能体可能被多个请求并发使用，所有状态都在锁里改。
    """

    def __init__(
        self,
        keys: Iterable[str],
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown: float = DEFAULT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        unique: list[str] = []
        for raw in keys or ():
            candidate = str(raw or "").strip()
            if candidate and candidate not in unique:
                unique.append(candidate)
        if not unique:
            raise ConfigError("API key 池是空的：请在 .env 里配置 LLM_API_KEY 或 LLM_API_KEYS。")

        self._states = [KeyState(key=key, index=index) for index, key in enumerate(unique)]
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown = max(1.0, float(cooldown))
        self._clock = clock
        self._lock = threading.RLock()
        self._cursor = 0

    def __len__(self) -> int:
        return len(self._states)

    # ------------------------------------------------------------------ 选取

    def primary(self) -> KeyState:
        """第一个 key（不推进轮询游标，只用于展示与初始化）。"""
        return self._states[0]

    def acquire(self) -> KeyState | None:
        """取一个当前可用的 key；全部在冷却中时返回 ``None``。

        轮询选取，避免总压在同一个 key 上。
        """
        now = self._clock()
        with self._lock:
            healthy = [state for state in self._states if not state.is_open(now)]
            if not healthy:
                return None
            state = healthy[self._cursor % len(healthy)]
            self._cursor += 1
            return state

    # ------------------------------------------------------------------ 记账

    def report_success(self, state: KeyState) -> None:
        """一次成功：失败计数清零，熔断解除。"""
        with self._lock:
            state.successes += 1
            state.failures = 0
            state.opened_until = 0.0

    def report_failure(self, state: KeyState) -> None:
        """一次失败：累计到阈值就把这个 key 熔断一段时间。"""
        with self._lock:
            state.failures += 1
            if state.failures >= self.failure_threshold:
                state.opened_until = self._clock() + self.cooldown
                # 标签用 key 的**下标**：指标要往监控系统送，少暴露一点是一点。
                # 下标与 `agentcode config` 里那串脱敏 key 的顺序一致。
                KEY_TRIPS.inc(key=str(state.index))

    # ------------------------------------------------------------------ 观测

    def next_ready_in(self) -> float | None:
        """最快还有多少秒会有 key 恢复；全都健康时返回 None。"""
        now = self._clock()
        opened = [s.opened_until - now for s in self._states if s.is_open(now)]
        return min(opened) if opened else None

    def snapshot(self) -> list[dict[str, object]]:
        """全部 key 的脱敏状态快照。"""
        now = self._clock()
        with self._lock:
            return [state.to_dict(now) for state in self._states]


def parse_keys(raw: str | Sequence[str] | None) -> list[str]:
    """把 ``key1,key2`` / 换行分隔的文本解析成去重后的 key 列表。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        candidates: Iterable[str] = raw.replace(";", ",").replace("\r", ",").replace("\n", ",").split(",")
    else:
        candidates = raw
    unique: list[str] = []
    for item in candidates:
        candidate = str(item or "").strip()
        if candidate and candidate not in unique:
            unique.append(candidate)
    return unique
