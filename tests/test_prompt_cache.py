"""提示词前缀稳定（供应商侧 prompt cache 的前提）+ 缓存用量解析（离线）。"""

from __future__ import annotations

from types import SimpleNamespace

from agentcode.agents import CodingAgent, ReActAgent, TestGenerationAgent
from agentcode.llm import ScriptedLLM
from agentcode.llm.openai_compatible import OpenAICompatibleLLM
from agentcode.metrics import PROMPT_CACHE
from agentcode.tools import ToolRegistry


def _tools() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register_tool("get_weather", "查天气", lambda city: f"{city}晴", {"city": "城市"})
    return tools


def _react(replies: list[str]) -> tuple[ReActAgent, ScriptedLLM]:
    llm = ScriptedLLM(replies)
    return ReActAgent(llm=llm, tools=_tools()), llm


# ---------------------------------------------------------------- 前缀稳定


def test_the_system_message_is_identical_across_runs():
    """供应商只缓存"最长公共前缀"：稳定部分必须一字不差，否则永远命不中。"""
    first, first_llm = _react(["Thought: 收尾\nAction: Finish[第一个答案]"])
    first.run("北京天气如何")
    second, second_llm = _react(["Thought: 收尾\nAction: Finish[第二个答案]"])
    second.run("上海天气如何")

    first_system = first_llm.calls[0][0]
    second_system = second_llm.calls[0][0]
    assert first_system["role"] == "system"
    assert first_system["content"] == second_system["content"]
    # 任务描述不能混进稳定块
    assert "北京天气如何" not in first_system["content"]
    assert "上海天气如何" not in second_system["content"]


def test_the_variable_parts_live_in_the_user_message():
    agent, llm = _react(["Thought: 收尾\nAction: Finish[好]"])
    agent.run("北京天气如何")

    user = llm.calls[0][-1]
    assert user["role"] == "user"
    assert "北京天气如何" in user["content"]
    assert "第一轮" in user["content"]  # 没有更早对话时的提示语


def test_the_system_message_stays_stable_while_steps_grow():
    agent, llm = _react(
        [
            "Thought: 先查天气\nAction: get_weather[北京]",
            "Thought: 够了\nAction: Finish[北京晴]",
        ]
    )
    agent.run("北京天气如何")

    assert len(llm.calls) == 2
    assert llm.calls[0][0] == llm.calls[1][0]  # system 一字不差
    # 工具观察只进 user（而且追加在后面，前缀也没被破坏）
    assert "北京晴" not in llm.calls[0][1]["content"]
    assert "北京晴" in llm.calls[1][1]["content"]


def test_coding_keeps_its_discipline_in_the_stable_block():
    llm = ScriptedLLM(["Thought: 收尾\nAction: Finish[完成]"])
    agent = CodingAgent(llm=llm, tools=_tools())
    agent.run("随便写点东西")

    system = llm.calls[0][0]["content"]
    assert system.count("写测试文件") == 1
    assert "run_python" in system
    assert "全绿" in system
    assert "随便写点东西" not in system


def test_test_gen_keeps_its_discipline_in_the_stable_block():
    llm = ScriptedLLM(["Thought: 收尾\nAction: Finish[完成]"])
    agent = TestGenerationAgent(llm=llm, tools=_tools())
    # 测试生成需要代码工具；这里只关心提示词，先把前置检查要的工具补上
    tools = ToolRegistry()
    for name in ("read_file", "write_file", "run_python"):
        tools.register_tool(name, f"{name} 工具", lambda *args, **kwargs: "ok")
    agent = TestGenerationAgent(llm=llm, tools=tools)
    agent.run("为 calculator.py 生成测试")

    system = llm.calls[0][0]["content"]
    assert "不要修改被测源文件" in system
    assert "边界" in system
    assert "calculator.py" not in system


# ---------------------------------------------------------------- 缓存用量


def _client_with_usage(usage) -> tuple[OpenAICompatibleLLM, list[dict]]:
    requests: list[dict] = []

    class Completions:
        def create(self, **kwargs):
            requests.append(kwargs)
            message = SimpleNamespace(content="好", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=usage)

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    llm = OpenAICompatibleLLM(
        model="m", api_key="sk-x", base_url="https://x", stream=False, client=client
    )
    return llm, requests


def test_deepseek_style_cache_tokens_are_parsed_and_counted():
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=10,
        total_tokens=110,
        prompt_cache_hit_tokens=80,
        prompt_cache_miss_tokens=20,
    )
    llm, _ = _client_with_usage(usage)
    hit_before = PROMPT_CACHE.value(result="hit")
    miss_before = PROMPT_CACHE.value(result="miss")

    llm.think([{"role": "user", "content": "x"}])

    assert llm.last_usage["cache_hit_tokens"] == 80
    assert llm.last_usage["cache_miss_tokens"] == 20
    assert PROMPT_CACHE.value(result="hit") == hit_before + 80
    assert PROMPT_CACHE.value(result="miss") == miss_before + 20


def test_openai_style_cached_tokens_are_mapped_to_cache_hits():
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=10,
        total_tokens=110,
        prompt_tokens_details=SimpleNamespace(cached_tokens=64),
    )
    llm, _ = _client_with_usage(usage)

    llm.think([{"role": "user", "content": "x"}])

    assert llm.last_usage["cache_hit_tokens"] == 64
    assert "cache_miss_tokens" not in llm.last_usage


def test_usage_without_cache_fields_stays_clean():
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12)
    llm, _ = _client_with_usage(usage)

    llm.think([{"role": "user", "content": "x"}])

    assert "cache_hit_tokens" not in llm.last_usage
    assert "cache_miss_tokens" not in llm.last_usage
