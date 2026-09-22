"""Reflection 智能体测试。"""

from __future__ import annotations

from agentcode.agents import ReflectionAgent
from agentcode.llm import ScriptedLLM
from agentcode.tools import ToolRegistry


def test_reflection_stops_when_no_improvement_needed():
    llm = ScriptedLLM(["def f(): return 1", "该实现已经是最优，无需改进"])
    result = ReflectionAgent(llm=llm, tools=ToolRegistry(), max_iterations=3).run("写一个函数")

    assert "无需改进" in result.answer
    assert result.success is True
    assert result.extra["iterations"] == 1


def test_reflection_iterates_when_feedback_requires_changes():
    llm = ScriptedLLM(
        [
            "def find_primes(n): return [x for x in range(2, n) if all(x % d for d in range(2, x))]",
            "试除法效率较低，建议改用埃拉托斯特尼筛法。",
            "def find_primes(n): return []  # 优化后的实现",
            "现在已经是常规最优解，无需改进。",
        ]
    )
    result = ReflectionAgent(llm=llm, tools=ToolRegistry(), max_iterations=3).run("找素数")

    assert result.success is True
    assert result.extra["iterations"] == 2
    assert "优化后的实现" in result.extra["final_version"]
    assert len(result.steps) == 4


def test_reflection_respects_max_iterations():
    llm = ScriptedLLM(["初版"], fallback="还需要继续改进。")
    result = ReflectionAgent(llm=llm, tools=ToolRegistry(), max_iterations=2).run("写点东西")

    assert result.extra["iterations"] == 2
    assert result.answer == "还需要继续改进。"


def test_reflection_rejects_invalid_max_iterations():
    try:
        ReflectionAgent(llm=ScriptedLLM([]), tools=ToolRegistry(), max_iterations=0)
    except ValueError as exc:
        assert "max_iterations" in str(exc)
    else:  # pragma: no cover - 未抛异常即视为失败
        raise AssertionError("max_iterations=0 应当抛出 ValueError")
