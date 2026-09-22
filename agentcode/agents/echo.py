"""扩展示例：新增一个智能体只需要这样一个文件。

它不调用模型，直接回显任务，用来演示注册机制的最小成本。
"""

from __future__ import annotations

from agentcode.core.agent import BaseAgent
from agentcode.core.context import RunContext
from agentcode.core.registry import register_agent
from agentcode.core.result import AgentResult, Step


@register_agent("echo", "演示智能体：不调用模型，直接回显任务内容。")
class EchoAgent(BaseAgent):
    """回显智能体。"""

    name = "echo"
    description = "演示智能体：不调用模型，直接回显任务内容。"

    def run(self, task: str, context: RunContext | None = None) -> AgentResult:
        """把任务原样返回，用于验证注册表与 CLI 链路。"""
        ctx = context or self._new_context(task)
        answer = f"已收到任务：{task}"
        ctx.add_step(
            Step(index=ctx.next_index(), thought="无需调用模型", action="回显任务", answer=answer)
        )
        self._remember(task, answer)
        return self._build_result(task, answer, ctx)
