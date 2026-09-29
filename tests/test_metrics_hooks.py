"""采集点测试：运行、模型调用、key 熔断、沙箱执行（全部离线）。"""

from __future__ import annotations

import queue

import pytest

from agentcode.metrics import METRICS
from agentcode.web.runs import RunRegistry


@pytest.fixture(autouse=True)
def _clean_metrics():
    METRICS.reset()
    yield
    METRICS.reset()


def _queue_stream(box: "queue.Queue"):
    def make_stream():
        while True:
            item = box.get()
            if item is None:
                return
            yield item

    return make_stream


def _rendered() -> str:
    return METRICS.render()


# ------------------------------------------------------------------ 运行


def test_successful_run_is_counted_by_agent():
    box: queue.Queue = queue.Queue()
    registry = RunRegistry()
    record = registry.start(_queue_stream(box), agent="react", task="t")
    box.put({"type": "answer", "data": {"answer": "好了"}})
    box.put(None)
    record.thread.join(timeout=5)

    text = _rendered()
    assert 'agentcode_runs_total{agent="react",result="ok"} 1.0' in text
    assert 'agentcode_run_seconds_count{agent="react"} 1' in text


def test_run_that_blew_up_is_counted_as_error():
    def boom():
        yield {"type": "status", "data": {"message": "开始"}}
        raise RuntimeError("炸了")

    registry = RunRegistry()
    record = registry.start(boom, agent="test_gen", task="t")
    record.thread.join(timeout=5)

    assert 'agentcode_runs_total{agent="test_gen",result="error"} 1.0' in _rendered()


def test_run_without_answer_is_counted_as_empty():
    box: queue.Queue = queue.Queue()
    registry = RunRegistry()
    record = registry.start(_queue_stream(box), agent="coding", task="t")
    box.put({"type": "status", "data": {"message": "跑一半"}})
    box.put(None)
    record.thread.join(timeout=5)

    assert 'agentcode_runs_total{agent="coding",result="empty"} 1.0' in _rendered()


def test_run_duration_is_recorded():
    box: queue.Queue = queue.Queue()
    registry = RunRegistry()
    record = registry.start(_queue_stream(box), agent="echo", task="t")
    record.started_at -= 2.5  # 假装跑了 2.5 秒
    box.put({"type": "answer", "data": {"answer": "好了"}})
    box.put(None)
    record.thread.join(timeout=5)

    hist = METRICS.histogram("agentcode_run_seconds", "")
    assert hist.total(agent="echo") >= 2.5
