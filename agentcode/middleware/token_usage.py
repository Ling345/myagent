"""用量统计中间件：通过对比调用前后的 ``RunContext.usage`` 计算增量。"""

from __future__ import annotations

from typing import Sequence

from agentcode.core.context import RunContext
from agentcode.core.result import TokenUsage
from agentcode.llm.base import Message
from agentcode.middleware.base import LLMCall, Middleware, ToolCall


class TokenUsageMiddleware(Middleware):
    """累计模型调用次数与 token 消耗。"""

    name = "token_usage"

    def __init__(self) -> None:
        self.usage = TokenUsage()
        self.tool_calls = 0

    def wrap_llm(self, call: LLMCall, ctx: RunContext) -> LLMCall:
        """调用前后对比上下文用量，得到本次调用的增量。"""

        def wrapped(messages: Sequence[Message]) -> str:
            before = (
                ctx.usage.prompt_tokens,
                ctx.usage.completion_tokens,
                ctx.usage.calls,
                ctx.usage.estimated,
            )
            reply = call(messages)
            after = (
                ctx.usage.prompt_tokens,
                ctx.usage.completion_tokens,
                ctx.usage.calls,
                ctx.usage.estimated,
            )
            self.usage.prompt_tokens += after[0] - before[0]
            self.usage.completion_tokens += after[1] - before[1]
            self.usage.calls += after[2] - before[2]
            self.usage.estimated = self.usage.estimated or after[3]
            return reply

        return wrapped

    def wrap_tool(self, call: ToolCall, ctx: RunContext) -> ToolCall:
        """统计工具调用次数。"""

        def wrapped(name: str, raw_input: str) -> str:
            self.tool_calls += 1
            return call(name, raw_input)

        return wrapped
