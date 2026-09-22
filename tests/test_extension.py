"""扩展示例测试：验证"加一个智能体只需一个文件"。"""

from __future__ import annotations

from agentcode.agents import EchoAgent
from agentcode.cli import main
from agentcode.core.registry import default_registry
from agentcode.llm import ScriptedLLM
from agentcode.tools import ToolRegistry


def test_echo_agent_is_registered():
    assert "echo" in default_registry
    assert "回显" in default_registry.description_of("echo")


def test_echo_agent_runs_without_llm_call():
    llm = ScriptedLLM([])  # 空脚本：一旦被调用就会抛错
    result = EchoAgent(llm=llm, tools=ToolRegistry()).run("演示任务")
    assert result.answer == "已收到任务：演示任务"
    assert llm.calls == []


def test_cli_can_run_registered_agent(capsys):
    code = main(["run", "--agent", "echo", "--llm", "mock", "--task", "演示任务"])
    assert code == 0
    assert "已收到任务：演示任务" in capsys.readouterr().out
