"""中间件基类。

中间件返回的是"新的调用函数"，因此既能观察（日志、用量），
也能改变行为（重试、超时），无需两套扩展点。
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from agentcode.core.context import RunContext
from agentcode.llm.base import Message

LLMCall = Callable[[Sequence[Message]], str]
ToolCall = Callable[[str, str], str]


class Middleware:
    """默认什么都不做，子类只覆盖需要的方法。"""

    name: str = "middleware"

    def wrap_llm(self, call: LLMCall, ctx: RunContext) -> LLMCall:
        """包装一次大语言模型调用。"""
        return call

    def wrap_tool(self, call: ToolCall, ctx: RunContext) -> ToolCall:
        """包装一次工具调用。"""
        return call

    def describe(self) -> dict[str, Any]:
        """返回中间件的简要信息，用于日志与调试。"""
        return {"name": self.name}
