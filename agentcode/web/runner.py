"""把智能体运行包装成事件流，供网页逐步展示。

一次请求的事件类型：

- ``status``：正在调用模型 / 工具，用于即时反馈；
- ``step``：一条已完成的推理步骤（思考 / 行动 / 观察）；
- ``answer``：最终答案与统计信息，事件流到此结束；
- ``error``：无法完成，附带中文原因。
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Iterator, Sequence

from agentcode import agents  # noqa: F401  导入即注册内置智能体
from agentcode.config import Settings
from agentcode.core.agent import BaseAgent
from agentcode.core.context import RunContext
from agentcode.core.errors import AgentCodeError
from agentcode.core.registry import default_registry
from agentcode.llm.mock import demo_responses_llm
from agentcode.llm.openai_compatible import OpenAICompatibleLLM
from agentcode.middleware import RetryMiddleware, TimeoutMiddleware
from agentcode.middleware.base import LLMCall, Middleware, ToolCall
from agentcode.tools import ToolRegistry, register_builtin_tools, register_demo_tools
from agentcode.web.sessions import AgentSessionStore

Event = dict[str, Any]
Emitter = Callable[[Event], None]


class EventMiddleware(Middleware):
    """把"正在调用模型 / 工具"推成状态事件，让页面有实时反馈。"""

    name = "event"

    def __init__(self, emit: Emitter) -> None:
        self._emit = emit

    def wrap_llm(self, call: LLMCall, ctx: RunContext) -> LLMCall:
        def wrapped(messages: Sequence[Any]) -> str:
            self._emit({"type": "status", "data": {"message": "正在调用模型…"}})
            return call(messages)

        return wrapped

    def wrap_tool(self, call: ToolCall, ctx: RunContext) -> ToolCall:
        def wrapped(name: str, raw_input: str) -> str:
            self._emit({"type": "status", "data": {"message": f"正在调用工具 {name}…"}})
            return call(name, raw_input)

        return wrapped


def build_tools(mock: bool) -> ToolRegistry:
    """按后端类型准备工具集。"""
    registry = ToolRegistry()
    register_builtin_tools(registry, include_search=not mock)
    if mock:
        register_demo_tools(registry)
    return registry


def build_middlewares(emit: Emitter, settings: Settings) -> list[Middleware]:
    """组装网页侧的中间件链（顺序即包装顺序，最外层在前）。"""
    return [
        EventMiddleware(emit),
        RetryMiddleware(max_retries=2, base_delay=0.2),
        TimeoutMiddleware(timeout=settings.timeout),
    ]


def create_backend(
    agent_name: str,
    llm_mode: str,
    settings: Settings,
    max_steps: int | None = None,
) -> BaseAgent:
    """装配一个智能体实例（模型与工具；中间件随后注入）。"""
    if llm_mode == "mock":
        llm = demo_responses_llm(agent_name)
        tools = build_tools(mock=True)
    else:
        settings.validate()
        llm = OpenAICompatibleLLM.from_settings(settings)
        tools = build_tools(mock=False)

    return default_registry.create(
        agent_name,
        llm=llm,
        tools=tools,
        max_steps=max_steps or settings.max_steps,
    )


def run_stream(
    agent_name: str,
    task: str,
    *,
    llm_mode: str = "mock",
    max_steps: int | None = None,
    settings: Settings | None = None,
    session_store: AgentSessionStore | None = None,
    session_id: str | None = None,
) -> Iterator[Event]:
    """在后台线程里执行智能体，把事件按发生顺序逐条产出。

    带上 ``session_store`` 与 ``session_id`` 时复用同一个智能体实例，
    上下文记忆因此可以在多次提问之间延续。
    """
    events: queue.Queue[Event | None] = queue.Queue()

    def emit(event: Event) -> None:
        events.put(event)

    def worker() -> None:
        try:
            active_settings = settings or Settings.from_env()
            ctx = RunContext(
                task=task,
                on_step=lambda step: emit({"type": "step", "data": step.to_dict()}),
            )

            if session_store is not None and session_id:
                agent = session_store.get(
                    session_id,
                    lambda: create_backend(agent_name, llm_mode, active_settings, max_steps),
                )
            else:
                agent = create_backend(agent_name, llm_mode, active_settings, max_steps)

            # 中间件绑定本次请求的事件出口，所以每次请求都换一套
            agent.middlewares = build_middlewares(emit, active_settings)

            result = agent.run(task, context=ctx)
            payload = result.to_dict()
            payload["memory_turns"] = len(agent.memory)
            emit({"type": "answer", "data": payload})
        except AgentCodeError as exc:
            emit({"type": "error", "data": {"message": str(exc)}})
        except Exception as exc:  # noqa: BLE001 - 兜底，避免线程静默死掉
            emit({"type": "error", "data": {"message": f"未预期的错误：{exc}"}})
        finally:
            events.put(None)

    thread = threading.Thread(target=worker, name="agentcode-run", daemon=True)
    thread.start()

    while True:
        event = events.get()
        if event is None:
            break
        yield event
