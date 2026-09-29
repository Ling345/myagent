"""指标内核测试（离线）。"""

from __future__ import annotations

import threading

from agentcode.metrics import Metrics, escape_label_value


def test_counter_accumulates_and_renders():
    metrics = Metrics()
    counter = metrics.counter("agentcode_demo_total", "演示用计数器")
    counter.inc()
    counter.inc(2)

    text = metrics.render()
    assert "# TYPE agentcode_demo_total counter" in text
    assert "# HELP agentcode_demo_total 演示用计数器" in text
    assert "agentcode_demo_total 3.0" in text


def test_counter_separates_label_values():
    metrics = Metrics()
    counter = metrics.counter("agentcode_http_requests_total", "HTTP 请求数")
    counter.inc(path="/api/run", status="200")
    counter.inc(path="/api/run", status="200")
    counter.inc(path="/api/me", status="401")

    assert counter.value(path="/api/run", status="200") == 2
    text = metrics.render()
    assert 'agentcode_http_requests_total{path="/api/me",status="401"} 1.0' in text
    assert 'agentcode_http_requests_total{path="/api/run",status="200"} 2.0' in text


def test_labels_are_rendered_in_a_stable_order():
    metrics = Metrics()
    counter = metrics.counter("agentcode_x_total", "x")
    counter.inc(b="2", a="1")
    # 标签顺序不影响归并：同一组标签换个顺序写，应当落在同一条序列上
    counter.inc(a="1", b="2")
    assert counter.value(a="1", b="2") == 2
    assert 'agentcode_x_total{a="1",b="2"} 2.0' in metrics.render()


def test_histogram_buckets_are_cumulative():
    metrics = Metrics()
    hist = metrics.histogram("agentcode_demo_seconds", "耗时", buckets=(0.1, 0.5, 1.0))
    for value in (0.05, 0.2, 0.7, 3.0):
        hist.observe(value)

    text = metrics.render()
    assert 'agentcode_demo_seconds_bucket{le="0.1"} 1' in text
    assert 'agentcode_demo_seconds_bucket{le="0.5"} 2' in text
    # 边界写法跟 Prometheus Go 客户端对齐：1.0 渲染成 "1"
    assert 'agentcode_demo_seconds_bucket{le="1"} 3' in text
    assert 'agentcode_demo_seconds_bucket{le="+Inf"} 4' in text
    assert "agentcode_demo_seconds_count 4" in text
    assert "agentcode_demo_seconds_sum 3.95" in text


def test_histogram_records_labels():
    metrics = Metrics()
    hist = metrics.histogram("agentcode_demo_seconds", "耗时", buckets=(1.0,))
    hist.observe(0.5, backend="docker")
    hist.observe(2.0, backend="docker")
    hist.observe(0.1, backend="local")

    assert hist.count(backend="docker") == 2
    assert hist.count(backend="local") == 1
    text = metrics.render()
    assert 'agentcode_demo_seconds_count{backend="docker"} 2' in text


def test_unknown_series_reports_zero():
    metrics = Metrics()
    counter = metrics.counter("agentcode_y_total", "y")
    assert counter.value(path="/nope") == 0
    hist = metrics.histogram("agentcode_z_seconds", "z")
    assert hist.count(kind="nope") == 0


def test_escape_label_value():
    assert escape_label_value('a"b\\c\nd') == 'a\\"b\\\\c\\nd'


def test_render_escapes_weird_label_values():
    metrics = Metrics()
    counter = metrics.counter("agentcode_w_total", "w")
    counter.inc(account='李"四')
    assert 'account="李\\"四"' in metrics.render()


def test_reset_clears_everything():
    metrics = Metrics()
    metrics.counter("agentcode_r_total", "r").inc()
    metrics.histogram("agentcode_r_seconds", "r").observe(1.0)
    metrics.reset()
    assert metrics.counter("agentcode_r_total", "r").value() == 0
    assert metrics.histogram("agentcode_r_seconds", "r").count() == 0


def test_counter_is_thread_safe():
    metrics = Metrics()
    counter = metrics.counter("agentcode_t_total", "t")

    def bump():
        for _ in range(500):
            counter.inc()

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert counter.value() == 2000


def test_render_is_sorted_for_stable_output():
    metrics = Metrics()
    counter = metrics.counter("agentcode_s_total", "s")
    counter.inc(which="b")
    counter.inc(which="a")
    lines = [line for line in metrics.render().splitlines() if line.startswith("agentcode_s_total{")]
    assert lines == [
        'agentcode_s_total{which="a"} 1.0',
        'agentcode_s_total{which="b"} 1.0',
    ]


def test_snapshot_is_flat_and_includes_histogram_counts():
    """告警靠它取两次快照做差，别再出现"方法漏提交"这种事。"""
    metrics = Metrics()
    metrics.counter("agentcode_snap_total", "s").inc(which="a", amount=3)
    metrics.histogram("agentcode_snap_seconds", "s").observe(1.0, kind="x")

    snap = metrics.snapshot()
    assert snap['agentcode_snap_total{which="a"}'] == 3.0
    assert snap['agentcode_snap_seconds_count{kind="x"}'] == 1.0
