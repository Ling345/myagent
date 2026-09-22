"""ReAct 智能体：思考 → 行动 → 观察的循环。"""

from __future__ import annotations

from agentcode.agents.prompts import REACT_PROMPT_TEMPLATE
from agentcode.core.agent import BaseAgent
from agentcode.core.context import RunContext
from agentcode.core.errors import LLMError
from agentcode.core.parsing import parse_action, parse_finish, parse_react_output
from agentcode.core.registry import register_agent
from agentcode.core.result import AgentResult, Step


@register_agent("react", "ReAct 范式：边推理边调用工具，适合需要实时信息的任务。")
class ReActAgent(BaseAgent):
    """ReAct 智能体。"""

    name = "react"
    description = "ReAct 范式：边推理边调用工具，适合需要实时信息的任务。"

    def _build_prompt(self, task: str, steps: list[str], history: str) -> str:
        """渲染当轮提示词。"""
        return REACT_PROMPT_TEMPLATE.format(
            tools=self.tools.describe(),
            question=task,
            history=history,
            steps="\n".join(steps) if steps else "（暂无）",
        )

    def run(self, task: str, context: RunContext | None = None) -> AgentResult:
        """执行 ReAct 循环直到给出最终答案或达到步数上限。"""
        ctx = context or self._new_context(task)
        history = self._history_text()
        steps: list[str] = []

        for _ in range(self.max_steps):
            prompt = self._build_prompt(task, steps, history)
            try:
                raw = self._think([{"role": "user", "content": prompt}], ctx)
            except LLMError as exc:
                return self._build_result(task, "", ctx, success=False, error=f"调用模型失败：{exc}")

            thought, action = parse_react_output(raw)
            if not action:
                ctx.add_step(
                    Step(
                        index=ctx.next_index(),
                        thought=thought or "",
                        action=raw.strip(),
                        error="模型输出缺少 Action 字段",
                    )
                )
                steps.append(
                    f"模型输出：{raw.strip()}\n"
                    "Observation: 输出缺少 Action 字段，请严格使用 Thought 与 Action 两行格式。"
                )
                continue

            answer = parse_finish(action)
            if answer is not None:
                ctx.add_step(
                    Step(index=ctx.next_index(), thought=thought or "", action=action, answer=answer)
                )
                self._remember(task, answer)
                return self._build_result(task, answer, ctx)

            tool_name, tool_input = parse_action(action)
            if not tool_name:
                ctx.add_step(
                    Step(
                        index=ctx.next_index(),
                        thought=thought or "",
                        action=action,
                        error="无法解析工具名称",
                    )
                )
                steps.append(
                    f"Action: {action}\n"
                    "Observation: 无法解析该行动，请使用 工具名[输入] 或 Finish[答案] 的格式。"
                )
                continue

            observation = self._call_tool(
                tool_name,
                tool_input or "",
                ctx,
                thought=thought or "",
                action=action,
            )
            steps.append(f"Action: {action}\nObservation: {observation}")

        return self._build_result(
            task,
            "",
            ctx,
            success=False,
            error=f"已达到最大步数（{self.max_steps}）仍未得出最终答案。",
        )
