"""重试中间件：指数退避重试失败的调用。"""

from __future__ import annotations

import time
from typing import Sequence, TypeVar

from agentcode.core.context import RunContext
from agentcode.core.errors import LLMError
from agentcode.llm.base import Message
from agentcode.middleware.base import LLMCall, Middleware, ToolCall

T = TypeVar("T")


class RetryMiddleware(Middleware):
    """让偶发失败不再直接中断整个流程。"""

    name = "retry"

    def __init__(
        self,
        max_retries: int = 2,
        base_delay: float = 0.5,
        exceptions: tuple[type[Exception], ...] = (LLMError,),
        sleep: object = time.sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries 不能为负数。")
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.exceptions = exceptions
        self._sleep = sleep
        self.attempts = 0

    def _run(self, func, args: tuple) -> object:
        """执行调用并按指数退避重试。"""
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self.attempts += 1
            try:
                return func(*args)
            except self.exceptions as exc:  # type: ignore[misc]
                last_error = exc
                if attempt == self.max_retries:
                    break
                delay = self.base_delay * (2**attempt)
                if delay > 0:
                    self._sleep(delay)  # type: ignore[operator]
        assert last_error is not None
        raise last_error

    def wrap_llm(self, call: LLMCall, ctx: RunContext) -> LLMCall:
        """包装模型调用。"""

        def wrapped(messages: Sequence[Message]) -> str:
            return self._run(call, (messages,))  # type: ignore[return-value]

        return wrapped

    def wrap_tool(self, call: ToolCall, ctx: RunContext) -> ToolCall:
        """包装工具调用。"""

        def wrapped(name: str, raw_input: str) -> str:
            return self._run(call, (name, raw_input))  # type: ignore[return-value]

        return wrapped
