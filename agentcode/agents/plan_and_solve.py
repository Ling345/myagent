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
from agentcode.llm.base import LLMReply, Message
from agentcode.llm.attachments import user_content
from agentcode.llm.structured import PLAN_SCHEMA, ask_json

#: 「调用模型并累计用量」的函数签名，由 BaseAgent._think 提供
ThinkFn = Callable[[Sequence[Message], RunContext], str]
#: 「调用模型并拿回完整回复」的函数签名，由 BaseAgent._think_reply 提供
CompleteFn = Callable[[Sequence[Message]], LLMReply]

#: 第一次没拿到合格 JSON 时，追一句纠正说明再问一次
_PLAN_RETRY_HINT = """

# 请注意
上一次的回答不合要求。请只输出一个 JSON 对象，形如：
{{"steps": ["步骤一", "步骤二"]}}
不要解释、不要用代码围栏包住整段回答。"""


class Planner:
    """把复杂问题拆解为有序步骤。"""

    def __init__(self, think: ThinkFn, complete: CompleteFn | None = None) -> None:
        self._think = think
        self._complete = complete

    def plan(self, question: str, ctx: RunContext, history: str) -> list[str]:
        """生成行动计划；解析失败时返回空列表。

        先按"回一段 JSON"要（结构化更稳）：合格就用；不合格但能按老格式解析出来
        （离线脚本模型、老提示词）也直接用，不再多花一次调用；两边都不行才
        带着问题再问一次。
        """
        prompt = PLANNER_PROMPT_TEMPLATE.format(question=question, history=history)
        messages = [{"role": "user", "content": user_content(prompt, ctx.images)}]
        if self._complete is not None:
            steps = self._try_structured(messages)
            if steps:
                return steps
            retry_messages = [
                {
                    "role": "user",
                    "content": user_content(prompt + _PLAN_RETRY_HINT, ctx.images),
                }
            ]
            if self._complete is not None:
                return self._try_structured(retry_messages)
        raw = self._think(messages, ctx)
        return parse_plan(raw)

    def _try_structured(self, messages: Sequence[Message]) -> list[str]:
        """问一次结构化计划：JSON 优先，老格式兜底。"""
        assert self._complete is not None
        answer = ask_json(self._complete, messages, PLAN_SCHEMA, retries=0)
        if answer.payload is not None:
            steps = [
                str(item).strip() for item in answer.payload.get("steps", []) if str(item).strip()
            ]
            if steps:
                return steps
        return parse_plan(answer.text)


class Executor:
    """严格按计划逐步执行，并累积历史结果。"""

    def __init__(self, think: ThinkFn) -> None:
        self._think = think

    def execute(self, question: str, plan: list[str], ctx: RunContext, history: str) -> str:
        """依次执行每个步骤，返回最后一步的结果。"""
        progress = ""
        final_answer = ""
        for index, step in enumerate(plan, start=1):
            prompt = EXECUTOR_PROMPT_TEMPLATE.format(
                question=question,
                plan=plan,
                history=history,
                progress=progress or "（暂无）",
                current_step=step,
            )
            result = self._think(
                [{"role": "user", "content": user_content(prompt, ctx.images)}], ctx
            )
            progress += f"步骤 {index}: {step}\n结果: {result}\n\n"
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
        history = self._history_text()
        planner = Planner(self._think, lambda messages: self._think_reply(messages, ctx))
        try:
            plan = planner.plan(task, ctx, history)
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
            answer = Executor(self._think).execute(task, trimmed, ctx, history)
        except LLMError as exc:
            return self._build_result(task, "", ctx, success=False, error=f"调用模型失败：{exc}")

        extra = {"plan": plan, "executed_steps": len(trimmed)}
        if len(plan) > len(trimmed):
            extra["plan_truncated"] = True
        self._remember(task, answer)
        return self._build_result(task, answer, ctx, extra=extra)
