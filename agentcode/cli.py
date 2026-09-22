"""命令行入口：run / list / config 三个子命令。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from agentcode import agents  # noqa: F401  导入即触发内置智能体注册
from agentcode.config import Settings
from agentcode.core.errors import AgentCodeError, AgentNotFoundError, ConfigError
from agentcode.core.registry import default_registry
from agentcode.core.result import AgentResult
from agentcode.llm.mock import demo_responses_llm
from agentcode.llm.openai_compatible import OpenAICompatibleLLM
from agentcode.memory.json_store import JsonStore
from agentcode.middleware import (
    LoggingMiddleware,
    RetryMiddleware,
    TimeoutMiddleware,
    TokenUsageMiddleware,
)
from agentcode.tools.base import ToolRegistry
from agentcode.tools.builtin import register_builtin_tools, register_demo_tools

_SEPARATOR = "=" * 48
_THIN_SEPARATOR = "-" * 48
_OBSERVATION_PREVIEW = 200


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="agentcode",
        description="可扩展的 Python 智能体框架（ReAct / Plan-and-Solve / Reflection）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="运行一个智能体")
    run_parser.add_argument("--agent", default=None, help="智能体名称，默认 react 或配置文件中的写法")
    run_parser.add_argument("--task", default=None, help="任务描述，未提供时进入交互式输入")
    run_parser.add_argument(
        "--llm",
        choices=["openai", "mock"],
        default="openai",
        help="模型后端；mock 为离线演示，不消耗额度",
    )
    run_parser.add_argument("--max-steps", type=int, default=None, help="最大步数")
    run_parser.add_argument("--trace", default=None, help="把运行轨迹写入指定 JSON 文件")
    run_parser.add_argument("--json", action="store_true", help="以 JSON 形式输出结果")
    run_parser.add_argument("--quiet", action="store_true", help="不打印过程日志")
    run_parser.add_argument("--env-file", default=None, help="指定 .env 文件路径")
    run_parser.add_argument("--config", default=None, help="JSON 配置文件路径")

    list_parser = subparsers.add_parser("list", help="列出已注册的智能体与内置工具")
    list_parser.add_argument("--config", default=None, help="JSON 配置文件路径")

    config_parser = subparsers.add_parser("config", help="显示解析后的配置（密钥脱敏）")
    config_parser.add_argument("--env-file", default=None, help="指定 .env 文件路径")
    config_parser.add_argument("--config", default=None, help="JSON 配置文件路径")

    return parser


def _use_utf8_stdout() -> None:
    """在真实终端下切换到 UTF-8，避免 Windows 控制台中文乱码。"""
    stream = sys.stdout
    try:
        if not stream.isatty():
            return
        stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError, OSError):
        return


def _load_settings(args: argparse.Namespace) -> Settings:
    """读取 .env 与可选 JSON 配置。"""
    settings = Settings.from_env(env_file=getattr(args, "env_file", None))
    config_path = getattr(args, "config", None)
    if config_path:
        settings = settings.apply_overrides(JsonStore.load(config_path))
    max_steps = getattr(args, "max_steps", None)
    if max_steps:
        settings = settings.apply_overrides({"max_steps": max_steps})
    return settings


def _build_tools(mock: bool) -> ToolRegistry:
    """按后端类型准备工具集。"""
    registry = ToolRegistry()
    register_builtin_tools(registry, include_search=not mock)
    if mock:
        register_demo_tools(registry)
    return registry


def _build_middlewares(settings: Settings, quiet: bool) -> list[Any]:
    """组装默认中间件链（顺序即包装顺序，最外层在前）。"""
    return [
        LoggingMiddleware(enabled=not quiet),
        RetryMiddleware(max_retries=2, base_delay=0.2),
        TimeoutMiddleware(timeout=settings.timeout),
        TokenUsageMiddleware(),
    ]


def _prompt_for_task() -> str:
    """交互式读取任务；非交互环境返回空字符串。"""
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return ""
        return input("请输入任务：").strip()
    except (EOFError, OSError):
        return ""


def _preview(text: str, limit: int = _OBSERVATION_PREVIEW) -> str:
    """压缩长文本，便于终端阅读。"""
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "…"


def render_result(result: AgentResult) -> str:
    """把运行结果渲染成人类可读文本。"""
    lines = [_SEPARATOR, f"智能体：{result.agent}", f"任务：{result.task}", _THIN_SEPARATOR]
    for step in result.steps:
        lines.append(f"[步骤 {step.index}]")
        if step.thought:
            lines.append(f"  思考：{_preview(step.thought)}")
        if step.action:
            lines.append(f"  行动：{_preview(step.action)}")
        if step.observation is not None:
            lines.append(f"  观察：{_preview(step.observation)}")
        if step.error:
            lines.append(f"  异常：{_preview(step.error)}")
    lines.append(_THIN_SEPARATOR)
    lines.append(f"最终答案：{result.answer or '（未得出结论）'}")
    if not result.success and result.error:
        lines.append(f"结束原因：{result.error}")
    usage = result.usage
    lines.append(
        f"模型调用：{usage.calls} 次；token 合计约 {usage.total_tokens}"
        f"{'（估算值）' if usage.estimated else ''}；耗时 {result.duration_ms:.0f} ms"
    )
    lines.append(_SEPARATOR)
    return "\n".join(lines)


def _run_command(args: argparse.Namespace) -> int:
    """执行 run 子命令。"""
    settings = _load_settings(args)
    agent_name = args.agent or settings.extra.get("agent") or "react"
    task = args.task or _prompt_for_task()
    if not task:
        print("错误：任务不能为空，请通过 --task 或交互式输入提供。")
        return 2

    if args.llm == "mock":
        llm = demo_responses_llm(agent_name)
        tools = _build_tools(mock=True)
    else:
        settings.validate()
        llm = OpenAICompatibleLLM.from_settings(settings)
        tools = _build_tools(mock=False)

    agent = default_registry.create(
        agent_name,
        llm=llm,
        tools=tools,
        middlewares=_build_middlewares(settings, quiet=args.quiet),
        max_steps=settings.max_steps,
    )
    result = agent.run(task)

    if args.trace:
        saved = JsonStore.save(result.to_dict(), args.trace)
        print(f"轨迹已写入：{saved}")
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_result(result))
    return 0 if result.success else 1


def main(argv: Sequence[str] | None = None) -> int:
    """命令行主函数，返回进程退出码。"""
    _use_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "list":
            print("可用智能体：")
            print(default_registry.describe())
            print("\n可用工具：")
            print(_build_tools(mock=False).describe())
            return 0

        if args.command == "config":
            settings = _load_settings(args)
            print("当前配置（密钥已脱敏）：")
            for key, value in settings.masked().items():
                print(f"- {key}: {value}")
            if settings.missing_keys():
                print("提示：缺少必需项，使用真实模型前请先补齐 .env。")
            return 0

        return _run_command(args)
    except ConfigError as exc:
        print(f"配置错误：{exc}")
        return 2
    except AgentNotFoundError as exc:
        print(f"错误：{exc}")
        return 2
    except AgentCodeError as exc:
        print(f"运行失败：{exc}")
        return 1
