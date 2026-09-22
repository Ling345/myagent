"""Plan-and-Solve 智能体测试。"""

from __future__ import annotations

from agentcode.agents import PlanAndSolveAgent
from agentcode.llm import ScriptedLLM
from agentcode.tools import ToolRegistry


def test_plan_and_solve_runs_each_step():
    llm = ScriptedLLM(["```python\n[\"算苹果数\", \"求和\"]\n```", "周一15个", "总共45个"])
    result = PlanAndSolveAgent(llm=llm, tools=ToolRegistry()).run("三天共卖出多少苹果")

    assert result.answer == "总共45个"
    assert len(result.steps) == 2
    assert result.steps[1].observation == "总共45个"
    assert result.extra["plan"] == ["算苹果数", "求和"]


def test_plan_and_solve_returns_failure_when_plan_unparsable():
    llm = ScriptedLLM(["我想不出步骤。"])
    result = PlanAndSolveAgent(llm=llm, tools=ToolRegistry()).run("随便问点什么")

    assert result.success is False
    assert "无法生成有效的行动计划" in result.error


def test_plan_and_solve_truncates_plan_by_max_steps():
    plan = "```python\n[\"一\", \"二\", \"三\", \"四\"]\n```"
    llm = ScriptedLLM([plan, "结果1", "结果2"])
    result = PlanAndSolveAgent(llm=llm, tools=ToolRegistry(), max_steps=2).run("四步任务")

    assert len(result.steps) == 2
    assert result.extra["plan_truncated"] is True
