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
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from agentcode import agents  # noqa: F401  导入即注册内置智能体
from agentcode.config import Settings
from agentcode.core.agent import BaseAgent
from agentcode.core.context import RunContext
from agentcode.core.errors import AgentCodeError
from agentcode.core.registry import default_registry
from agentcode.llm.mock import demo_responses_llm
from agentcode.llm.openai_compatible import OpenAICompatibleLLM
from agentcode.memory import ShortTermMemory
from agentcode.middleware import BudgetMiddleware, RetryMiddleware, TimeoutMiddleware
from agentcode.middleware.base import LLMCall, Middleware, ToolCall
from agentcode.tools import (
    ToolRegistry,
    register_builtin_tools,
    register_code_tools,
    register_demo_tools,
)
from agentcode.web.sessions import SessionStore

Event = dict[str, Any]
Emitter = Callable[[Event], None]
#: 需要代码工具的智能体（读源码、写测试、跑测试）
CODE_TOOL_AGENTS = frozenset({"coding", "test_gen"})


def snapshot_files(root: str | Path) -> dict[str, tuple[float, int]]:
    """记录目录下每个文件的（修改时间, 大小），用于比出本轮新增/改动。"""
    base = Path(root)
    if not base.is_dir():
        return {}
    snapshot: dict[str, tuple[float, int]] = {}
    for item in base.rglob("*"):
        if item.is_file():
            try:
                stat = item.stat()
            except OSError:
                continue
            snapshot[item.relative_to(base).as_posix()] = (stat.st_mtime, stat.st_size)
    return snapshot


def changed_files(
    before: dict[str, tuple[float, int]],
    after: dict[str, tuple[float, int]],
) -> list[dict[str, Any]]:
    """比出新增或内容变化的文件，按路径排序。"""
    changed = [
        {"path": path, "bytes": size}
        for path, (mtime, size) in sorted(after.items())
        if before.get(path) != (mtime, size)
    ]
    return changed


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


def build_tools(
    mock: bool,
    settings: Settings | None = None,
    agent_name: str | None = None,
) -> ToolRegistry:
    """按后端类型与智能体准备工具集。

    代码工具只给 coding 智能体：别的智能体用不上，挂在工具清单里既占提示词、
    又会诱导模型去做多余的代码执行（多一轮就多几秒）。
    """
    active = settings or Settings.from_env()
    registry = ToolRegistry()
    register_builtin_tools(registry, include_search=not mock, serpapi_key=active.serpapi_key)
    if mock:
        register_demo_tools(registry)
    elif not active.allow_code_tools:
        # 面向公网默认不允许执行代码：容器化隔离之前，这是最稳的默认值
        return registry

    if agent_name is not None and agent_name not in CODE_TOOL_AGENTS:
        return registry
    register_code_tools(
        registry,
        root=active.code_root,
        timeout=active.code_timeout,
        output_limit=active.code_output_limit,
        execution_backend=active.execution_backend,
        docker_image=active.docker_image,
        docker_binary=active.docker_binary,
        docker_memory=active.docker_memory,
        docker_cpus=active.docker_cpus,
        docker_pids_limit=active.docker_pids_limit,
        docker_user=active.docker_user,
    )
    return registry


def build_middlewares(emit: Emitter, settings: Settings) -> list[Middleware]:
    """组装网页侧的中间件链（顺序即包装顺序，最外层在前）。"""
    return [
        # 预算放在最外层：超预算的错误不会被重试逻辑反复触发
        BudgetMiddleware(max_tokens=settings.run_token_budget),
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
    """装配一个智能体实例（模型、工具与上下文记忆；中间件随后注入）。"""
    if llm_mode == "mock":
        llm = demo_responses_llm(agent_name)
        tools = build_tools(mock=True)
    else:
        settings.validate()
        llm = OpenAICompatibleLLM.from_settings(settings)
        tools = build_tools(mock=False, settings=settings, agent_name=agent_name)

    return default_registry.create(
        agent_name,
        llm=llm,
        tools=tools,
        memory=ShortTermMemory(max_turns=settings.memory_turns),
        max_steps=max_steps or settings.max_steps_for(agent_name),
    )


def run_stream(
    agent_name: str,
    task: str,
    *,
    llm_mode: str = "mock",
    max_steps: int | None = None,
    settings: Settings | None = None,
    session_store: SessionStore | None = None,
    session_id: str | None = None,
) -> Iterator[Event]:
    """在后台线程里执行智能体，把事件按发生顺序逐条产出。

    带上 ``session_store`` 与 ``session_id`` 时复用同一个智能体实例（记忆延续），
    并把这一问一答记进会话记录，方便之后切回来看。
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
                    max_turns=active_settings.memory_turns,
                )
                session_store.append_message(session_id, "user", task)
            else:
                agent = create_backend(agent_name, llm_mode, active_settings, max_steps)

            # 中间件绑定本次请求的事件出口，所以每次请求都换一套
            agent.middlewares = build_middlewares(emit, active_settings)

            # 会写文件的智能体（coding / test_gen）：跑之前先拍快照，跑完比出本轮产物。
            # test_gen 也要算进去——它是本项目的作业方向，生成的测试文件必须能在页面上点开。
            code_root = Path(active_settings.code_root).resolve()
            before = snapshot_files(code_root) if agent_name in CODE_TOOL_AGENTS else None

            result = agent.run(task, context=ctx)
            artifacts = changed_files(before, snapshot_files(code_root)) if before is not None else []
            payload = result.to_dict()
            payload["memory_turns"] = len(agent.memory)
            payload["memory_limit"] = agent.memory.max_turns
            payload["turn_index"] = agent.memory.total_turns
            payload["session_id"] = session_id
            payload["artifacts"] = artifacts

            if session_store is not None and session_id:
                session_store.append_message(
                    session_id,
                    "assistant",
                    result.answer or (result.error or "（未得出结论）"),
                    success=result.success,
                    artifacts=artifacts,
                )
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
