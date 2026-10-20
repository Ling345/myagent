"""ReAct 智能体：思考 → 行动 → 观察的循环。

工具有两条调用路径，行为一致，只是"怎么把工具交给模型"不同：

- **原生工具调用**（后端支持时，默认开启）：把工具 schema 交给模型，模型回
  ``tool_calls``，参数是 JSON；工具结果以 ``role="tool"`` 的消息喂回去；
- **文本协议**（后端不支持时的兜底）：提示词里写清 ``工具名[输入]`` 格式，
  模型输出后我们自己解析，观察结果拼进下一次提示词。

服务商不认 ``tools`` 参数时会自动退回文本协议（记一条 ``fallback`` 指标），
任务不会因为"这家不支持工具调用"而做死。
"""

from __future__ import annotations

from agentcode.agents.prompts import (
    REACT_NATIVE_SYSTEM_PROMPT,
    REACT_NATIVE_TASK_TEMPLATE,
    REACT_SYSTEM_PROMPT,
    REACT_TASK_TEMPLATE,
)
from agentcode.core.agent import BaseAgent
from agentcode.core.context import RunContext
from agentcode.core.errors import LLMError
from agentcode.core.parsing import parse_action, parse_finish, parse_react_output
from agentcode.core.registry import register_agent
from agentcode.core.result import AgentResult, Step
from agentcode.llm.base import LLMReply, Message
from agentcode.llm.attachments import user_content
from agentcode.metrics import TOOL_CALLING
from agentcode.tools.base import FINISH_TOOL_NAME


@register_agent("react", "ReAct 范式：边推理边调用工具，适合需要实时信息的任务。")
class ReActAgent(BaseAgent):
    """ReAct 智能体。"""

    name = "react"
    description = "ReAct 范式：边推理边调用工具，适合需要实时信息的任务。"
    #: 稳定前缀：工具清单 + 格式 + 规则；子类换成带自己纪律的版本
    SYSTEM_PROMPT = REACT_SYSTEM_PROMPT
    NATIVE_SYSTEM_PROMPT = REACT_NATIVE_SYSTEM_PROMPT

    def _build_messages(
        self,
        task: str,
        steps: list[str],
        history: str,
        context: RunContext | None = None,
    ) -> list[Message]:
        """两段式消息：稳定的 system + 变化的 user（prefix cache 才命中得了）。"""
        return [
            {
                "role": "system",
                "content": self.SYSTEM_PROMPT.format(tools=self.tools.describe()),
            },
            {
                "role": "user",
                "content": user_content(
                    REACT_TASK_TEMPLATE.format(
                        history=history,
                        steps="\n".join(steps) if steps else "（暂无）",
                        question=task,
                    ),
                    context.images if context is not None else None,
                ),
            },
        ]

    def run(self, task: str, context: RunContext | None = None) -> AgentResult:
        """执行 ReAct 循环直到给出最终答案或达到步数上限。"""
        ctx = context or self._new_context(task)
        if self.native_tools_enabled():
            result = self._run_native(task, ctx)
            if result is not None:
                return result
            # 服务商不认工具调用：退回文本协议，从头重跑一次
            self.llm.supports_tools = False
            self.last_tool_calling_mode = "fallback"
            TOOL_CALLING.inc(mode="fallback")
            return self._run_text(task, ctx)
        self.last_tool_calling_mode = "text"
        TOOL_CALLING.inc(mode="text")
        return self._run_text(task, ctx)

    def _mark_native(self) -> None:
        """记一次"这次走的是原生工具调用"（一次运行只记一次）。"""
        if self.last_tool_calling_mode != "native":
            self.last_tool_calling_mode = "native"
            TOOL_CALLING.inc(mode="native")

    # -------------------------------------------------------------- 原生工具调用

    def _build_native_messages(
        self, task: str, history: str, context: RunContext | None = None
    ) -> list[Message]:
        """原生模式：同样"稳定 system + 变化 user"。"""
        return [
            {
                "role": "system",
                "content": self.NATIVE_SYSTEM_PROMPT.format(tools=self.tools.describe()),
            },
            {
                "role": "user",
                "content": user_content(
                    REACT_NATIVE_TASK_TEMPLATE.format(history=history, question=task),
                    context.images if context is not None else None,
                ),
            },
        ]

    def _run_native(self, task: str, ctx: RunContext) -> AgentResult | None:
        """走原生工具调用；返回 ``None`` 表示"这家不支持，请回退"。"""
        history = self._history_text()
        messages: list[Message] = self._build_native_messages(task, history, ctx)
        schemas = self.tools.json_schemas()

        for _ in range(self.max_steps):
            try:
                reply = self._think_reply(messages, ctx, tools=schemas)
            except LLMError as exc:
                if not ctx.steps:  # 还没跑过任何工具，回退是安全的
                    print(f"提示：这个服务不支持工具调用（{exc}），已自动改用文本协议。")
                    return None
                return self._build_result(task, "", ctx, success=False, error=f"调用模型失败：{exc}")
            self._mark_native()

            if not reply.has_tool_calls:
                answer = (reply.text or "").strip()
                if not answer:
                    ctx.add_step(
                        Step(
                            index=ctx.next_index(),
                            action="（空回复）",
                            error="模型既没有调用工具，也没有给出答案",
                        )
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": "请调用工具继续，或用 finish 给出最终答案。",
                        }
                    )
                    continue
                ctx.add_step(Step(index=ctx.next_index(), action="直接回答", answer=answer))
                self._remember(task, answer)
                return self._build_result(task, answer, ctx)

            messages.append(self._assistant_tool_message(reply))
            for call in reply.tool_calls:
                if call.name == FINISH_TOOL_NAME:
                    answer = str(call.arguments_dict().get("answer") or reply.text or "").strip()
                    ctx.add_step(
                        Step(
                            index=ctx.next_index(),
                            action=f"{FINISH_TOOL_NAME}[{call.arguments}]",
                            answer=answer,
                        )
                    )
                    if answer:
                        self._remember(task, answer)
                        return self._build_result(task, answer, ctx)
                    continue
                observation = self._call_tool(
                    call.name,
                    call.arguments,
                    ctx,
                    action=f"{call.name}[{call.arguments}]",
                    json_arguments=True,
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": observation,
                    }
                )

        return self._build_result(
            task,
            "",
            ctx,
            success=False,
            error=f"已达到最大步数（{self.max_steps}）仍未得出最终答案。",
        )

    @staticmethod
    def _assistant_tool_message(reply: LLMReply) -> Message:
        """把模型的工具调用原样记回对话（OpenAI 协议要求这一步）。"""
        return {
            "role": "assistant",
            "content": reply.text or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in reply.tool_calls
            ],
        }

    # ---------------------------------------------------------------- 文本协议

    def _run_text(self, task: str, ctx: RunContext) -> AgentResult:
        """原来的文本协议循环（`Thought/Action` 两行格式）。"""
        history = self._history_text()
        steps: list[str] = []

        for _ in range(self.max_steps):
            messages = self._build_messages(task, steps, history, ctx)
            try:
                raw = self._think(messages, ctx)
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
