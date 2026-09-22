"""内置工具：网页搜索、计算器、当前时间，以及离线演示用的假工具。"""

from __future__ import annotations

import ast
import operator
import os
from datetime import datetime
from typing import Any, Callable

import requests

from agentcode.tools.base import ToolRegistry

# --------------------------------------------------------------------- 计算器

_BIN_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type, Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _eval_node(node: ast.AST) -> Any:
    """递归求值，只允许数字、括号与四则运算，避免使用 eval 带来的注入风险。"""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"不支持的常量类型：{type(node.value).__name__}")
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))
    raise ValueError(f"不支持的表达式节点：{type(node).__name__}")


def calculator(expression: str) -> str:
    """计算一个算术表达式，例如 ``(15*1 + 30 - 5)``。"""
    try:
        tree = ast.parse(str(expression), mode="eval")
        value = _eval_node(tree)
    except ZeroDivisionError:
        return "错误：计算器遇到除数为零。"
    except (SyntaxError, ValueError) as exc:
        return f"错误：计算器无法解析表达式 '{expression}'（{exc}）。"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


# ----------------------------------------------------------------------- 时间

def current_time(timezone: str = "Asia/Shanghai") -> str:
    """返回指定时区的当前时间（ISO 格式）。"""
    try:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo(timezone))
    except Exception:  # noqa: BLE001 - 时区数据缺失等情况回退本机时间
        now = datetime.now()
        return f"{now.isoformat(timespec='seconds')}（时区 {timezone} 不可用，已回退本机时区）"
    return now.isoformat(timespec="seconds")


# ----------------------------------------------------------------------- 搜索

def web_search(query: str, api_key: str | None = None, max_results: int = 3) -> str:
    """用 SerpApi 做网页搜索，返回前若干条结果的标题与摘要。"""
    key = api_key or os.getenv("SERPAPI_API_KEY")
    if not key:
        return "错误：未配置 SERPAPI_API_KEY，无法执行网页搜索。"
    try:
        from serpapi import SerpApiClient

        client = SerpApiClient(
            {
                "engine": "google",
                "q": query,
                "api_key": key,
                "gl": "cn",
                "hl": "zh-cn",
            }
        )
        results = client.get_dict()
    except Exception as exc:  # noqa: BLE001 - 网络或配额问题统一转为提示
        return f"错误：网页搜索失败（{exc}）。"

    answer_box = results.get("answer_box") or {}
    if answer_box.get("answer"):
        return str(answer_box["answer"])
    knowledge = results.get("knowledge_graph") or {}
    if knowledge.get("description"):
        return str(knowledge["description"])
    organic = results.get("organic_results") or []
    if not organic:
        return f"没有找到关于 '{query}' 的搜索结果。"
    snippets = [
        f"[{index}] {item.get('title', '')}\n{item.get('snippet', '')}"
        for index, item in enumerate(organic[:max_results], start=1)
    ]
    return "\n\n".join(snippets)


# ------------------------------------------------------------------- 假工具

def mock_weather(city: str) -> str:
    """返回固定的演示天气数据（不联网）。"""
    return f"{city}当前天气：晴，气温 24 摄氏度（演示数据）"


def mock_attraction(city: str, weather: str = "晴") -> str:
    """返回固定的演示景点推荐（不联网）。"""
    return f"结合{city}的{weather}天气，推荐故宫与颐和园（演示数据）。"


# -------------------------------------------------------------------- 注册

def register_builtin_tools(registry: ToolRegistry, include_search: bool = True) -> ToolRegistry:
    """注册真实可用的内置工具。"""
    registry.register_tool(
        "calculator",
        "计算一个算术表达式，例如 (15*2-5)/3。",
        calculator,
        {"expression": "需要计算的算术表达式"},
    )
    registry.register_tool(
        "current_time",
        "查询指定时区的当前时间。",
        current_time,
        {"timezone": "时区名，例如 Asia/Shanghai"},
    )
    if include_search:
        registry.register_tool(
            "web_search",
            "网页搜索引擎，用于查询时事、事实以及模型知识库之外的信息。",
            web_search,
            {"query": "搜索关键词"},
        )
    return registry


def register_demo_tools(registry: ToolRegistry) -> ToolRegistry:
    """注册离线演示工具，供 ``--llm mock`` 场景使用。"""
    registry.register_tool(
        "get_weather",
        "查询城市天气（离线演示数据）。",
        mock_weather,
        {"city": "城市名称，例如 北京"},
    )
    registry.register_tool(
        "get_attraction",
        "根据城市与天气推荐景点（离线演示数据）。",
        mock_attraction,
        {"city": "城市名称", "weather": "天气描述"},
    )
    return registry
