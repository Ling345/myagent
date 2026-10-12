"""工具的另一套入口：JSON Schema 交给模型、按 JSON 参数直接调用（离线）。"""

from __future__ import annotations

from agentcode.tools import ToolRegistry
from agentcode.tools.base import FINISH_TOOL_NAME


def _tools() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_tool(
        "get_weather",
        "查询城市天气",
        lambda city: f"{city}晴",
        {"city": "城市名称，例如 北京"},
    )
    return registry


def test_json_schemas_describe_every_tool_plus_finish():
    schemas = _tools().json_schemas()
    by_name = {item["function"]["name"]: item for item in schemas}

    assert set(by_name) == {"get_weather", FINISH_TOOL_NAME}
    for item in schemas:
        assert item["type"] == "function"
        assert item["function"]["description"]


def test_parameters_become_a_string_object_schema():
    schema = {item["function"]["name"]: item for item in _tools().json_schemas()}["get_weather"]
    parameters = schema["function"]["parameters"]

    assert parameters["type"] == "object"
    assert parameters["properties"]["city"] == {
        "type": "string",
        "description": "城市名称，例如 北京",
    }
    assert parameters["required"] == ["city"]


def test_a_tool_without_parameters_gets_an_empty_schema():
    registry = ToolRegistry()
    registry.register_tool("now", "看当前时间", lambda: "12:00")

    schema = {item["function"]["name"]: item for item in registry.json_schemas()}["now"]

    assert schema["function"]["parameters"] == {
        "type": "object",
        "properties": {},
        "required": [],
    }


def test_finish_tool_takes_the_answer():
    schema = {
        item["function"]["name"]: item for item in _tools().json_schemas()
    }[FINISH_TOOL_NAME]

    assert schema["function"]["parameters"]["required"] == ["answer"]
    assert "最终答案" in schema["function"]["parameters"]["properties"]["answer"]["description"]


def test_invoke_json_calls_the_tool_with_keyword_arguments():
    assert _tools().invoke_json("get_weather", '{"city": "北京"}') == "北京晴"


def test_invoke_json_reports_missing_arguments_in_chinese():
    message = _tools().invoke_json("get_weather", "{}")

    assert "参数不匹配" in message


def test_invoke_json_reports_broken_json():
    message = _tools().invoke_json("get_weather", "不是 JSON")

    assert "不是合法的 JSON" in message


def test_invoke_json_reports_unknown_tool():
    message = _tools().invoke_json("查天气", '{"city": "北京"}')

    assert "未注册的工具" in message
