"""ReAct 的原生工具调用循环：用 tool_calls 干活，走不通就回退文本协议（离线）。"""

from __future__ import annotations

from agentcode.agents import ReActAgent
from agentcode.core.errors import LLMError
from agentcode.llm import ScriptedLLM
from agentcode.llm.base import BaseLLM, LLMReply, ToolCallRequest
from agentcode.metrics import TOOL_CALLING
from agentcode.tools import ToolRegistry
from agentcode.tools.base import FINISH_TOOL_NAME


def _weather_tools() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register_tool("get_weather", "查天气", lambda city: f"{city}晴", {"city": "城市"})
    return tools


class FakeToolLLM(BaseLLM):
    """会说"工具调用"的假后端：按脚本返回 LLMReply 或抛异常。"""

    name = "fake-tools"
    supports_tools = True

    def __init__(self, script: list) -> None:
        super().__init__()
        self.script = list(script)
        self.requests: list[list] = []
        self.tool_schemas: list = []

    def think(self, messages, temperature=None) -> str:
        reply = self.complete(messages)
        if not reply.text:
            raise LLMError("这是工具调用，没有文本")
        return reply.text

    def complete(self, messages, *, tools=None, temperature=None) -> LLMReply:
        self.requests.append(list(messages))
        self.tool_schemas.append(tools)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _call(name: str, arguments: str, call_id: str = "call-1") -> ToolCallRequest:
    return ToolCallRequest(id=call_id, name=name, arguments=arguments)


def test_native_loop_calls_the_tool_and_finishes_with_its_answer():
    llm = FakeToolLLM(
        [
            LLMReply(tool_calls=[_call("get_weather", '{"city": "北京"}')]),
            LLMReply(tool_calls=[_call(FINISH_TOOL_NAME, '{"answer": "北京晴，适合出门"}', "call-2")]),
        ]
    )

    result = ReActAgent(llm=llm, tools=_weather_tools()).run("北京天气如何")

    assert result.success is True
    assert result.answer == "北京晴，适合出门"
    assert result.steps[0].tool == "get_weather"
    assert result.steps[0].tool_input == '{"city": "北京"}'
    assert result.steps[0].observation == "北京晴"
    # 工具结果是以 role=tool 的消息喂回去的，不是拼在一段文本里
    follow_up = llm.requests[1]
    assert follow_up[-1]["role"] == "tool"
    assert follow_up[-1]["content"] == "北京晴"
    assert follow_up[-1]["tool_call_id"] == "call-1"
    assert llm.tool_schemas[0] is not None
    assert TOOL_CALLING.value(mode="native") >= 1


def test_native_loop_treats_plain_text_as_the_final_answer():
    llm = FakeToolLLM([LLMReply(text="直接给答案也可以。")])

    result = ReActAgent(llm=llm, tools=_weather_tools()).run("随便问问")

    assert result.success is True
    assert result.answer == "直接给答案也可以。"


def test_native_loop_reports_unknown_tools_and_keeps_going():
    llm = FakeToolLLM(
        [
            LLMReply(tool_calls=[_call("查天气", '{"city": "北京"}')]),
            LLMReply(tool_calls=[_call(FINISH_TOOL_NAME, '{"answer": "改用别的办法"}', "call-2")]),
        ]
    )

    result = ReActAgent(llm=llm, tools=_weather_tools()).run("北京天气如何")

    assert "未注册的工具" in result.steps[0].observation
    assert result.success is True


def test_native_loop_stops_at_max_steps():
    replies = [
        LLMReply(tool_calls=[_call("get_weather", '{"city": "北京"}', f"call-{index}")])
        for index in range(4)
    ]

    result = ReActAgent(llm=FakeToolLLM(replies), tools=_weather_tools(), max_steps=2).run("北京天气")

    assert result.success is False
    assert "最大步数" in result.error
    assert len(result.steps) == 2


def test_text_mode_never_sends_tool_schemas():
    llm = FakeToolLLM([LLMReply(text="Thought: 直接答\nAction: Finish[文本协议]")])

    result = ReActAgent(
        llm=llm, tools=_weather_tools(), tool_calling="text"
    ).run("北京天气如何")

    assert result.answer == "文本协议"
    # 文本协议下不该把工具 schema 交给模型（请求里 tools 是 None）
    assert all(item is None for item in llm.tool_schemas)


def test_a_backend_without_tool_support_uses_the_text_protocol():
    llm = ScriptedLLM(
        [
            "Thought: 先查天气\nAction: get_weather[北京]",
            "Thought: 够了\nAction: Finish[北京晴]",
        ]
    )

    result = ReActAgent(llm=llm, tools=_weather_tools()).run("北京天气如何")

    assert result.answer == "北京晴"
    assert result.steps[0].tool == "get_weather"


def test_a_provider_that_rejects_tools_falls_back_to_text():
    """服务商不认 tools 参数时不能把任务做死：退回文本协议重跑一遍。"""
    error = LLMError("调用大语言模型失败：tools is not supported")
    llm = FakeToolLLM(
        [
            error,
            LLMReply(text="Thought: 先查天气\nAction: get_weather[上海]"),
            LLMReply(text="Thought: 够了\nAction: Finish[上海晴]"),
        ]
    )
    agent = ReActAgent(llm=llm, tools=_weather_tools())

    result = agent.run("上海天气如何")

    assert result.success is True
    assert result.answer == "上海晴"
    assert agent.last_tool_calling_mode == "fallback"
    assert TOOL_CALLING.value(mode="fallback") >= 1
