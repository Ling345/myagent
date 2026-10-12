"""智能体基类：统一依赖注入、中间件链、轨迹记录、上下文记忆与结果构造。"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Sequence

from agentcode.core.context import RunContext
from agentcode.core.errors import AgentCodeError
from agentcode.core.result import AgentResult, Step
from agentcode.llm.base import BaseLLM, LLMReply, Message
from agentcode.memory.short_term import ShortTermMemory
from agentcode.middleware.base import LLMCall, Middleware, ToolCall
from agentcode.tools.base import ToolRegistry

DEFAULT_MAX_STEPS = 6
#: 上下文记忆默认保留的轮数（一轮 = 一次提问 + 一次回答）
DEFAULT_MEMORY_TURNS = 5

#: 工具调用模式：auto = 后端支持就用原生、不支持就退回文本协议
TOOL_CALLING_AUTO = "auto"
TOOL_CALLING_NATIVE = "native"
TOOL_CALLING_TEXT = "text"
TOOL_CALLING_MODES = (TOOL_CALLING_AUTO, TOOL_CALLING_NATIVE, TOOL_CALLING_TEXT)


class BaseAgent(ABC):
    """所有智能体的共同骨架。

    子类只需要实现 :meth:`run`，并使用 :meth:`_think` / :meth:`_call_tool`
    发起调用，即可自动获得中间件、用量统计与轨迹记录能力；
    在给出最终答案时调用 :meth:`_remember`，上下文记忆就会延续到下一轮。

    子类可以用 ``default_max_steps`` 声明自己需要的默认步数
    （例如写代码的智能体需要比闲聊更多的步数）。
    """

    #: 注册表中使用的名称
    name: str = "base"
    #: 一句话说明，用于 CLI 展示与提示词
    description: str = "基础智能体"
    #: 未显式指定步数时使用的默认值
    default_max_steps: int = DEFAULT_MAX_STEPS

    def __init__(
        self,
        llm: BaseLLM,
        tools: ToolRegistry | None = None,
        middlewares: Sequence[Middleware] | None = None,
        memory: ShortTermMemory | None = None,
        max_steps: int | None = None,
        temperature: float | None = None,
        name: str | None = None,
        description: str | None = None,
        tool_calling: str | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools if tools is not None else ToolRegistry()
        self.middlewares: list[Middleware] = list(middlewares or [])
        self.memory = (
            memory if memory is not None else ShortTermMemory(max_turns=DEFAULT_MEMORY_TURNS)
        )
        self.max_steps = max_steps or type(self).default_max_steps or DEFAULT_MAX_STEPS
        self.temperature = temperature
        if name:
            self.name = name
        if description:
            self.description = description
        mode = str(tool_calling or TOOL_CALLING_AUTO).strip().lower()
        if mode not in TOOL_CALLING_MODES:
            raise AgentCodeError(
                f"工具调用模式只能是 {'/'.join(TOOL_CALLING_MODES)}，收到的是：{mode}"
            )
        #: auto/native/text，见 :data:`TOOL_CALLING_MODES`
        self.tool_calling = mode
        #: 这次运行实际走的哪条路（native/text/fallback），跑完才有值
        self.last_tool_calling_mode: str = ""

    # ------------------------------------------------------------------ 抽象

    @abstractmethod
    def run(self, task: str, context: RunContext | None = None) -> AgentResult:
        """执行任务并返回结果。"""
        raise NotImplementedError

    # ------------------------------------------------------------------ 调用

    def _wrap_llm_call(
        self,
        ctx: RunContext,
        *,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> LLMCall:
        """按注册顺序把中间件自外向内套在模型调用上。

        用量累计放在最内层的 ``base_call`` 中完成，
        这样外层中间件（例如用量统计）在调用返回后就能读到已更新的上下文。
        """

        def base_call(messages: Sequence[Message]) -> LLMReply:
            reply = self.llm.complete(messages, tools=tools, temperature=self.temperature)
            ctx.usage.add(self.llm.last_usage)
            return reply

        call: LLMCall = base_call
        for middleware in reversed(self.middlewares):
            call = middleware.wrap_llm(call, ctx)
        return call

    def _wrap_tool_call(self, ctx: RunContext, *, json_arguments: bool = False) -> ToolCall:
        """按注册顺序把中间件自外向内套在工具调用上。

        ``json_arguments`` 表示参数是原生工具调用给的 JSON（而不是文本协议里
        那串 ``k=v`` 文本），两者都要经过同一套中间件。
        """

        def base_call(name: str, raw_input: str) -> str:
            if json_arguments:
                return self.tools.invoke_json(name, raw_input)
            return self.tools.invoke(name, raw_input)

        call: ToolCall = base_call
        for middleware in reversed(self.middlewares):
            call = middleware.wrap_tool(call, ctx)
        return call

    def _think(self, messages: Sequence[Message], ctx: RunContext) -> str:
        """调用大语言模型；用量已在最内层累计。"""
        return self._wrap_llm_call(ctx)(messages).text

    def _think_reply(
        self,
        messages: Sequence[Message],
        ctx: RunContext,
        *,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> LLMReply:
        """调用模型并拿回完整回复（可能带着工具调用）。"""
        return self._wrap_llm_call(ctx, tools=tools)(messages)

    # -------------------------------------------------------------- 工具调用模式

    def native_tools_enabled(self) -> bool:
        """这次要不要走原生工具调用。"""
        return (
            self.tool_calling != TOOL_CALLING_TEXT
            and bool(getattr(self.llm, "supports_tools", False))
            and len(self.tools) > 0
        )

    def _call_tool(
        self,
        name: str,
        raw_input: str,
        ctx: RunContext,
        thought: str = "",
        action: str = "",
        json_arguments: bool = False,
    ) -> str:
        """执行工具调用并记录一条轨迹。"""
        started = time.monotonic()
        observation = self._wrap_tool_call(ctx, json_arguments=json_arguments)(name, raw_input)
        duration_ms = (time.monotonic() - started) * 1000
        ctx.add_step(
            Step(
                index=ctx.next_index(),
                thought=thought,
                action=action,
                tool=name,
                tool_input=raw_input,
                observation=observation,
                duration_ms=duration_ms,
                error=observation if observation.startswith("错误：") else None,
            )
        )
        return observation

    # ------------------------------------------------------------------ 记忆

    def _history_text(self) -> str:
        """把更早的对话渲染成提示词片段。"""
        dialogue = self.memory.as_dialogue()
        return dialogue or "（这是本次会话的第一轮，没有更早的对话）"

    def _remember(self, task: str, answer: str) -> None:
        """一轮成功结束后，把"用户提问 + 最终答案"整体写入记忆。"""
        if not answer:
            return
        self.memory.add_turn(
            [
                {"role": "user", "content": task},
                {"role": "assistant", "content": answer},
            ]
        )

    # ------------------------------------------------------------------ 结果

    def _new_context(self, task: str) -> RunContext:
        """创建本次运行的上下文。"""
        return RunContext(task=task)

    def _build_result(
        self,
        task: str,
        answer: str,
        ctx: RunContext,
        success: bool = True,
        error: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AgentResult:
        """把上下文整理成对外的运行结果。

        用了多模型档位时，顺手记下"这次是谁答的"——出了问题（答得不好、
        费用不对）第一个要问的就是它。
        """
        payload = dict(extra or {})
        profile = str(getattr(self.llm, "used_profile", "") or "")
        if profile:
            payload.setdefault("model_profile", profile)
        return AgentResult(
            agent=self.name,
            task=task,
            answer=answer,
            success=success,
            steps=list(ctx.steps),
            error=error,
            duration_ms=ctx.elapsed_ms(),
            usage=ctx.usage,
            extra=payload,
        )
