"""单次运行的 token 预算：超了就中断，避免一次任务吞掉一天额度。"""

from __future__ import annotations

from typing import Sequence

from agentcode.core.context import RunContext
from agentcode.core.errors import AgentCodeError
from agentcode.llm.base import Message
from agentcode.middleware.base import LLMCall, Middleware, ToolCall


class BudgetMiddleware(Middleware):
    """每次模型调用后检查累计用量，超出预算就抛错中断本次运行。

    放在中间件链最外层，这样它的错误不会被重试逻辑反复触发。
    """

    name = "budget"

    def __init__(self, max_tokens: int) -> None:
        if max_tokens <= 0:
            raise ValueError("max_tokens 必须大于 0。")
        self.max_tokens = int(max_tokens)

    def wrap_llm(self, call: LLMCall, ctx: RunContext) -> LLMCall:
        """调用返回后检查是否超预算。"""

        def wrapped(messages: Sequence[Message]) -> str:
            reply = call(messages)
            used = ctx.usage.total_tokens
            if used > self.max_tokens:
                raise AgentCodeError(
                    f"本次运行已超出单次 token 预算（{used}/{self.max_tokens}），已中断以免继续消耗。"
                    "可以把任务拆小，或用 AGENT_RUN_TOKEN_BUDGET 调高上限。"
                )
            return reply

        return wrapped

    def wrap_tool(self, call: ToolCall, ctx: RunContext) -> ToolCall:
        """工具调用不额外计费，原样透传。"""
        return call
