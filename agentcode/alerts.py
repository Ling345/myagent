"""阈值告警：从指标增量里看出"出事了"，然后推给人。

为什么不另起一套埋点：**指标已经是唯一的事实来源**。这里只在固定的时间点
给指标拍快照，用两次快照的差算窗口内的量——不需要任何调用方多写一行代码，
也就不会出现"指标和告警各记各的、对不上"这种经典问题。

四条规则，都只看最近一个窗口：

| 规则 | 触发条件 | 为什么 |
| --- | --- | --- |
| `http_error_rate` | 5xx 占比超过阈值，且样本数够 | "服务挂了"最直接的信号 |
| `llm_failure_rate` | 模型调用失败占比超过阈值，且样本数够 | 上游挂了 / key 全废了 |
| `sandbox_timeout` | 窗口内出现过超时 | 通常是模型写了个死循环，值得看一眼 |
| `keypool_trip` | 窗口内有 key 被熔断 | key 额度用完或失效 |

**4xx 不参与错误率**：没登录、参数写错这类是用户自己的问题，
半夜为它们报警只会让人学会无视告警。

推送要渠道（webhook），不配就只记日志——本地自己用没必要接。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from agentcode.metrics import METRICS, Metrics

#: 观察窗口（秒）
DEFAULT_WINDOW_SECONDS = 300.0
#: 同一个规则多久之内不重复推（秒）
DEFAULT_COOLDOWN_SECONDS = 900.0
#: 触发错误率告警的阈值
DEFAULT_ERROR_RATE = 0.5
#: 样本太少时不做比例判断——刚起来两个请求里有一个 5xx，不代表服务挂了
DEFAULT_MIN_SAMPLES = 20

Poster = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class Alert:
    """一条告警。"""

    rule: str
    message: str
    severity: str = "warning"
    details: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """推送用的载荷。"""
        return {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "details": dict(self.details),
        }


def _default_poster(url: str, payload: dict[str, Any]) -> None:
    """默认推送方式：POST 一段 JSON。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10):
        pass


class AlertMonitor:
    """按时拍指标快照、判断规则、按冷却推送。线程安全。"""

    def __init__(
        self,
        *,
        webhook: str = "",
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        error_rate: float = DEFAULT_ERROR_RATE,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        metrics: Metrics | None = None,
        clock: Callable[[], float] = time.monotonic,
        poster: Poster | None = None,
    ) -> None:
        self.webhook = str(webhook or "").strip()
        self.window_seconds = max(1.0, float(window_seconds))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.error_rate = min(1.0, max(0.0, float(error_rate)))
        self.min_samples = max(1, int(min_samples))
        self.metrics = metrics or METRICS
        self._clock = clock
        self._poster = poster or _default_poster
        self._samples: deque[tuple[float, dict[str, float]]] = deque()
        self._last_fired: dict[str, float] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 采样

    def evaluate(self) -> list[Alert]:
        """拍一次快照并判断规则；返回**尚未按冷却过滤**的告警。"""
        now = self._clock()
        with self._lock:
            self._samples.append((now, self.metrics.snapshot()))
            while len(self._samples) > 1 and now - self._samples[0][0] > self.window_seconds:
                self._samples.popleft()
            if len(self._samples) < 2:
                return []  # 只有一拍，没有增量可算
            base_time, base = self._samples[0]
            _, latest = self._samples[-1]
        return self._rules(base, latest)

    def check(self) -> list[Alert]:
        """判断规则并按冷却推送；返回**这次真的报出来**的告警。"""
        fired: list[Alert] = []
        now = self._clock()
        with self._lock:
            for alert in self.evaluate():
                last = self._last_fired.get(alert.rule)
                if last is not None and now - last < self.cooldown_seconds:
                    continue
                self._last_fired[alert.rule] = now
                fired.append(alert)
            for alert in fired:
                self._notify(alert)
        return fired

    # ------------------------------------------------------------------ 规则

    def _rules(self, base: dict[str, float], latest: dict[str, float]) -> list[Alert]:
        alerts: list[Alert] = []

        http_total = 0.0
        http_failed = 0.0
        for key, value in latest.items():
            if not key.startswith("agentcode_http_requests_total{"):
                continue
            delta = value - base.get(key, 0.0)
            if delta <= 0:
                continue
            http_total += delta
            if 'status="5' in key:
                http_failed += delta
        if http_total >= self.min_samples:
            ratio = http_failed / http_total
            if ratio >= self.error_rate:
                alerts.append(
                    Alert(
                        rule="http_error_rate",
                        message=(
                            f"最近窗口内 5xx 占比 {ratio:.0%}"
                            f"（{int(http_failed)}/{int(http_total)}），服务可能出问题了。"
                        ),
                        severity="critical",
                        details={"ratio": round(ratio, 3), "failed": int(http_failed),
                                 "total": int(http_total)},
                    )
                )

        llm_total = 0.0
        llm_failed = 0.0
        for key, value in latest.items():
            if not key.startswith("agentcode_llm_calls_total{"):
                continue
            delta = value - base.get(key, 0.0)
            if delta <= 0:
                continue
            llm_total += delta
            if 'result="failed"' in key:
                llm_failed += delta
        if llm_total >= self.min_samples:
            ratio = llm_failed / llm_total
            if ratio >= self.error_rate:
                alerts.append(
                    Alert(
                        rule="llm_failure_rate",
                        message=(
                            f"最近窗口内模型调用失败率 {ratio:.0%}"
                            f"（{int(llm_failed)}/{int(llm_total)}），检查上游或 API key。"
                        ),
                        severity="critical",
                        details={"ratio": round(ratio, 3), "failed": int(llm_failed),
                                 "total": int(llm_total)},
                    )
                )

        timeouts = self._delta_where(latest, base, "agentcode_sandbox_runs_total",
                                    'result="timeout"')
        if timeouts >= 1:
            alerts.append(
                Alert(
                    rule="sandbox_timeout",
                    message=f"最近窗口内有 {int(timeouts)} 次代码执行超时，通常是模型写了死循环。",
                    details={"count": int(timeouts)},
                )
            )

        trips = self._delta_sum(latest, base, "agentcode_keypool_trips_total")
        if trips >= 1:
            alerts.append(
                Alert(
                    rule="keypool_trip",
                    message=f"最近窗口内有 {int(trips)} 次 API key 熔断，可能是额度用完或 key 失效。",
                    severity="critical",
                    details={"count": int(trips)},
                )
            )
        return alerts

    @staticmethod
    def _delta_where(latest: dict[str, float], base: dict[str, float], prefix: str,
                     needle: str) -> float:
        total = 0.0
        for key, value in latest.items():
            if key.startswith(f"{prefix}{{") and needle in key:
                total += value - base.get(key, 0.0)
        return total

    @staticmethod
    def _delta_sum(latest: dict[str, float], base: dict[str, float], prefix: str) -> float:
        total = 0.0
        for key, value in latest.items():
            if key.startswith(prefix):
                total += value - base.get(key, 0.0)
        return total

    # ------------------------------------------------------------------ 推送

    def _notify(self, alert: Alert) -> None:
        """推一条告警：日志一定要打，webhook 配了才发。"""
        print(
            json.dumps(
                {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "alert": alert.to_payload()},
                ensure_ascii=False,
            ),
            flush=True,
        )
        if not self.webhook:
            return
        try:
            self._poster(self.webhook, alert.to_payload())
        except Exception as exc:  # noqa: BLE001 - 推不出去也不能影响服务
            print(
                json.dumps(
                    {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "alert_push_failed": str(exc)},
                    ensure_ascii=False,
                ),
                flush=True,
            )


def run_alert_loop(monitor: AlertMonitor, interval_seconds: float = 60.0) -> None:
    """后台循环：每 ``interval_seconds`` 秒判一次。"""
    while True:
        time.sleep(interval_seconds)
        try:
            monitor.check()
        except Exception as exc:  # noqa: BLE001 - 监控线程不能死
            print(json.dumps({"alert_loop_error": str(exc)}, ensure_ascii=False), flush=True)
