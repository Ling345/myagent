"""Coding 智能体测试：用 scripted 模型驱动一次"写错 → 跑失败 → 改对 → 通过"。"""

from __future__ import annotations

from agentcode.agents import CodingAgent
from agentcode.config import Settings
from agentcode.llm import ScriptedLLM
from agentcode.tools import ToolRegistry
from agentcode.tools.code import register_code_tools


def _code_tools(root) -> ToolRegistry:
    registry = ToolRegistry()
    register_code_tools(registry, root=root, timeout=10, output_limit=2000)
    return registry


def test_coding_agent_default_steps_are_larger():
    assert CodingAgent.default_max_steps == 20
    assert Settings().coding_steps == 20
    assert Settings().max_steps_for("coding") == 20
    assert Settings().max_steps_for("react") == Settings().max_steps


def test_coding_agent_fixes_failing_code(tmp_path):
    """初版写成加法，测试要求乘法：必须真的跑一次、看到失败、再改对。"""
    script = [
        # 第 1 步：写测试
        'Thought: 先写测试\nAction: write_file[path="test_calc.py", '
        'content="from calc import multiply\n\n\ndef test_multiply():\n    assert multiply(3, 4) == 12\n"]',
        # 第 2 步：写实现（故意写错）
        'Thought: 先写一个初版\nAction: write_file[path="calc.py", '
        'content="def multiply(a, b):\n    return a + b\n"]',
        # 第 3 步：跑测试 → 会失败
        "Thought: 跑一遍测试\nAction: run_python[code=\"import pytest, sys; sys.exit(pytest.main(['-q']))\"]",
        # 第 4 步：修正实现
        'Thought: 加法写错了，改成乘法\nAction: write_file[path="calc.py", '
        'content="def multiply(a, b):\n    return a * b\n"]',
        # 第 5 步：再跑 → 通过
        "Thought: 再跑一遍\nAction: run_python[code=\"import pytest, sys; sys.exit(pytest.main(['-q']))\"]",
        # 第 6 步：收尾
        "Thought: 全绿了\nAction: Finish[实现 multiply 并让 1 个用例通过]",
    ]
    agent = CodingAgent(llm=ScriptedLLM(script), tools=_code_tools(tmp_path), max_steps=12)

    result = agent.run("实现 multiply(a, b) 并写一个测试")

    assert result.success is True
    assert "通过" in result.answer
    # 真的执行过代码，并且第一次是失败的、第二次是成功的
    observations = [step.observation or "" for step in result.steps if step.tool == "run_python"]
    assert len(observations) == 2
    assert "退出码：1" in observations[0]
    assert "退出码：0" in observations[1]
    # 文件被真的改了
    assert "a * b" in (tmp_path / "calc.py").read_text(encoding="utf-8")


def test_coding_agent_reports_run_failure_without_lying(tmp_path):
    """代码一直跑不通时，必须如实报告，不许假装成功。"""
    broken = (
        'Thought: 写个坏的\nAction: write_file[path="bad.py", content="raise SystemError(\'炸了\')\n"]'
    )
    agent = CodingAgent(
        llm=ScriptedLLM(
            [broken, "Thought: 直接跑\nAction: run_python[code=\"import bad\"]"], fallback=broken
        ),
        tools=_code_tools(tmp_path),
        max_steps=4,
    )

    result = agent.run("把 bad.py 跑起来")

    assert result.success is False
    assert "最大步数" in result.error


def test_coding_agent_prompt_mentions_test_discipline(tmp_path):
    llm = ScriptedLLM(["Thought: 收尾\nAction: Finish[完成]"])
    agent = CodingAgent(llm=llm, tools=_code_tools(tmp_path))
    agent.run("随便写点东西")

    prompt = llm.calls[0][-1]["content"]
    assert "写测试文件" in prompt
    assert "run_python" in prompt
    assert "全绿" in prompt
