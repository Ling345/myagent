"""智能体注册表与基类测试。"""

from __future__ import annotations

import pytest

from agentcode.core.agent import BaseAgent
from agentcode.core.context import RunContext
from agentcode.core.errors import AgentCodeError, AgentNotFoundError
from agentcode.core.registry import AgentRegistry
from agentcode.core.result import AgentResult
from agentcode.llm import ScriptedLLM
from agentcode.middleware import LoggingMiddleware
from agentcode.tools import ToolRegistry


class EchoAgent(BaseAgent):
    """测试用回显智能体。"""

    name = "echo_test"
    description = "回显"

    def run(self, task, context=None):
        ctx = context or self._new_context(task)
        answer = self._think([{"role": "user", "content": task}], ctx)
        self._remember(task, answer)
        return self._build_result(task, answer, ctx)


def test_registry_create_unknown_agent_raises():
    registry = AgentRegistry()
    with pytest.raises(AgentNotFoundError):
        registry.create("不存在", llm=ScriptedLLM([]), tools=ToolRegistry())


def test_registry_register_and_create():
    registry = AgentRegistry()
    registry.register("echo_test", EchoAgent, "回显")
    agent = registry.create("echo_test", llm=ScriptedLLM(["好的"]), tools=ToolRegistry())
    assert isinstance(agent, EchoAgent)
    assert registry.names() == ["echo_test"]
    assert "回显" in registry.describe()


def test_registry_rejects_duplicate_name():
    registry = AgentRegistry()
    registry.register("echo_test", EchoAgent, "回显")
    with pytest.raises(AgentCodeError):
        registry.register("echo_test", EchoAgent, "回显")


def test_base_agent_wraps_llm_with_middleware():
    stream_records: list[dict] = []

    class Recorder(LoggingMiddleware):
        def __init__(self):
            super().__init__(enabled=False)

        def _emit(self, text: str) -> None:  # pragma: no cover - 直接改写记录方式
            self.records.append({"event": text})
            stream_records.append({"event": text})

    agent = EchoAgent(
        llm=ScriptedLLM(["好的"]),
        tools=ToolRegistry(),
        middlewares=[Recorder()],
    )
    result = agent.run("你好")
    assert result.answer == "好的"
    assert result.success is True
    assert any("调用模型" in record["event"] for record in stream_records)


def test_result_records_token_usage():
    agent = EchoAgent(llm=ScriptedLLM(["好的"]), tools=ToolRegistry())
    result = agent.run("你好")
    assert result.usage.calls == 1
    assert result.usage.total_tokens > 0
    assert result.steps == []


def test_run_stores_task_and_answer_in_memory():
    agent = EchoAgent(llm=ScriptedLLM(["好的"]), tools=ToolRegistry())
    agent.run("你好")
    messages = agent.memory.get_messages()
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "你好"
    assert messages[1]["content"] == "好的"


def test_build_result_carries_extra_fields():
    agent = EchoAgent(llm=ScriptedLLM(["好的"]), tools=ToolRegistry())
    ctx = RunContext(task="你好")
    result = agent._build_result("你好", "答案", ctx, extra={"自定义": 1})
    assert isinstance(result, AgentResult)
    assert result.to_dict()["extra"] == {"自定义": 1}
