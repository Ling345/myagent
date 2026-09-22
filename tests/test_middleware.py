"""中间件测试。"""

from __future__ import annotations

import io
import time

import pytest

from agentcode.core.context import RunContext
from agentcode.core.errors import LLMError
from agentcode.core.result import TokenUsage
from agentcode.middleware import (
    LoggingMiddleware,
    RetryMiddleware,
    TimeoutMiddleware,
    TokenUsageMiddleware,
)


def test_retry_middleware_retries_until_success():
    attempts = {"n": 0}

    def call(messages):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise LLMError("暂时失败")
        return "成功"

    wrapped = RetryMiddleware(max_retries=3, base_delay=0.0).wrap_llm(call, RunContext())
    assert wrapped([]) == "成功"
    assert attempts["n"] == 3


def test_retry_middleware_raises_after_exhausting_retries():
    attempts = {"n": 0}

    def call(messages):
        attempts["n"] += 1
        raise LLMError("始终失败")

    wrapped = RetryMiddleware(max_retries=2, base_delay=0.0).wrap_llm(call, RunContext())
    with pytest.raises(LLMError):
        wrapped([])
    assert attempts["n"] == 3


def test_retry_middleware_uses_exponential_backoff():
    delays: list[float] = []

    def call(messages):
        raise LLMError("失败")

    middleware = RetryMiddleware(max_retries=2, base_delay=0.5, sleep=delays.append)
    with pytest.raises(LLMError):
        middleware.wrap_llm(call, RunContext())([])
    assert delays == [0.5, 1.0]


def test_timeout_middleware_raises_llm_error():
    wrapped = TimeoutMiddleware(timeout=0.05).wrap_llm(lambda messages: time.sleep(0.5), RunContext())
    with pytest.raises(LLMError):
        wrapped([])


def test_timeout_middleware_returns_error_string_for_tools():
    wrapped = TimeoutMiddleware(timeout=0.05).wrap_tool(
        lambda name, raw: time.sleep(0.5), RunContext()
    )
    assert "超时" in wrapped("slow", "")


def test_logging_middleware_records_events():
    stream = io.StringIO()
    middleware = LoggingMiddleware(stream=stream)
    wrapped = middleware.wrap_llm(lambda messages: "回复内容", RunContext())
    assert wrapped([{"role": "user", "content": "你好"}]) == "回复内容"
    assert len(middleware.records) == 2
    assert "调用模型" in stream.getvalue()
    assert "模型返回" in stream.getvalue()


def test_token_usage_middleware_counts_delta():
    ctx = RunContext()

    def call(messages):
        ctx.usage.add({"prompt_tokens": 10, "completion_tokens": 5})
        return "回复"

    middleware = TokenUsageMiddleware()
    wrapped = middleware.wrap_llm(call, ctx)
    wrapped([{"role": "user", "content": "你好"}])
    assert middleware.usage.calls == 1
    assert middleware.usage.prompt_tokens == 10
    assert middleware.usage.completion_tokens == 5


def test_token_usage_middleware_counts_tool_calls():
    middleware = TokenUsageMiddleware()
    wrapped = middleware.wrap_tool(lambda name, raw: "结果", RunContext())
    wrapped("search", "北京")
    assert middleware.tool_calls == 1


def test_usage_dataclass_totals():
    usage = TokenUsage()
    usage.add({"prompt_tokens": 3, "completion_tokens": 4})
    assert usage.total_tokens == 7
    assert usage.calls == 1
