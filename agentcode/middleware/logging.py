"""日志中间件：记录并可选打印每次调用。"""

from __future__ import annotations

import sys
from typing import Sequence, TextIO

from agentcode.core.context import RunContext
from agentcode.llm.base import Message
from agentcode.middleware.base import LLMCall, Middleware, ToolCall

_MAX_PREVIEW = 60


def _preview(text: str, limit: int = _MAX_PREVIEW) -> str:
    """把长文本压缩成一行预览。"""
    single_line = " ".join(str(text).split())
    return single_line if len(single_line) <= limit else single_line[:limit] + "…"


class LoggingMiddleware(Middleware):
    """记录 LLM 与工具调用的次数与预览，便于复盘。"""

    name = "logging"

    def __init__(self, stream: TextIO | None = None, enabled: bool = True) -> None:
        self.stream = stream
        self.enabled = enabled
        self.records: list[dict[str, str]] = []
        self.llm_calls = 0
        self.tool_calls = 0

    def _emit(self, text: str) -> None:
        """写入记录并按需打印。"""
        self.records.append({"event": text})
        if not self.enabled:
            return
        target = self.stream or sys.stdout
        print(f"[agentcode] {text}", file=target)

    def wrap_llm(self, call: LLMCall, ctx: RunContext) -> LLMCall:
        """记录一次模型调用。"""

        def wrapped(messages: Sequence[Message]) -> str:
            last = messages[-1].get("content", "") if messages else ""
            self.llm_calls += 1
            self._emit(f"调用模型（第 {self.llm_calls} 次请求），输入预览：{_preview(last)}")
            reply = call(messages)
            self._emit(f"模型返回：{_preview(reply)}")
            return reply

        return wrapped

    def wrap_tool(self, call: ToolCall, ctx: RunContext) -> ToolCall:
        """记录一次工具调用。"""

        def wrapped(name: str, raw_input: str) -> str:
            self.tool_calls += 1
            self._emit(f"调用工具 {name}（第 {self.tool_calls} 次），输入：{_preview(raw_input)}")
            result = call(name, raw_input)
            self._emit(f"工具 {name} 返回：{_preview(result)}")
            return result

        return wrapped
