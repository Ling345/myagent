"""限流、并发闸门与单次 token 预算。"""

from __future__ import annotations

import pytest

from agentcode.agents import ReActAgent
from agentcode.core.errors import AgentCodeError
from agentcode.llm.base import BaseLLM
from agentcode.middleware import BudgetMiddleware
from agentcode.tools import ToolRegistry
from agentcode.web.limits import UsageGuard


class _Clock:
    """可手动推进的时钟，便于测窗口过期。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ------------------------------------------------------------------ 频率


def test_rate_limit_blocks_after_quota():
    guard = UsageGuard(per_minute=3, max_concurrent=2)
    assert [guard.check_request("u").allowed for _ in range(3)] == [True, True, True]

    blocked = guard.check_request("u")
    assert blocked.allowed is False
    assert "过于频繁" in blocked.reason
    assert blocked.retry_after >= 1


def test_rate_limit_window_expires():
    clock = _Clock()
    guard = UsageGuard(per_minute=1, clock=clock)
    assert guard.check_request("u").allowed is True
    assert guard.check_request("u").allowed is False

    clock.advance(61)
    assert guard.check_request("u").allowed is True


def test_rate_limit_is_per_key():
    guard = UsageGuard(per_minute=1)
    assert guard.check_request("a").allowed is True
    assert guard.check_request("b").allowed is True  # 别的 key 不受影响
    assert guard.check_request("a").allowed is False


# ------------------------------------------------------------------ 并发


def test_concurrency_limit_blocks_extra_runs():
    guard = UsageGuard(per_minute=100, max_concurrent=1)
    assert guard.acquire("u").allowed is True
    assert guard.active_count("u") == 1

    blocked = guard.acquire("u")
    assert blocked.allowed is False
    assert "同时最多 1 个" in blocked.reason


def test_release_frees_slot():
    guard = UsageGuard(per_minute=100, max_concurrent=1)
    guard.acquire("u")
    guard.release("u")
    assert guard.active_count("u") == 0
    assert guard.acquire("u").allowed is True


def test_release_without_acquire_is_safe():
    guard = UsageGuard()
    guard.release("从未占用的 key")
    assert guard.active_count("从未占用的 key") == 0


def test_acquire_also_counts_towards_rate_limit():
    """并发检查也要计入频率，避免"占坑"绕过每分钟限制。"""
    guard = UsageGuard(per_minute=1, max_concurrent=10)
    assert guard.acquire("u").allowed is True
    guard.release("u")
    assert guard.acquire("u").allowed is False


def test_reset_clears_everything():
    guard = UsageGuard(per_minute=1, max_concurrent=1)
    guard.acquire("u")
    guard.reset()
    assert guard.active_count("u") == 0
    assert guard.check_request("u").allowed is True


# ------------------------------------------------------------- 单次预算


class _BigUsageLLM(BaseLLM):
    """每次调用都上报大量 token 的假模型。"""

    name = "big-usage"

    def think(self, messages, temperature=None) -> str:
        self._last_usage = {"prompt_tokens": 8000, "completion_tokens": 8000}
        return "Thought: 继续\nAction: Finish[完成]"


def test_budget_middleware_stops_run_when_exceeded():
    agent = ReActAgent(
        llm=_BigUsageLLM(), tools=ToolRegistry(), middlewares=[BudgetMiddleware(max_tokens=1000)]
    )
    with pytest.raises(AgentCodeError) as excinfo:
        agent.run("随便问问")
    assert "token 预算" in str(excinfo.value)
    assert "AGENT_RUN_TOKEN_BUDGET" in str(excinfo.value)


def test_budget_middleware_allows_run_within_budget():
    agent = ReActAgent(
        llm=_BigUsageLLM(),
        tools=ToolRegistry(),
        middlewares=[BudgetMiddleware(max_tokens=100000)],
    )
    assert agent.run("随便问问").answer == "完成"


def test_budget_middleware_rejects_bad_limit():
    with pytest.raises(ValueError):
        BudgetMiddleware(max_tokens=0)
