"""超时中间件：用线程池给单次调用加上限时，保证 Windows 下同样可用。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Sequence

from agentcode.core.context import RunContext
from agentcode.core.errors import LLMError
from agentcode.llm.base import Message
from agentcode.middleware.base import LLMCall, Middleware, ToolCall


class TimeoutMiddleware(Middleware):
    """超过时限时 LLM 调用抛 :class:`LLMError`，工具调用返回中文错误字符串。"""

    name = "timeout"

    def __init__(self, timeout: float = 30.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout 必须大于 0。")
        self.timeout = timeout

    def _run(self, func, args: tuple) -> object:
        """在独立线程中执行并限时等待；超时后立即返回，不再等待后台线程。"""
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(func, *args)
            return future.result(timeout=self.timeout)
        finally:
            executor.shutdown(wait=False)

    def wrap_llm(self, call: LLMCall, ctx: RunContext) -> LLMCall:
        """包装模型调用。"""

        def wrapped(messages: Sequence[Message]) -> str:
            try:
                return self._run(call, (messages,))  # type: ignore[return-value]
            except FutureTimeoutError as exc:
                raise LLMError(f"调用大语言模型超时（超过 {self.timeout} 秒）。") from exc

        return wrapped

    def wrap_tool(self, call: ToolCall, ctx: RunContext) -> ToolCall:
        """包装工具调用。"""

        def wrapped(name: str, raw_input: str) -> str:
            try:
                return self._run(call, (name, raw_input))  # type: ignore[return-value]
            except FutureTimeoutError:
                return f"错误：工具 '{name}' 调用超时（超过 {self.timeout} 秒）。"

        return wrapped
