"""测试生成智能体：注册、缺工具时报错、提示词纪律、以及真的生成能跑的测试。"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentcode.agents import TestGenerationAgent
from agentcode.cli import main
from agentcode.core.registry import default_registry
from agentcode.llm.mock import demo_responses_llm
from agentcode.tools import ToolRegistry
from agentcode.tools.builtin import register_demo_tools
from agentcode.tools.code import register_code_tools

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample_code" / "calculator.py"


@pytest.fixture
def workspace(tmp_path) -> Path:
    """一个放好被测文件的工作目录。"""
    target = tmp_path / "sandbox"
    target.mkdir()
    shutil.copyfile(SAMPLE, target / "calculator.py")
    return target


def _tools(root: Path) -> ToolRegistry:
    registry = ToolRegistry()
    register_code_tools(registry, root=root, timeout=30, output_limit=4000)
    return registry


def test_test_gen_agent_is_registered():
    assert "test_gen" in default_registry
    assert "测试" in default_registry.description_of("test_gen")
    assert TestGenerationAgent.default_max_steps == 16


def test_test_gen_requires_code_tools():
    """没有代码工具时立刻说清楚，不要白白空转十几步。"""
    tools = ToolRegistry()
    register_demo_tools(tools)  # 只有演示工具，没有 read_file/write_file/run_python
    agent = TestGenerationAgent(llm=demo_responses_llm("test_gen"), tools=tools)

    result = agent.run("为 calculator.py 生成测试")

    assert result.success is False
    assert "需要代码工具" in result.error
    assert "AGENT_ALLOW_CODE_TOOLS" in result.error


def test_test_gen_prompt_forbids_modifying_source(workspace):
    llm = demo_responses_llm("test_gen")
    agent = TestGenerationAgent(llm=llm, tools=_tools(workspace))
    agent.run("为 calculator.py 生成测试")

    prompt = llm.calls[0][-1]["content"]
    assert "不要修改被测源文件" in prompt
    assert "边界" in prompt


def test_test_gen_generates_tests_that_actually_pass(workspace):
    """端到端：生成测试 → 真的跑 → 我用 pytest 独立复跑一遍确认。"""
    agent = TestGenerationAgent(llm=demo_responses_llm("test_gen"), tools=_tools(workspace))

    result = agent.run("为 calculator.py 生成 pytest 测试用例")

    assert result.success is True
    test_file = workspace / "test_calculator.py"
    assert test_file.is_file()
    assert "def test_" in test_file.read_text(encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", test_file.name],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    # 源码必须原封不动
    assert (workspace / "calculator.py").read_text(encoding="utf-8") == SAMPLE.read_text(
        encoding="utf-8"
    )


def test_test_gen_answers_without_test_report(workspace):
    agent = TestGenerationAgent(llm=demo_responses_llm("test_gen"), tools=_tools(workspace))
    answer = agent.run("为 calculator.py 生成 pytest 测试用例").answer
    assert "test_calculator.py" in answer
    assert "pytest.main" not in answer
    assert "退出码" not in answer


def test_cli_file_option_copies_source(workspace, monkeypatch, capsys):
    """--file：把被测文件复制进工作目录，并据此组织任务。"""
    monkeypatch.setenv("AGENT_CODE_ROOT", str(workspace))
    code = main(
        [
            "run",
            "--agent",
            "test_gen",
            "--llm",
            "mock",
            "--file",
            str(SAMPLE),
            "--quiet",
        ]
    )
    output = capsys.readouterr().out

    assert code == 0
    assert "已把被测文件复制到工作目录" in output
    assert (workspace / "test_calculator.py").is_file()


def test_cli_file_option_reports_missing_file(tmp_path, capsys):
    code = main(["run", "--agent", "test_gen", "--file", str(tmp_path / "不存在.py")])
    assert code == 2
    assert "找不到文件" in capsys.readouterr().out
