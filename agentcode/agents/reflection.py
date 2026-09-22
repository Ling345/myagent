"""Reflection 智能体：生成 → 评审 → 优化的迭代循环。"""

from __future__ import annotations

from agentcode.agents.prompts import (
    REFLECTION_CRITIQUE_TEMPLATE,
    REFLECTION_INITIAL_TEMPLATE,
    REFLECTION_REFINE_TEMPLATE,
)
from agentcode.core.agent import BaseAgent
from agentcode.core.context import RunContext
from agentcode.core.errors import LLMError
from agentcode.core.registry import register_agent
from agentcode.core.result import AgentResult, Step

_NO_IMPROVEMENT_MARKERS = ("无需改进", "无需修改", "no need for improvement")


@register_agent("reflection", "Reflection 范式：自我评审并迭代优化，适合代码与写作任务。")
class ReflectionAgent(BaseAgent):
    """Reflection 智能体。"""

    name = "reflection"
    description = "Reflection 范式：自我评审并迭代优化，适合代码与写作任务。"

    def __init__(self, *args, max_iterations: int = 3, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        if max_iterations <= 0:
            raise ValueError("max_iterations 必须大于 0。")
        self.max_iterations = max_iterations

    @staticmethod
    def _needs_no_improvement(feedback: str) -> bool:
        """判断评审是否认为已经无须改进。"""
        lowered = feedback.lower()
        return any(marker.lower() in lowered for marker in _NO_IMPROVEMENT_MARKERS)

    def run(self, task: str, context: RunContext | None = None) -> AgentResult:
        """执行初始生成与最多 ``max_iterations`` 轮反思优化。"""
        ctx = context or self._new_context(task)
        history = self._history_text()
        try:
            current = self._think(
                [
                    {
                        "role": "user",
                        "content": REFLECTION_INITIAL_TEMPLATE.format(task=task, history=history),
                    }
                ],
                ctx,
            )
        except LLMError as exc:
            return self._build_result(task, "", ctx, success=False, error=f"调用模型失败：{exc}")

        ctx.add_step(
            Step(
                index=ctx.next_index(),
                thought="初始生成",
                action="生成初版结果",
                observation=current,
                answer=current,
            )
        )

        for round_index in range(1, self.max_iterations + 1):
            try:
                feedback = self._think(
                    [
                        {
                            "role": "user",
                            "content": REFLECTION_CRITIQUE_TEMPLATE.format(task=task, code=current),
                        }
                    ],
                    ctx,
                )
            except LLMError as exc:
                return self._build_result(
                    task, current, ctx, success=False, error=f"调用模型失败：{exc}"
                )

            ctx.add_step(
                Step(
                    index=ctx.next_index(),
                    thought=f"第 {round_index} 轮反思",
                    action="评审上一版结果",
                    observation=feedback,
                )
            )
            if self._needs_no_improvement(feedback):
                self._remember(task, feedback)
                return self._build_result(
                    task,
                    feedback,
                    ctx,
                    extra={"iterations": round_index, "final_version": current},
                )

            try:
                current = self._think(
                    [
                        {
                            "role": "user",
                            "content": REFLECTION_REFINE_TEMPLATE.format(
                                task=task, last_code=current, feedback=feedback, history=history
                            ),
                        }
                    ],
                    ctx,
                )
            except LLMError as exc:
                return self._build_result(
                    task, current, ctx, success=False, error=f"调用模型失败：{exc}"
                )

            ctx.add_step(
                Step(
                    index=ctx.next_index(),
                    thought=f"第 {round_index} 轮优化",
                    action="根据评审意见优化",
                    observation=current,
                    answer=current,
                )
            )

        self._remember(task, current)
        return self._build_result(
            task,
            current,
            ctx,
            extra={"iterations": self.max_iterations, "final_version": current},
        )
