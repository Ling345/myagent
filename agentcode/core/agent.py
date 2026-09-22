"""智能体基类：统一依赖注入、中间件链、轨迹记录、上下文记忆与结果构造。"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, Sequence

from agentcode.core.context import RunContext
from agentcode.core.result import AgentResult, Step
from agentcode.llm.base import BaseLLM, Message
from agentcode.memory.short_term import ShortTermMemory
from agentcode.middleware.base import LLMCall, Middleware, ToolCall
from agentcode.tools.base import ToolRegistry

DEFAULT_MAX_STEPS = 6
#: 上下文记忆默认保留的轮数（一轮 = 一次提问 + 一次回答）
DEFAULT_MEMORY_TURNS = 5


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

    # ------------------------------------------------------------------ 抽象

    @abstractmethod
    def run(self, task: str, context: RunContext | None = None) -> AgentResult:
        """执行任务并返回结果。"""
        raise NotImplementedError

    # ------------------------------------------------------------------ 调用

    def _wrap_llm_call(self, ctx: RunContext) -> LLMCall:
        """按注册顺序把中间件自外向内套在模型调用上。

        用量累计放在最内层的 ``base_call`` 中完成，
        这样外层中间件（例如用量统计）在调用返回后就能读到已更新的上下文。
        """

        def base_call(messages: Sequence[Message]) -> str:
            reply = self.llm.think(messages, temperature=self.temperature)
            ctx.usage.add(self.llm.last_usage)
            return reply

        call: LLMCall = base_call
        for middleware in reversed(self.middlewares):
            call = middleware.wrap_llm(call, ctx)
        return call

    def _wrap_tool_call(self, ctx: RunContext) -> ToolCall:
        """按注册顺序把中间件自外向内套在工具调用上。"""

        def base_call(name: str, raw_input: str) -> str:
            return self.tools.invoke(name, raw_input)

        call: ToolCall = base_call
        for middleware in reversed(self.middlewares):
            call = middleware.wrap_tool(call, ctx)
        return call

    def _think(self, messages: Sequence[Message], ctx: RunContext) -> str:
        """调用大语言模型；用量已在最内层累计。"""
        return self._wrap_llm_call(ctx)(messages)

    def _call_tool(
        self,
        name: str,
        raw_input: str,
        ctx: RunContext,
        thought: str = "",
        action: str = "",
    ) -> str:
        """执行工具调用并记录一条轨迹。"""
        started = time.monotonic()
        observation = self._wrap_tool_call(ctx)(name, raw_input)
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
        """把上下文整理成对外的运行结果。"""
        return AgentResult(
            agent=self.name,
            task=task,
            answer=answer,
            success=success,
            steps=list(ctx.steps),
            error=error,
            duration_ms=ctx.elapsed_ms(),
            usage=ctx.usage,
            extra=dict(extra or {}),
        )
