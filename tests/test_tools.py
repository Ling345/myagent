"""工具系统测试。"""

from __future__ import annotations

import pytest

from agentcode.core.errors import ToolError
from agentcode.tools import ToolRegistry, calculator, current_time, register_builtin_tools


def test_invoke_unknown_tool_returns_chinese_error():
    registry = ToolRegistry()
    assert "未注册" in registry.invoke("nope", "x")


def test_invoke_passes_keyword_arguments():
    registry = ToolRegistry()
    registry.register_tool("add", "求和", lambda a, b: str(int(a) + int(b)))
    assert registry.invoke("add", "a=2, b=3") == "5"


def test_invoke_passes_plain_string_as_positional_argument():
    registry = ToolRegistry()
    registry.register_tool("echo", "回显", lambda text: f"收到：{text}")
    assert registry.invoke("echo", "北京天气") == "收到：北京天气"


def test_tool_exception_becomes_error_string():
    registry = ToolRegistry()
    registry.register_tool("boom", "抛错", lambda: 1 / 0)
    assert "执行失败" in registry.invoke("boom", "")


def test_tool_argument_mismatch_is_reported():
    registry = ToolRegistry()
    registry.register_tool("need_two", "需要两个参数", lambda a, b: a + b)
    assert "参数不匹配" in registry.invoke("need_two", "只有一个")


def test_duplicate_registration_requires_overwrite():
    registry = ToolRegistry()
    registry.register_tool("dup", "第一个", lambda: "a")
    with pytest.raises(ToolError):
        registry.register_tool("dup", "第二个", lambda: "b")
    registry.register_tool("dup", "第二个", lambda: "b", overwrite=True)
    assert registry.invoke("dup") == "b"


def test_decorator_registers_tool_with_docstring():
    registry = ToolRegistry()

    @registry.tool(description="打招呼", parameters={"name": "姓名"})
    def greet(name: str) -> str:
        """这里不会作为描述使用。"""
        return f"你好，{name}"

    assert registry.names() == ["greet"]
    assert "打招呼" in registry.describe()
    assert "姓名" in registry.describe()
    assert registry.invoke("greet", "小明") == "你好，小明"


def test_calculator_evaluates_expression():
    assert calculator("(15*1 + 30 - 5) / 2") == "20"


def test_calculator_rejects_non_arithmetic_input():
    assert "无法解析" in calculator("__import__('os').system('ls')")


def test_current_time_returns_iso_string():
    assert current_time("Asia/Shanghai").startswith("20")


def test_register_builtin_tools_adds_expected_names():
    registry = register_builtin_tools(ToolRegistry())
    assert "calculator" in registry.names()
    assert "current_time" in registry.names()
    assert "web_search" in registry.names()


def test_web_search_without_key_returns_chinese_error(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    registry = register_builtin_tools(ToolRegistry())
    assert "SERPAPI_API_KEY" in registry.invoke("web_search", "北京天气")
