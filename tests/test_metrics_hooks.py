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


# ------------------------------------------------------------------ 模型调用


class _Boom(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__("boom")
        self.status_code = status_code


def _patch_llm_clients(monkeypatch, behaviors: dict):
    """按 key 提供不同的假客户端。"""
    from types import SimpleNamespace

    from agentcode.llm import openai_compatible as module

    def fake_shared_client(api_key: str, base_url: str, timeout: float):
        create = behaviors[api_key]
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(module, "_shared_client", fake_shared_client)


def _ok_client(text: str):
    from types import SimpleNamespace

    def create(**kwargs):
        delta = SimpleNamespace(content=text)
        return [SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)]

    return create


def _failing_client(status: int):
    def create(**kwargs):
        raise _Boom(status)

    return create


def test_llm_success_is_counted(monkeypatch):
    from agentcode.llm.openai_compatible import OpenAICompatibleLLM

    _patch_llm_clients(monkeypatch, {"sk-a": _ok_client("好")})
    llm = OpenAICompatibleLLM(model="m", api_key="sk-a", base_url="https://x.invalid")
    llm.think([{"role": "user", "content": "x"}])

    text = _rendered()
    # key 用**下标**而不是脱敏后的明文：指标要往监控系统送，少暴露一点是一点
    assert 'agentcode_llm_calls_total{key="0",result="ok"} 1.0' in text
    assert 'agentcode_llm_call_seconds_count{key="0"} 1' in text


def test_llm_failure_is_counted(monkeypatch):
    from agentcode.core.errors import LLMError
    from agentcode.llm.openai_compatible import OpenAICompatibleLLM

    _patch_llm_clients(monkeypatch, {"sk-a": _failing_client(500)})
    llm = OpenAICompatibleLLM(model="m", api_key="sk-a", base_url="https://x.invalid")
    with pytest.raises(LLMError):
        llm.think([{"role": "user", "content": "x"}])

    assert 'agentcode_llm_calls_total{key="0",result="failed"} 1.0' in _rendered()


def test_llm_rotation_shows_up_per_key_index(monkeypatch):
    from agentcode.llm.openai_compatible import OpenAICompatibleLLM

    _patch_llm_clients(monkeypatch, {"sk-bad": _failing_client(429), "sk-good": _ok_client("好")})
    llm = OpenAICompatibleLLM(
        model="m", api_keys=["sk-bad", "sk-good"], base_url="https://x.invalid"
    )
    assert llm.think([{"role": "user", "content": "x"}]) == "好"

    text = _rendered()
    assert 'agentcode_llm_calls_total{key="0",result="failed"} 1.0' in text
    assert 'agentcode_llm_calls_total{key="1",result="ok"} 1.0' in text


# ------------------------------------------------------------------ key 熔断


def test_keypool_trip_is_counted():
    from agentcode.llm.keypool import KeyPool

    pool = KeyPool(["sk-a", "sk-b"], failure_threshold=2)
    state = pool.acquire()
    pool.report_failure(state)
    # 指标早就声明了（HELP/TYPE 一直在），这里查的是"还没有序列"
    assert 'agentcode_keypool_trips_total{key="0"}' not in _rendered()

    pool.report_failure(state)
    assert 'agentcode_keypool_trips_total{key="0"} 1.0' in _rendered()


def test_keypool_success_resets_but_keeps_the_trip_count():
    from agentcode.llm.keypool import KeyPool

    pool = KeyPool(["sk-a"], failure_threshold=1)
    state = pool.acquire()
    pool.report_failure(state)
    pool.report_success(state)

    assert state.failures == 0
    # 熔断次数是"一共发生过几次"，不会因为恢复就归零
    assert 'agentcode_keypool_trips_total{key="0"} 1.0' in _rendered()


# ------------------------------------------------------------------ 沙箱执行


def test_local_sandbox_success_is_counted(tmp_path):
    from agentcode.tools.sandbox import LocalBackend

    LocalBackend(tmp_path).run("print(1)", 5)

    text = _rendered()
    assert 'agentcode_sandbox_runs_total{backend="local",result="ok"} 1.0' in text
    assert 'agentcode_sandbox_seconds_count{backend="local"} 1' in text


def test_sandbox_failure_is_counted(tmp_path):
    from agentcode.tools.sandbox import LocalBackend

    LocalBackend(tmp_path).run("raise SystemError('炸')", 5)
    assert 'agentcode_sandbox_runs_total{backend="local",result="failed"} 1.0' in _rendered()


def test_sandbox_timeout_is_counted_separately(tmp_path):
    """超时和"代码自己报错"是两回事：超时通常意味着模型写了个死循环。"""
    from agentcode.tools.sandbox import LocalBackend

    LocalBackend(tmp_path).run("while True: pass", 0.5)
    assert 'agentcode_sandbox_runs_total{backend="local",result="timeout"} 1.0' in _rendered()


def test_unavailable_docker_is_counted_as_error(tmp_path):
    from agentcode.tools.sandbox import DockerBackend, ExecutionError

    backend = DockerBackend(tmp_path, docker_executable="definitely-not-a-docker")
    with pytest.raises(ExecutionError):
        backend.run("print(1)", 5)
    assert 'agentcode_sandbox_runs_total{backend="docker",result="error"} 1.0' in _rendered()
