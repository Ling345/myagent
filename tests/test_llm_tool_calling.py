"""模型层的工具调用：请求里带 schema、回复里读 tool_calls（离线）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentcode.core.errors import LLMError
from agentcode.llm import OpenAICompatibleLLM
from agentcode.llm.base import BaseLLM, LLMReply, ToolCallRequest


def _tool_call(call_id: str, name: str, arguments: str):
    return SimpleNamespace(
        id=call_id, type="function", function=SimpleNamespace(name=name, arguments=arguments)
    )


def _response(*, text: str | None = None, tool_calls=None, usage=None):
    message = SimpleNamespace(content=text, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class FakeCompletions:
    """记录请求参数，按脚本返回（可能是流式分块，也可能是一次性响应）。"""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        reply = self.script.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _llm(script: list, **kwargs) -> tuple[OpenAICompatibleLLM, FakeCompletions]:
    completions = FakeCompletions(script)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    llm = OpenAICompatibleLLM(
        model="fake-model",
        api_key="sk-fake",
        base_url="https://example.invalid/v1",
        client=client,
        **kwargs,
    )
    return llm, completions


def _chunk(text: str | None):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text))], usage=None)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查天气",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }
]


# ---------------------------------------------------------------- 原生工具调用


def test_complete_sends_the_tool_schema_and_reads_tool_calls():
    llm, completions = _llm([_response(tool_calls=[_tool_call("c1", "get_weather", '{"city": "北京"}')])])

    reply = llm.complete([{"role": "user", "content": "北京天气"}], tools=TOOLS)

    request = completions.requests[0]
    assert request["tools"] == TOOLS
    assert request["tool_choice"] == "auto"
    # 工具调用不能用流式：分块里没有 tool_calls
    assert request["stream"] is False
    assert reply.text == ""
    assert [call.name for call in reply.tool_calls] == ["get_weather"]
    assert reply.tool_calls[0].arguments == '{"city": "北京"}'
    assert reply.tool_calls[0].id == "c1"


def test_tool_call_arguments_can_be_parsed_into_a_dict():
    call = ToolCallRequest(id="c1", name="get_weather", arguments='{"city": "北京"}')

    assert call.arguments_dict() == {"city": "北京"}
    assert ToolCallRequest(id="c2", name="now", arguments="").arguments_dict() == {}
    assert ToolCallRequest(id="c3", name="x", arguments="不是 JSON").arguments_dict() == {}


def test_usage_is_recorded_for_tool_calls_too():
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=2, total_tokens=13)
    llm, _ = _llm([_response(tool_calls=[_tool_call("c1", "get_weather", "{}")], usage=usage)])

    llm.complete([{"role": "user", "content": "x"}], tools=TOOLS)

    assert llm.last_usage["total_tokens"] == 13


def test_a_provider_that_rejects_tools_raises_a_plain_llm_error():
    error = RuntimeError("tools is not supported")
    error.status_code = 400  # type: ignore[attr-defined]
    llm, _ = _llm([error], stream=False)

    with pytest.raises(LLMError):
        llm.complete([{"role": "user", "content": "x"}], tools=TOOLS)


# ---------------------------------------------------------------- 老行为不变


def test_complete_without_tools_keeps_the_old_streaming_behaviour():
    llm, completions = _llm([[_chunk("你好"), _chunk("，世界")]])

    reply = llm.complete([{"role": "user", "content": "打个招呼"}])

    assert "tools" not in completions.requests[0]
    assert completions.requests[0]["stream"] is True
    assert reply.text == "你好，世界"
    assert reply.tool_calls == []


def test_think_still_returns_plain_text():
    llm, _ = _llm([[_chunk("旧接口照旧")]])

    assert llm.think([{"role": "user", "content": "x"}]) == "旧接口照旧"


class _OnlyThink(BaseLLM):
    """只实现了 think 的老后端：不该被工具调用这件事拖累。"""

    name = "only-think"

    def think(self, messages, temperature=None) -> str:
        return "文本回复"


def test_a_backend_that_only_implements_think_ignores_tools():
    llm = _OnlyThink()

    reply = llm.complete([{"role": "user", "content": "x"}], tools=TOOLS)

    assert llm.supports_tools is False
    assert reply == LLMReply(text="文本回复")
    assert reply.tool_calls == []
