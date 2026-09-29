"""进程内指标：计数器与直方图，按 Prometheus 文本格式暴露。

为什么需要它：服务跑起来之后，"模型调用失败率多少、容器执行是不是变慢了、
哪把 key 被熔断了、谁在刷额度"这些问题一个都答不上来。日志是一行行的，
要统计只能人肉 grep。

设计取舍：

- 进程内计数，**不引第三方依赖**（没有 prometheus_client）；
- 只做 counter 与 histogram —— 这两种覆盖九成需求，加起来一百多行；
- 多进程部署时各进程各算各的；真有那一天，把 ``/metrics`` 换成远端聚合即可，
  采集点的代码不用动。

输出格式是 Prometheus 文本格式：它是事实标准，Grafana / Prometheus 直接能读，
而且本身就是纯文本、肉眼可读，等于白拿。
"""

from __future__ import annotations

import threading
from typing import Iterable, Sequence

#: 默认分桶（秒）。覆盖毫秒级到几十秒的调用。
DEFAULT_BUCKETS: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)


def escape_label_value(value: str) -> str:
    """按 Prometheus 文本格式转义标签值：反斜杠、双引号、换行。"""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels_key(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """标签要排序后再当 key，否则 {a,b} 与 {b,a} 会被当成两条序列。"""
    return tuple(sorted((str(name), str(value)) for name, value in labels.items()))


def _render_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    parts = [f'{name}="{escape_label_value(value)}"' for name, value in labels]
    return "{" + ",".join(parts) + "}"


class Counter:
    """单调递增的计数器。"""

    def __init__(self, registry: "Metrics", name: str, help_text: str) -> None:
        self.registry = registry
        self.name = name
        self.help_text = help_text

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        """加一笔。标签的不同组合各自算一条序列。"""
        self.registry._add(self.name, _labels_key(labels), float(amount))

    def value(self, **labels: str) -> float:
        """取当前值；没记录过就是 0。"""
        return self.registry._values.get((self.name, _labels_key(labels)), 0.0)


class Histogram:
    """直方图：记分布，能算分位数（在 Prometheus 那边算）。"""

    def __init__(
        self,
        registry: "Metrics",
        name: str,
        help_text: str,
        buckets: Sequence[float] = DEFAULT_BUCKETS,
    ) -> None:
        self.registry = registry
        self.name = name
        self.help_text = help_text
        self.buckets = tuple(sorted(float(b) for b in buckets))

    def observe(self, value: float, **labels: str) -> None:
        """记一次观测。"""
        self.registry._observe(self.name, _labels_key(labels), float(value), self.buckets)

    def count(self, **labels: str) -> int:
        """观测次数。"""
        return self.registry._hist_counts.get((self.name, _labels_key(labels)), 0)

    def total(self, **labels: str) -> float:
        """观测值之和。"""
        return self.registry._hist_sums.get((self.name, _labels_key(labels)), 0.0)

    def bucket_counts(self, **labels: str) -> dict[float, int]:
        """各桶的**累计**计数。"""
        key = (self.name, _labels_key(labels))
        return {
            bound: self.registry._hist_buckets.get((key, bound), 0) for bound in self.buckets
        }


class Metrics:
    """一个指标注册表。线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._hist_counts: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
        self._hist_sums: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._hist_buckets: dict[tuple[tuple[str, tuple[tuple[str, str], ...]], float], int] = {}
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}

    # ------------------------------------------------------------------ 定义

    def counter(self, name: str, help_text: str) -> Counter:
        """取（或创建）一个计数器。同名重复调用返回同一个对象。"""
        with self._lock:
            existing = self._counters.get(name)
            if existing is None:
                existing = Counter(self, name, help_text)
                self._counters[name] = existing
            return existing

    def histogram(
        self,
        name: str,
        help_text: str,
        buckets: Sequence[float] = DEFAULT_BUCKETS,
    ) -> Histogram:
        """取（或创建）一个直方图。"""
        with self._lock:
            existing = self._histograms.get(name)
            if existing is None:
                existing = Histogram(self, name, help_text, buckets)
                self._histograms[name] = existing
            return existing

    # ------------------------------------------------------------------ 写入

    def _add(self, name: str, labels: tuple[tuple[str, str], ...], amount: float) -> None:
        with self._lock:
            self._values[(name, labels)] = self._values.get((name, labels), 0.0) + amount

    def _observe(
        self,
        name: str,
        labels: tuple[tuple[str, str], ...],
        value: float,
        buckets: Sequence[float],
    ) -> None:
        with self._lock:
            key = (name, labels)
            self._hist_counts[key] = self._hist_counts.get(key, 0) + 1
            self._hist_sums[key] = self._hist_sums.get(key, 0.0) + value
            for bound in buckets:
                if value <= bound:
                    bucket_key = (key, bound)
                    self._hist_buckets[bucket_key] = self._hist_buckets.get(bucket_key, 0) + 1

    # ------------------------------------------------------------------ 输出

    def render(self) -> str:
        """渲染成 Prometheus 文本格式。行序稳定，方便对比。"""
        with self._lock:
            lines: list[str] = []
            for name in sorted(self._counters):
                counter = self._counters[name]
                lines.append(f"# HELP {name} {counter.help_text}")
                lines.append(f"# TYPE {name} counter")
                series: Iterable[tuple[tuple[tuple[str, str], ...], float]] = sorted(
                    (
                        (labels, value)
                        for (metric, labels), value in self._values.items()
                        if metric == name
                    ),
                    key=lambda item: item[0],
                )
                for labels, value in series:
                    lines.append(f"{name}{_render_labels(labels)} {_format(value)}")

            for name in sorted(self._histograms):
                histogram = self._histograms[name]
                lines.append(f"# HELP {name} {histogram.help_text}")
                lines.append(f"# TYPE {name} histogram")
                keys = sorted(
                    labels
                    for (metric, labels) in self._hist_counts
                    if metric == name
                )
                for labels in keys:
                    key = (name, labels)
                    for bound in histogram.buckets:
                        count = self._hist_buckets.get((key, bound), 0)
                        if not count and not labels:
                            continue
                        merged = labels + (("le", _format_bound(bound)),)
                        lines.append(f"{name}_bucket{_render_labels(merged)} {count}")
                    total = self._hist_counts.get(key, 0)
                    merged_inf = labels + (("le", "+Inf"),)
                    lines.append(f"{name}_bucket{_render_labels(merged_inf)} {total}")
                    lines.append(f"{name}_sum{_render_labels(labels)} {_format(self._hist_sums.get(key, 0.0))}")
                    lines.append(f"{name}_count{_render_labels(labels)} {total}")
            return "\n".join(lines) + ("\n" if lines else "")

    def reset(self) -> None:
        """清空所有数据（测试与压测用）。"""
        with self._lock:
            self._values.clear()
            self._hist_counts.clear()
            self._hist_sums.clear()
            self._hist_buckets.clear()


def _format(value: float) -> str:
    """整数写成 x.0，小数保留三位有效数字，避免浮点长尾巴。"""
    if value == int(value):
        return f"{int(value)}.0"
    return f"{round(value, 6):g}"


def _format_bound(bound: float) -> str:
    return f"{bound:g}"


#: 全局指标表。采集点直接用下面这些命名对象，调用很短。
METRICS = Metrics()

HTTP_REQUESTS = METRICS.counter(
    "agentcode_http_requests_total", "HTTP 请求数，按路径与状态码"
)
HTTP_SECONDS = METRICS.histogram("agentcode_http_request_seconds", "HTTP 请求耗时（秒）")
RUNS = METRICS.counter("agentcode_runs_total", "任务运行数，按智能体与结果")
RUN_SECONDS = METRICS.histogram("agentcode_run_seconds", "任务运行耗时（秒）")
LLM_CALLS = METRICS.counter("agentcode_llm_calls_total", "模型调用数，按结果")
LLM_SECONDS = METRICS.histogram("agentcode_llm_call_seconds", "模型调用耗时（秒）")
KEY_TRIPS = METRICS.counter("agentcode_keypool_trips_total", "API key 被熔断次数")
SANDBOX_RUNS = METRICS.counter("agentcode_sandbox_runs_total", "沙箱执行次数，按后端与结果")
SANDBOX_SECONDS = METRICS.histogram("agentcode_sandbox_seconds", "沙箱执行耗时（秒）")
REJECTIONS = METRICS.counter("agentcode_rejections_total", "被拒绝的请求，按原因")
ALERTS = METRICS.counter("agentcode_alerts_total", "触发的告警次数，按规则")
