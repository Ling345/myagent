"""ReAct 智能体测试。"""

from __future__ import annotations

from agentcode.agents import ReActAgent
from agentcode.llm import ScriptedLLM
from agentcode.tools import ToolRegistry


def _weather_tools() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register_tool("get_weather", "查天气", lambda city: f"{city}晴")
    return tools


def test_react_agent_calls_tool_then_finishes():
    llm = ScriptedLLM(
        [
            "Thought: 先查天气\nAction: get_weather[北京]",
            "Thought: 信息够了\nAction: Finish[北京晴，适合去故宫]",
        ]
    )
    result = ReActAgent(llm=llm, tools=_weather_tools()).run("北京天气如何")

    assert result.success is True
    assert result.answer == "北京晴，适合去故宫"
    assert result.steps[0].tool == "get_weather"
    assert result.steps[0].observation == "北京晴"
    assert result.steps[1].answer == "北京晴，适合去故宫"


def test_react_agent_stops_at_max_steps():
    llm = ScriptedLLM(["Thought: 继续\nAction: get_weather[北京]"])
    result = ReActAgent(llm=llm, tools=_weather_tools(), max_steps=2).run("北京天气如何")

    assert result.success is False
    assert "最大步数" in result.error
    assert len(result.steps) == 2


def test_react_agent_recovers_from_unparsable_output():
    llm = ScriptedLLM(
        [
            "我先想想，不打算按格式回答。",
            "Thought: 这次按格式来\nAction: Finish[补上了]",
        ]
    )
    result = ReActAgent(llm=llm, tools=_weather_tools()).run("北京天气如何")

    assert result.success is True
    assert result.answer == "补上了"
    assert result.steps[0].error == "模型输出缺少 Action 字段"


def test_react_agent_passes_tool_observations_into_next_prompt():
    llm = ScriptedLLM(
        [
            "Thought: 查天气\nAction: get_weather[上海]",
            "Thought: 根据观察作答\nAction: Finish[上海晴]",
        ]
    )
    ReActAgent(llm=llm, tools=_weather_tools()).run("上海天气如何")
    second_prompt = llm.calls[1][-1]["content"]
    assert "上海晴" in second_prompt


def test_react_agent_reports_unknown_tool_as_observation():
    llm = ScriptedLLM(
        [
            "Thought: 用不存在的工具\nAction: 查天气[北京]",
            "Thought: 换个方式\nAction: Finish[改用直接回答]",
        ]
    )
    result = ReActAgent(llm=llm, tools=_weather_tools()).run("北京天气如何")

    assert "未注册的工具" in result.steps[0].observation
    assert result.success is True
