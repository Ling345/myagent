"""Plan-and-Solve 智能体：先制定计划，再逐步执行。"""

from __future__ import annotations

from typing import Callable, Sequence

from agentcode.agents.prompts import EXECUTOR_PROMPT_TEMPLATE, PLANNER_PROMPT_TEMPLATE
from agentcode.core.agent import BaseAgent
from agentcode.core.context import RunContext
from agentcode.core.errors import LLMError
from agentcode.core.parsing import parse_plan
from agentcode.core.registry import register_agent
from agentcode.core.result import AgentResult, Step
from agentcode.llm.base import Message

#: 「调用模型并累计用量」的函数签名，由 BaseAgent._think 提供
ThinkFn = Callable[[Sequence[Message], RunContext], str]


class Planner:
    """把复杂问题拆解为有序步骤。"""

    def __init__(self, think: ThinkFn) -> None:
        self._think = think

    def plan(self, question: str, ctx: RunContext) -> list[str]:
        """生成行动计划；解析失败时返回空列表。"""
        prompt = PLANNER_PROMPT_TEMPLATE.format(question=question)
        raw = self._think([{"role": "user", "content": prompt}], ctx)
        return parse_plan(raw)


class Executor:
    """严格按计划逐步执行，并累积历史结果。"""

    def __init__(self, think: ThinkFn) -> None:
        self._think = think

    def execute(self, question: str, plan: list[str], ctx: RunContext) -> str:
        """依次执行每个步骤，返回最后一步的结果。"""
        history = ""
        final_answer = ""
        for index, step in enumerate(plan, start=1):
            prompt = EXECUTOR_PROMPT_TEMPLATE.format(
                question=question,
                plan=plan,
                history=history or "（暂无）",
                current_step=step,
            )
            result = self._think([{"role": "user", "content": prompt}], ctx)
            history += f"步骤 {index}: {step}\n结果: {result}\n\n"
            final_answer = result
            ctx.add_step(
                Step(
                    index=ctx.next_index(),
                    thought=step,
                    action=f"执行步骤 {index}",
                    observation=result,
                    answer=result,
                )
            )
        return final_answer


@register_agent("plan_and_solve", "Plan-and-Solve 范式：先拆解计划再逐步执行，适合多步推理题。")
class PlanAndSolveAgent(BaseAgent):
    """Plan-and-Solve 智能体。"""

    name = "plan_and_solve"
    description = "Plan-and-Solve 范式：先拆解计划再逐步执行，适合多步推理题。"

    def run(self, task: str, context: RunContext | None = None) -> AgentResult:
        """先规划再执行。"""
        ctx = context or self._new_context(task)
        try:
            plan = Planner(self._think).plan(task, ctx)
        except LLMError as exc:
            return self._build_result(task, "", ctx, success=False, error=f"调用模型失败：{exc}")

        if not plan:
            return self._build_result(
                task,
                "",
                ctx,
                success=False,
                error="无法生成有效的行动计划，请检查模型输出格式。",
            )

        trimmed = plan[: self.max_steps]
        try:
            answer = Executor(self._think).execute(task, trimmed, ctx)
        except LLMError as exc:
            return self._build_result(task, "", ctx, success=False, error=f"调用模型失败：{exc}")

        extra = {"plan": plan, "executed_steps": len(trimmed)}
        if len(plan) > len(trimmed):
            extra["plan_truncated"] = True
        self.memory.add_turn([{"role": "assistant", "content": answer}])
        return self._build_result(task, answer, ctx, extra=extra)
