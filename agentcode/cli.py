"""命令行入口：run / web / open / list / config 五个子命令。"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

from agentcode import agents  # noqa: F401  导入即触发内置智能体注册
from agentcode.config import Settings
from agentcode.core.errors import AgentCodeError, AgentNotFoundError, ConfigError
from agentcode.core.registry import default_registry
from agentcode.core.result import AgentResult
from agentcode.llm.mock import demo_responses_llm
from agentcode.llm.openai_compatible import OpenAICompatibleLLM
from agentcode.memory import FileSessionStore, JsonStore, ShortTermMemory
from agentcode.memory.session_store import DEFAULT_SESSION_DIR
from agentcode.middleware import (
    BudgetMiddleware,
    LoggingMiddleware,
    RetryMiddleware,
    TimeoutMiddleware,
    TokenUsageMiddleware,
)
from agentcode.tools.base import ToolRegistry
from agentcode.tools.builtin import register_builtin_tools, register_demo_tools
from agentcode.tools.code import register_code_tools

_SEPARATOR = "=" * 48
_THIN_SEPARATOR = "-" * 48
_OBSERVATION_PREVIEW = 200
#: 网页默认使用的模型模式；离线演示只在显式传 --llm mock 时启用
_DEFAULT_WEB_MODE = "openai"


def _add_web_arguments(parser: argparse.ArgumentParser) -> None:
    """``web`` 与 ``open`` 两个子命令共用的参数。"""
    parser.add_argument("--host", default=None, help="监听地址，默认 127.0.0.1")
    parser.add_argument(
        "--port", type=int, default=None, help="监听端口，默认 8000；填 0 表示随机"
    )
    parser.add_argument(
        "--llm",
        choices=["openai", "mock"],
        default=None,
        help="模型模式，默认 openai；mock 仅供离线演示与自动化测试",
    )
    parser.add_argument("--verbose", action="store_true", help="打印访问日志")
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="免登录模式（仅限本机自用；公网必须保留登录）",
    )
    parser.add_argument("--env-file", default=None, help="指定 .env 文件路径")
    parser.add_argument("--config", default=None, help="JSON 配置文件路径")


def _add_data_path_arguments(parser: argparse.ArgumentParser) -> None:
    """给"只读数据目录"的子命令加参数（审计、回收站用）。

    这些命令不碰模型，所以只给 .env / JSON 配置的入口——
    密钥没配也照样能用，那正是库出问题时最需要它们的时候。
    """
    parser.add_argument("--env-file", default=None, help="指定 .env 文件路径")
    parser.add_argument("--config", default=None, help="JSON 配置文件路径")


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="agentcode",
        description="可扩展的 Python 智能体框架（ReAct / Plan-and-Solve / Reflection / Coding）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="运行一个智能体")
    run_parser.add_argument("--agent", default=None, help="智能体名称，默认 react 或配置文件中的写法")
    run_parser.add_argument("--task", default=None, help="任务描述，未提供时进入交互式输入")
    run_parser.add_argument(
        "--file",
        default=None,
        help="被测源码文件：复制到代码工作目录，并作为测试生成任务的输入",
    )
    run_parser.add_argument(
        "--llm",
        choices=["openai", "mock"],
        default="openai",
        help="模型后端；mock 为离线演示，不消耗额度",
    )
    run_parser.add_argument("--max-steps", type=int, default=None, help="覆盖最大步数")
    run_parser.add_argument(
        "--memory-turns", type=int, default=None, help="上下文记忆保留几轮，默认取配置"
    )
    run_parser.add_argument("--trace", default=None, help="把运行轨迹写入指定 JSON 文件")
    run_parser.add_argument("--json", action="store_true", help="以 JSON 形式输出结果")
    run_parser.add_argument("--quiet", action="store_true", help="不打印过程日志")
    run_parser.add_argument(
        "--no-code", action="store_true", help="禁止 coding 智能体执行代码（默认允许）"
    )
    run_parser.add_argument(
        "--session",
        default=None,
        help="会话名称；带上它就能跨次运行延续上下文，例如 --session 北京游",
    )
    run_parser.add_argument(
        "--session-dir", default=None, help=f"会话文件目录，默认 {DEFAULT_SESSION_DIR}"
    )
    run_parser.add_argument("--env-file", default=None, help="指定 .env 文件路径")
    run_parser.add_argument("--config", default=None, help="JSON 配置文件路径")

    web_parser = subparsers.add_parser("web", help="启动本地可视化网页")
    _add_web_arguments(web_parser)
    web_parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")

    open_parser = subparsers.add_parser("open", help="打开网页：没启动就顺手启动")
    _add_web_arguments(open_parser)
    open_parser.add_argument(
        "--no-browser", action="store_true", help="只确保服务在运行，不打开浏览器"
    )

    testgen_parser = subparsers.add_parser(
        "test-gen", help="给源码文件补 pytest 测试（放进 CI 就是一道门禁）"
    )
    testgen_parser.add_argument("path", help="源码文件或目录")
    testgen_parser.add_argument("--out", default=None, help="测试写到哪个目录，默认放源文件旁边")
    testgen_parser.add_argument(
        "--force", action="store_true", help="已有的测试文件也覆盖（默认跳过）"
    )
    testgen_parser.add_argument(
        "--limit", type=int, default=20, help="一次最多处理几个文件，默认 20"
    )
    testgen_parser.add_argument("--dry-run", action="store_true", help="只列出要处理哪些文件")
    testgen_parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    testgen_parser.add_argument(
        "--llm", choices=["openai", "mock"], default="openai", help="模型后端，mock 供离线演示"
    )
    testgen_parser.add_argument("--quiet", action="store_true", help="不打印中间步骤")
    testgen_parser.add_argument("--env-file", default=None, help="指定 .env 文件路径")
    testgen_parser.add_argument("--config", default=None, help="JSON 配置文件路径")

    user_parser = subparsers.add_parser("user", help="管理账号（每个用户一个账号与额度）")
    user_sub = user_parser.add_subparsers(dest="user_command", required=True)
    add_parser = user_sub.add_parser("add", help="新建账号")
    add_parser.add_argument("name", help="账号名")
    add_parser.add_argument("--password", default=None, help="不填则自动生成并打印一次")
    add_parser.add_argument("--plan", default="free", help="套餐名，默认 free")
    add_parser.add_argument("--daily-limit", type=int, default=None, help="每日 token 上限")
    user_sub.add_parser("list", help="列出账号与今日用量")
    for action in ("disable", "enable"):
        action_parser = user_sub.add_parser(action, help=f"{action} 账号")
        action_parser.add_argument("name")
    limit_parser = user_sub.add_parser("limit", help="调整某个账号的每日额度")
    limit_parser.add_argument("name")
    limit_parser.add_argument("tokens", type=int)
    passwd_parser = user_sub.add_parser("passwd", help="修改账号密码")
    passwd_parser.add_argument("name")
    passwd_parser.add_argument("--password", default=None, help="不填则自动生成并打印一次")
    usage_parser = user_sub.add_parser("usage", help="查看账号最近用量")
    usage_parser.add_argument("name", nargs="?", help="不填则列出全部账号")
    plan_parser = user_sub.add_parser("plan", help="切换账号套餐（会延长期限）")
    plan_parser.add_argument("name")
    plan_parser.add_argument("plan", help="free / basic / pro / team / owner")
    plan_parser.add_argument("--months", type=int, default=1, help="有效月数，默认 1")
    email_parser = user_sub.add_parser("email", help="设置/查看接收通知的邮箱")
    email_parser.add_argument("name", help="账号名")
    email_parser.add_argument("--set", dest="address", default=None, help="新邮箱；给空串即清空")
    email_parser.add_argument("--clear", action="store_true", help="清空邮箱（不再接收通知）")

    billing_parser = subparsers.add_parser("billing", help="订单与开通（收费相关）")
    billing_sub = billing_parser.add_subparsers(dest="billing_command", required=True)
    orders_parser = billing_sub.add_parser("orders", help="列出订单")
    orders_parser.add_argument("--account", default=None, help="只看某个账号")
    orders_parser.add_argument("--limit", type=int, default=20)
    orders_parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    costs_parser = billing_sub.add_parser(
        "costs", help="用量换算成钱：本月花了多少、每个套餐还剩多少毛利"
    )
    costs_parser.add_argument("--account", default=None, help="只看某个账号")
    costs_parser.add_argument("--days", type=int, default=30, help="看最近多少天，默认 30")
    costs_parser.add_argument(
        "--output-ratio",
        type=float,
        default=None,
        help="假设输出占多少（0~1）；没有真实用量时用来估算，默认 0.2",
    )
    costs_parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    costs_parser.add_argument("--env-file", default=None, help="指定 .env 文件路径")
    costs_parser.add_argument("--config", default=None, help="JSON 配置文件路径")
    grant_parser = billing_sub.add_parser("grant", help="直接开通/续期，不经过订单")
    grant_parser.add_argument("name")
    grant_parser.add_argument("plan")
    grant_parser.add_argument("--months", type=int, default=1)
    confirm_parser = billing_sub.add_parser("confirm", help="确认到账并开通")
    confirm_parser.add_argument("order_id")
    confirm_parser.add_argument("--reference", default=None, help="支付流水备注")

    backup_parser = subparsers.add_parser(
        "backup", help="备份与恢复（数据要有第二份，而且演练过）"
    )
    backup_sub = backup_parser.add_subparsers(dest="backup_command", required=True)

    backup_create = backup_sub.add_parser("create", help="把整份安装打成一个 .tar.gz")
    backup_create.add_argument("--out", default=None, help="备份文件路径")
    backup_create.add_argument(
        "--dir", default=None, help="备份放哪个目录（没给 --out 时用），默认 backups"
    )
    backup_create.add_argument("--json", action="store_true", help="以 JSON 输出")
    backup_create.add_argument("--env-file", default=None, help="指定 .env 文件路径")
    backup_create.add_argument("--config", default=None, help="JSON 配置文件路径")

    backup_verify = backup_sub.add_parser(
        "verify", help="恢复演练：解开包、真的把库打开读一遍"
    )
    backup_verify.add_argument("archive", help="备份文件路径")
    backup_verify.add_argument("--json", action="store_true", help="以 JSON 输出")

    backup_restore = backup_sub.add_parser("restore", help="把备份解到一个目录")
    backup_restore.add_argument("archive", help="备份文件路径")
    backup_restore.add_argument("--to", required=True, help="恢复到哪个目录")
    backup_restore.add_argument(
        "--force", action="store_true", help="目标非空也恢复：旧数据改名留一份，不删除"
    )
    backup_restore.add_argument("--yes", action="store_true", help="跳过二次确认（脚本里用）")

    backup_list = backup_sub.add_parser("list", help="列出某个目录里的备份")
    backup_list.add_argument("--dir", default=None, help="备份目录，默认 backups")

    audit_parser = subparsers.add_parser("audit", help="操作审计：谁在什么时候做了什么")
    audit_sub = audit_parser.add_subparsers(dest="audit_command", required=True)
    audit_list = audit_sub.add_parser("list", help="列最近的审计记录")
    audit_list.add_argument("--limit", type=int, default=50, help="看多少条，默认 50")
    audit_list.add_argument("--actor", default=None, help="只看某个账号")
    audit_list.add_argument("--action", default=None, help="只看某个动作，如 session.delete")
    audit_list.add_argument(
        "--action-prefix", dest="action_prefix", default=None, help="按动作前缀筛，如 login."
    )
    audit_list.add_argument("--json", action="store_true", help="以 JSON 输出")
    audit_prune = audit_sub.add_parser("prune", help="删掉太老的审计记录")
    audit_prune.add_argument("--days", type=int, default=None, help="默认用 AGENT_AUDIT_DAYS")
    audit_prune.add_argument("--env-file", default=None, help="指定 .env 文件路径")
    audit_prune.add_argument("--config", default=None, help="JSON 配置文件路径")

    notify_parser = subparsers.add_parser("notify", help="服务通知：发出去没有、发到哪")
    notify_sub = notify_parser.add_subparsers(dest="notify_command", required=True)
    notify_list = notify_sub.add_parser("list", help="看通知台账（发成功/排队/失败）")
    notify_list.add_argument("--limit", type=int, default=20, help="看多少条，默认 20")
    notify_list.add_argument("--account", default=None, help="只看某个账号")
    notify_list.add_argument("--status", default=None, help="只看某种状态：sent/queued/failed/skipped")
    notify_list.add_argument("--json", action="store_true", help="以 JSON 输出")
    _add_data_path_arguments(notify_list)
    notify_test = notify_sub.add_parser("test", help="给某个账号真发一封测试通知")
    notify_test.add_argument("account", help="账号名")
    _add_data_path_arguments(notify_test)

    token_parser = subparsers.add_parser("token", help="API 令牌：给脚本和 CI 用")
    token_sub = token_parser.add_subparsers(dest="token_command", required=True)
    token_create = token_sub.add_parser("create", help="给某个账号建一把令牌（明文只显示一次）")
    token_create.add_argument("account", help="账号名")
    token_create.add_argument("--name", default="", help="给这把令牌起个名字，比如 CI")
    token_create.add_argument("--days", type=int, default=None, help="多少天后过期；不填＝不过期")
    _add_data_path_arguments(token_create)
    token_list = token_sub.add_parser("list", help="列出某个账号的令牌")
    token_list.add_argument("account", help="账号名")
    token_list.add_argument("--json", action="store_true", help="以 JSON 输出")
    token_revoke = token_sub.add_parser("revoke", help="吊销一把令牌")
    token_revoke.add_argument("account", help="账号名")
    token_revoke.add_argument("token_id", help="令牌 id（用 token list 查）")

    api_parser = subparsers.add_parser("api", help="对外 API 的任务台账（运维侧）")
    api_sub = api_parser.add_subparsers(dest="api_command", required=True)
    api_list = api_sub.add_parser("list", help="看某个账号最近的 API 任务")
    api_list.add_argument("account", help="账号名")
    api_list.add_argument("--limit", type=int, default=20, help="看多少条，默认 20")
    api_list.add_argument("--json", action="store_true", help="以 JSON 输出")
    api_callbacks = api_sub.add_parser(
        "callbacks", help="看某个账号的完成回调投递到了哪一步"
    )
    api_callbacks.add_argument("account", help="账号名")
    api_callbacks.add_argument("--limit", type=int, default=20, help="看多少条，默认 20")
    api_callbacks.add_argument("--json", action="store_true", help="以 JSON 输出")

    trash_parser = subparsers.add_parser("trash", help="回收站：误删的东西在这里")
    trash_sub = trash_parser.add_subparsers(dest="trash_command", required=True)
    trash_list = trash_sub.add_parser("list", help="看某个账号的回收站")
    trash_list.add_argument("account", help="账号名")
    trash_list.add_argument("--json", action="store_true", help="以 JSON 输出")
    _add_data_path_arguments(trash_list)
    trash_restore = trash_sub.add_parser("restore", help="把一条放回去")
    trash_restore.add_argument("account", help="账号名")
    trash_restore.add_argument("kind", choices=["session", "code"], help="session=会话，code=代码目录")
    trash_restore.add_argument("entry", help="条目名（用 trash list 查）")
    _add_data_path_arguments(trash_restore)
    trash_purge = trash_sub.add_parser("purge", help="彻底删掉一条；不给条目就清空整个回收站")
    trash_purge.add_argument("account", help="账号名")
    trash_purge.add_argument(
        "kind", nargs="?", choices=["session", "code"], help="不给就清空整个回收站"
    )
    trash_purge.add_argument("entry", nargs="?", help="条目名")
    trash_purge.add_argument("--yes", action="store_true", help="清空时不问一遍")
    _add_data_path_arguments(trash_purge)

    list_parser = subparsers.add_parser("list", help="列出已注册的智能体与工具")
    list_parser.add_argument("--env-file", default=None, help="指定 .env 文件路径")
    list_parser.add_argument("--config", default=None, help="JSON 配置文件路径")

    config_parser = subparsers.add_parser("config", help="显示解析后的配置（密钥脱敏）")
    config_parser.add_argument("--env-file", default=None, help="指定 .env 文件路径")
    config_parser.add_argument("--config", default=None, help="JSON 配置文件路径")

    return parser


def _use_utf8_stdout() -> None:
    """在真实终端下切换到 UTF-8，避免 Windows 控制台中文乱码。

    另外统一加上 ``errors="replace"``：万一输出里出现当前编码表示不了的字符
    （Windows 中文版控制台默认是 GBK，像 ✓ 这种符号编不出来），
    也只是显示成问号，而不是整个命令崩掉。重定向到文件时尤其要这样。
    """
    stream = sys.stdout
    try:
        if stream.isatty():
            stream.reconfigure(encoding="utf-8", errors="replace")
        else:
            stream.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):
        return


def _load_settings(args: argparse.Namespace) -> Settings:
    """读取 .env 与可选 JSON 配置。"""
    settings = Settings.from_env(env_file=getattr(args, "env_file", None))
    config_path = getattr(args, "config", None)
    if config_path:
        settings = settings.apply_overrides(JsonStore.load(config_path))
    overrides: dict[str, Any] = {}
    max_steps = getattr(args, "max_steps", None)
    if max_steps:
        # 显式指定步数时对循环类智能体一并生效
        overrides.update({"max_steps": max_steps, "coding_steps": max_steps})
    memory_turns = getattr(args, "memory_turns", None)
    if memory_turns:
        overrides["memory_turns"] = memory_turns
    return settings.apply_overrides(overrides) if overrides else settings


def _web_target(args: argparse.Namespace, settings: Settings) -> tuple[str, int, str]:
    """解析网页服务的地址、端口与模型模式。"""
    from agentcode.web.server import DEFAULT_HOST, DEFAULT_PORT

    host = args.host or settings.extra.get("web_host") or DEFAULT_HOST
    port = args.port if args.port is not None else int(settings.extra.get("web_port", DEFAULT_PORT))
    llm_mode = args.llm or settings.extra.get("llm_mode") or _DEFAULT_WEB_MODE
    return host, port, llm_mode


def _session_dir(args: argparse.Namespace, settings: Settings) -> str:
    """解析会话文件目录。"""
    return (
        getattr(args, "session_dir", None)
        or settings.extra.get("session_dir")
        or DEFAULT_SESSION_DIR
    )


def _build_tools(
    mock: bool,
    settings: Settings,
    create_root: bool = True,
    agent_name: str | None = None,
    allow_code: bool = True,
) -> ToolRegistry:
    """按后端类型与智能体准备工具集。

    代码工具只给 coding 智能体，避免别的智能体在工具清单里被"诱导"去跑代码。
    命令行跑在你自己机器上，默认允许（可用 --no-code 关掉）；网页端由
    ``AGENT_ALLOW_CODE_TOOLS`` 控制，默认不允许。
    """
    registry = ToolRegistry()
    register_builtin_tools(
        registry, include_search=not mock, serpapi_key=settings.serpapi_key
    )
    if mock:
        register_demo_tools(registry)
    if not allow_code or agent_name not in {"coding", "test_gen"}:
        return registry
    register_code_tools(
        registry,
        root=settings.code_root,
        timeout=settings.code_timeout,
        output_limit=settings.code_output_limit,
        create=create_root,
        execution_backend=settings.execution_backend,
        docker_image=settings.docker_image,
        docker_binary=settings.docker_binary,
        docker_memory=settings.docker_memory,
        docker_cpus=settings.docker_cpus,
        docker_pids_limit=settings.docker_pids_limit,
        docker_user=settings.docker_user,
    )
    return registry


def _build_middlewares(settings: Settings, quiet: bool) -> list[Any]:
    """组装默认中间件链（顺序即包装顺序，最外层在前）。"""
    return [
        BudgetMiddleware(max_tokens=settings.run_token_budget),
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


def _audit_command(args: argparse.Namespace) -> int:
    """看审计日志、清过期记录。

    **只在命令行**：这是运营数据，网页上不给用户看别人（以及自己）的每一步操作。
    """
    from agentcode.accounts import AccountStore
    from agentcode.audit import AuditLog
    from agentcode.config import DEFAULT_DB_PATH

    store = AccountStore(os.environ.get("AGENT_DB_PATH") or DEFAULT_DB_PATH)
    log = AuditLog(store)

    if args.audit_command == "prune":
        days = args.days if args.days is not None else int(_load_settings(args).audit_days)
        if days <= 0:
            print("没删：保留天数 ≤ 0 表示永久保留；要删就给个天数（--days 30）。")
            return 0
        print(f"已删掉 {store.prune_audit(days)} 条超过 {days} 天的审计记录。")
        return 0

    entries = log.recent(
        limit=args.limit,
        actor_name=args.actor,
        action=args.action,
        action_prefix=args.action_prefix,
    )
    if args.json:
        print(json.dumps({"entries": entries}, ensure_ascii=False))
        return 0
    if not entries:
        print("（没有符合条件的记录）")
        return 0
    print(f"最近 {len(entries)} 条（新的在上面）：")
    for item in entries:
        who = str(item["actor_name"] or "（匿名）")
        target = str(item["target"] or "-")
        mark = "" if item["result"] == "ok" else f"  [{item['result']}]"
        detail = item["detail"]
        tail = f"  {json.dumps(detail, ensure_ascii=False)}" if detail else ""
        print(f"  {item['at']}  {who:<12} {str(item['action']):<18} {target}{mark}{tail}")
    return 0


def _trash_command(args: argparse.Namespace) -> int:
    """回收站：看、恢复、彻底删。

    主要给运营方用——用户投诉"我误删了"，客服得能立刻查、立刻恢复。
    """
    from agentcode.accounts import AccountStore
    from agentcode.config import DEFAULT_DB_PATH
    from agentcode.trash import (
        TrashError,
        account_trash,
        empty_trash,
        purge_entry,
        restore_entry,
    )

    store = AccountStore(os.environ.get("AGENT_DB_PATH") or DEFAULT_DB_PATH)
    account = store.get(args.account)
    if account is None:
        print(f"错误：没有这个账号：{args.account}")
        return 2
    settings = _load_settings(args)

    if args.trash_command == "list":
        trash = account_trash(settings, account.id)
        if args.json:
            print(
                json.dumps(
                    {kind: [item.to_dict() for item in items] for kind, items in trash.items()},
                    ensure_ascii=False,
                )
            )
            return 0
        if not trash["sessions"] and not trash["code"]:
            print(f"{account.name} 的回收站是空的。")
            return 0
        for kind, items in trash.items():
            for item in items:
                print(
                    f"  [{kind}] {item.entry}  {item.name}"
                    f"（{item.files} 个文件，删于 {item.deleted_at}）"
                )
        return 0

    try:
        if args.trash_command == "restore":
            report = restore_entry(settings, account.id, args.kind, args.entry)
            print(f"已恢复：{report['path']}")
            return 0

        if args.kind and args.entry:
            removed = purge_entry(settings, account.id, args.kind, args.entry)
            print("已彻底删除。" if removed else "没有找到这一条。")
            return 0 if removed else 1
        if args.kind and not args.entry:
            print("错误：只给了类型没给条目。给个条目名，或者两个都不给来清空回收站。")
            return 2
        if not args.yes:
            try:
                answer = input(f"清空 {account.name} 的回收站？里面的东西再也找不回来，输入 yes 继续：")
            except EOFError:
                print("读不到确认输入（非交互环境）；确定要清空请加 --yes。")
                return 1
            if answer.strip().lower() not in {"yes", "y"}:
                print("已取消，什么都没改。")
                return 1
        removed = empty_trash(settings, account.id)
        print(f"已清空：会话 {removed['sessions']} 项、代码目录 {removed['code']} 项。")
        return 0
    except TrashError as exc:
        print(f"错误：{exc}")
        return 1


def _notify_command(args: argparse.Namespace) -> int:
    """通知台账与测试发送。

    没配渠道时通知只会记进台账（``queued``），所以这个命令的第二个用途是
    **自查**："我以为配好了，其实一封都没发出去"要能一眼看出来。
    """
    from agentcode.accounts import AccountStore
    from agentcode.config import DEFAULT_DB_PATH
    from agentcode.notify import Notifier

    store = AccountStore(os.environ.get("AGENT_DB_PATH") or DEFAULT_DB_PATH)
    settings = _load_settings(args)

    if args.notify_command == "test":
        account = store.get(args.account)
        if account is None:
            print(f"错误：没有这个账号：{args.account}")
            return 2
        notifier = Notifier(store, settings)
        if not account.email:
            print(f"{account.name} 还没填邮箱，先补一个：agentcode user email {account.name} --set a@b.com")
            return 1
        print(f"通知渠道：{notifier.channel_name()}")
        result = notifier.notify(
            "notify.test",
            account=account,
            subject="AgentCode 测试通知",
            body=f"这是发给 {account.name} 的一条测试通知。收到说明渠道配对了。",
            dedupe_key=f"test-{time.time():.0f}",
        )
        if result["delivered"]:
            print(f"已发出（{result['channel']}）→ {account.email}")
            return 0
        print(f"没发出去：{result.get('reason') or result.get('error') or result['status']}")
        return 1

    account_id = None
    if args.account:
        target = store.get(args.account)
        if target is None:
            print(f"错误：没有这个账号：{args.account}")
            return 2
        account_id = target.id
    rows = store.notifications(limit=args.limit, account_id=account_id, status=args.status)
    if args.json:
        print(json.dumps({"notifications": rows}, ensure_ascii=False))
        return 0
    if not rows:
        print("（通知台账是空的）")
        return 0
    print(f"最近 {len(rows)} 条：")
    for item in rows:
        who = item["account_name"] or "-"
        mark = {"sent": "已发", "queued": "未发（没配渠道）", "failed": "失败", "skipped": "跳过"}.get(
            str(item["status"]), str(item["status"])
        )
        note = f"：{item['error']}" if item["error"] else ""
        print(f"  {item['at']}  {who:<10} {item['event']:<26} {mark}{note}")
    print("  提示：agentcode notify test <账号> 可以真发一封试试渠道通不通。")
    return 0


def _token_command(args: argparse.Namespace) -> int:
    """API 令牌的运营侧管理。

    用户自己在网页上也能建；这里给运营方用——比如用户说"我想接 CI"，
    客服能当场建一把、把明文念给他（**明文只出现这一次**）。
    """
    from agentcode import audit as AUDIT
    from agentcode.accounts import AccountStore
    from agentcode.audit import AuditLog
    from agentcode.config import DEFAULT_DB_PATH
    from agentcode.tokens import ApiTokenService

    store = AccountStore(os.environ.get("AGENT_DB_PATH") or DEFAULT_DB_PATH)
    service = ApiTokenService(store)
    account = store.get(args.account)
    if account is None:
        print(f"错误：没有这个账号：{args.account}")
        return 2
    log = AuditLog(store)

    if args.token_command == "create":
        record, plaintext = service.create(account, name=args.name, expires_days=args.days)
        log.record(
            AUDIT.API_TOKEN_CREATE,
            actor_name="命令行",
            target=str(record["name"]),
            detail={"account": account.name, "prefix": record["prefix"]},
        )
        print(f"已为 {account.name} 创建令牌「{record['name']}」")
        print(_THIN_SEPARATOR)
        print(plaintext)
        print(_THIN_SEPARATOR)
        print("这串令牌**只显示这一次**，请立刻复制保存；丢了只能吊销后重建。")
        if record["expires_at"]:
            print(f"到期时间：{record['expires_at']}")
        print(f"用法：curl -X POST <服务地址>/v1/run -H \"Authorization: Bearer {plaintext[:12]}…\" …")
        return 0

    if args.token_command == "revoke":
        revoked = service.revoke(account.id, args.token_id)
        log.record(
            AUDIT.API_TOKEN_REVOKE,
            actor_name="命令行",
            target=args.token_id,
            result="ok" if revoked else "not_found",
            detail={"account": account.name},
        )
        print("已吊销。" if revoked else "没有找到这把令牌（或者它已经吊销过了）。")
        return 0 if revoked else 1

    tokens = service.list(account.id)
    if args.json:
        print(json.dumps({"tokens": tokens}, ensure_ascii=False))
        return 0
    if not tokens:
        print(f"{account.name} 还没有 API 令牌。用 agentcode token create {account.name} 建一把。")
        return 0
    print(f"{account.name} 的令牌：")
    for item in tokens:
        state = "已吊销" if not item["is_active"] else ("已过期" if item["expired"] else "有效")
        used = item["last_used_at"] or "从未使用"
        expires = item["expires_at"] or "不过期"
        print(f"  {item['id']}  {item['name']:<12} {item['prefix']}…  {state}  最近使用 {used}  到期 {expires}")
    return 0


def _api_command(args: argparse.Namespace) -> int:
    """对外 API 的任务台账：用户说"我提交的任务一直没结果"时查这里。

    卡在 ``running`` 的旧记录会在定期清理里被标成失败（服务重启、线程意外死掉
    都会留下这种记录）——所以看到失败先看 ``error`` 写了什么，那是原因。

    异步任务配了 ``callback_url`` 的话，投递到了哪一步也记在同一行台账里
    （``api callbacks <账号>`` 看细节）。
    """
    from agentcode.accounts import AccountStore
    from agentcode.config import DEFAULT_DB_PATH

    store = AccountStore(os.environ.get("AGENT_DB_PATH") or DEFAULT_DB_PATH)
    account = store.get(args.account)
    if account is None:
        print(f"错误：没有这个账号：{args.account}")
        return 2
    if args.api_command == "callbacks":
        return _api_callbacks(store, account, args)
    runs = store.api_runs(account.id, limit=max(1, int(args.limit)))
    if args.json:
        print(json.dumps({"runs": runs}, ensure_ascii=False))
        return 0
    if not runs:
        print(f"{account.name} 还没有通过 API 提交过任务。")
        return 0
    print(f"{account.name} 最近 {len(runs)} 个 API 任务：")
    for item in runs:
        task = str(item["task"]).replace("\n", " ")[:28]
        mark = {"running": "跑着", "succeeded": "成功", "failed": "失败"}.get(
            str(item["status"]), str(item["status"])
        )
        key = f"  幂等键 {item['request_key']}" if item["request_key"] else ""
        callback = _callback_mark(item)
        print(
            f"  {item['created_at']}  {item['run_id']}  {item['agent']:<12}"
            f" {mark}  {task}{key}{callback}"
        )
    print("  要看某个任务的答案：GET /v1/runs/<run_id>（带令牌）")
    return 0


def _callback_mark(item: dict[str, Any]) -> str:
    """``api list`` 里那一小段回调状态；没有回调就什么都不加。"""
    status = str(item.get("callback_status") or "none")
    attempts = int(item.get("callback_attempts") or 0)
    if status == "succeeded":
        return "  回调 已发"
    if status == "pending":
        return f"  回调 待发（已试 {attempts} 次）" if attempts else "  回调 待发"
    if status == "failed":
        return f"  回调 失败（试了 {attempts} 次）"
    return ""


def _api_callbacks(store: Any, account: Any, args: argparse.Namespace) -> int:
    """配过回调的任务：投递到了哪一步、为什么没发出去。

    手写的地址（特别是打内网的）在**提交时**就被拒了，所以这里看到的都是
    过了校验的地址。
    """
    from agentcode.callbacks import safe_target

    runs = store.callback_runs(account.id, limit=max(1, int(args.limit)))
    if args.json:
        print(json.dumps({"callbacks": runs}, ensure_ascii=False))
        return 0
    if not runs:
        print(f"{account.name} 还没有配过回调的 API 任务。")
        return 0
    print(f"{account.name} 最近 {len(runs)} 个带回调的任务：")
    for item in runs:
        status = str(item.get("callback_status") or "none")
        mark = {
            "pending": "待发",
            "succeeded": "已发",
            "failed": "失败",
            "none": "（没配）",
        }.get(status, status)
        attempts = int(item.get("callback_attempts") or 0)
        line = (
            f"  {item['created_at']}  {item['run_id']}  {mark}"
            f"  已试 {attempts} 次  {safe_target(str(item.get('callback_url') or ''))}"
        )
        if item.get("callback_delivered_at"):
            line += f"  送达 {item['callback_delivered_at']}"
        if item.get("callback_next_at"):
            line += f"  下次 {item['callback_next_at']}"
        print(line)
        if item.get("callback_error"):
            print(f"      最后错误：{item['callback_error']}")
    print("  重试：网络错误/5xx/429 按退避重试，最多 AGENT_CALLBACK_MAX_ATTEMPTS 次；4xx 直接放弃")
    return 0


def _run_command(args: argparse.Namespace) -> int:
    """执行 run 子命令。"""
    settings = _load_settings(args)
    agent_name = args.agent or settings.extra.get("agent") or "react"
    task = args.task or _prompt_for_task()

    # --file：把被测源码复制到代码工作目录，并据此组织任务描述
    source_file = getattr(args, "file", None)
    if source_file:
        source = Path(source_file)
        if not source.is_file():
            print(f"错误：找不到文件 {source_file}")
            return 2
        workspace = Path(settings.code_root)
        workspace.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, workspace / source.name)
        print(f"已把被测文件复制到工作目录：{workspace / source.name}")
        extra = f"\n补充要求：{task}" if task else ""
        task = f"为 {source.name} 生成 pytest 测试用例，覆盖正常路径与边界情况。{extra}"

    if not task:
        print("错误：任务不能为空，请通过 --task 或交互式输入提供。")
        return 2

    session_name = getattr(args, "session", None)
    session_store = (
        FileSessionStore(_session_dir(args, settings), max_turns=settings.memory_turns)
        if session_name
        else None
    )
    memory = (
        session_store.load(session_name)
        if session_store
        else ShortTermMemory(max_turns=settings.memory_turns)
    )

    if args.llm == "mock":
        llm = demo_responses_llm(agent_name)
        tools = _build_tools(mock=True, settings=settings, agent_name=agent_name)
    else:
        settings.validate()
        llm = OpenAICompatibleLLM.from_settings(settings)
        tools = _build_tools(
            mock=False,
            settings=settings,
            agent_name=agent_name,
            allow_code=not getattr(args, "no_code", False),
        )

    agent = default_registry.create(
        agent_name,
        llm=llm,
        tools=tools,
        middlewares=_build_middlewares(settings, quiet=args.quiet),
        max_steps=settings.max_steps_for(agent_name),
        memory=memory,
    )
    if session_store and len(agent.memory):
        print(f"已载入会话「{session_name}」的 {len(agent.memory)} 轮上下文。")

    result = agent.run(task)

    if session_store:
        saved = session_store.save(session_name, agent.memory)
        print(f"会话已保存：{saved}（共 {len(agent.memory)}/{settings.memory_turns} 轮）")
    if args.trace:
        saved_trace = JsonStore.save(result.to_dict(), args.trace)
        print(f"轨迹已写入：{saved_trace}")
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_result(result))
    return 0 if result.success else 1


def _user_command(args: argparse.Namespace) -> int:
    """执行 user 子命令：开户、停用、调额度、看用量。

    每个**改动**都写一条审计：用户来问"我的账号怎么被停了"，得答得出来。
    """
    from agentcode.accounts import AccountStore
    from agentcode import audit as AUDIT
    from agentcode.audit import AuditLog
    from agentcode.config import DEFAULT_DB_PATH
    from agentcode.core.errors import AgentCodeError

    store = AccountStore(os.environ.get("AGENT_DB_PATH") or DEFAULT_DB_PATH)
    log = AuditLog(store)
    # 命令行没有登录态，用这个名字标明"这是运维/本机操作"
    OPERATOR = "命令行"
    command = args.user_command

    if command == "add":
        password = args.password or secrets.token_urlsafe(9)
        account = store.create(
            args.name, password, plan=args.plan, daily_token_limit=args.daily_limit
        )
        log.record(
            AUDIT.ACCOUNT_CREATE,
            actor_name=OPERATOR,
            target=account.name,
            detail={"plan": account.plan, "daily_limit": account.daily_token_limit},
        )
        print(
            f"已创建账号：{account.name}（套餐 {account.plan}，每日 {account.daily_token_limit} token）"
        )
        if not args.password:
            print(f"初始密码：{password}（只显示这一次，请立刻转交给用户）")
        return 0

    if command == "list":
        accounts = store.list()
        if not accounts:
            print("还没有账号。用 agentcode user add <名字> 创建。")
            return 0
        for account in accounts:
            used, calls = store.usage_today(account.id)
            state = "启用" if account.is_active else "停用"
            print(
                f"{account.name:<16}{account.plan:<8}{state:<4}"
                f"今日 {used}/{account.daily_token_limit} token，{calls} 次调用"
            )
        return 0

    if command in ("disable", "enable"):
        changed = store.set_active(args.name, command == "enable")
        log.record(
            AUDIT.ACCOUNT_ENABLE if command == "enable" else AUDIT.ACCOUNT_DISABLE,
            actor_name=OPERATOR,
            target=args.name,
            result="ok" if changed else "not_found",
        )
        print("已更新。" if changed else f"账号「{args.name}」不存在。")
        return 0 if changed else 1

    if command == "limit":
        changed = store.set_limit(args.name, args.tokens)
        log.record(
            AUDIT.ACCOUNT_LIMIT,
            actor_name=OPERATOR,
            target=args.name,
            result="ok" if changed else "not_found",
            detail={"tokens": args.tokens},
        )
        print("已更新。" if changed else f"账号「{args.name}」不存在。")
        return 0 if changed else 1

    if command == "passwd":
        password = args.password or secrets.token_urlsafe(9)
        changed = store.set_password(args.name, password)
        # 只记"改了哪个账号的密码"，密码本身绝不进审计
        log.record(
            AUDIT.ACCOUNT_PASSWD,
            actor_name=OPERATOR,
            target=args.name,
            result="ok" if changed else "not_found",
        )
        print("已更新密码。" if changed else f"账号「{args.name}」不存在。")
        if changed and not args.password:
            print(f"新密码：{password}（只显示这一次）")
        return 0 if changed else 1

    if command == "email":
        account = store.get(args.name)
        if account is None:
            print(f"账号「{args.name}」不存在。")
            return 1
        if args.address is None and not args.clear:
            print(f"{account.name} 的接收邮箱：{account.email or '（没填，不接收通知）'}")
            return 0
        try:
            changed = store.set_email(account.name, "" if args.clear else args.address)
        except AgentCodeError as exc:
            print(f"错误：{exc}")
            return 2
        log.record(
            AUDIT.ACCOUNT_EMAIL,
            actor_name=OPERATOR,
            target=account.name,
            result="ok" if changed else "not_found",
            detail={"email": store.get(account.name).email or ""},
        )
        print(f"已更新：{store.get(account.name).email or '（清空，不再接收通知）'}")
        return 0 if changed else 1

    if command == "plan":
        from agentcode.billing import BillingService
        from agentcode.core.errors import AgentCodeError

        account = store.get(args.name)
        if account is None:
            print(f"错误：没有这个账号：{args.name}")
            return 2
        try:
            refreshed = BillingService(store).grant(account, args.plan, args.months)
        except AgentCodeError as exc:
            print(f"错误：{exc}")
            return 2
        log.record(
            AUDIT.ACCOUNT_PLAN,
            actor_name=OPERATOR,
            target=refreshed.name,
            detail={
                "plan": refreshed.plan,
                "months": args.months,
                "expires_at": refreshed.plan_expires_at,
            },
        )
        expires = refreshed.plan_expires_at or "不过期"
        print(f"已把 {refreshed.name} 设为套餐 {refreshed.plan}，到期时间：{expires}")
        return 0

    # usage
    if args.name:
        account = store.get(args.name)
        if account is None:
            print(f"账号「{args.name}」不存在。")
            return 1
        targets = [account]
    else:
        targets = store.list()

    for account in targets:
        used, calls = store.usage_today(account.id)
        print(f"== {account.name}（今日 {used}/{account.daily_token_limit} token，{calls} 次）")
        for row in store.usage_history(account.id, limit=7):
            print(f"   {row['day']}  {row['tokens']} token / {row['calls']} 次")
    return 0


def _billing_command(args: argparse.Namespace) -> int:
    """执行 billing 子命令：看订单、确认到账、直接开通。

    这些动作**只在命令行**开放，网页上不给接口——否则等于给自己留了一条
    自助提权的路。
    """
    from agentcode.accounts import AccountStore
    from agentcode import audit as AUDIT
    from agentcode.audit import AuditLog
    from agentcode.billing import BillingService
    from agentcode.config import DEFAULT_DB_PATH
    from agentcode.core.errors import AgentCodeError

    if args.billing_command == "costs":
        return _costs_command(args)

    store = AccountStore(os.environ.get("AGENT_DB_PATH") or DEFAULT_DB_PATH)
    billing = BillingService(store)
    log = AuditLog(store)
    OPERATOR = "命令行"
    command = args.billing_command

    if command == "orders":
        orders = store.list_orders(args.account, args.limit)
        if args.json:
            print(json.dumps({"orders": orders}, ensure_ascii=False))
            return 0
        if not orders:
            print("（还没有订单）")
            return 0
        for order in orders:
            amount = order["amount_cents"] / 100
            print(
                f"{order['created_at']}  {order['id']}  {order['plan']} x{order['months']}  "
                f"{amount:.2f} {order['currency']}  [{order['status']}]"
            )
        return 0

    if command == "grant":
        account = store.get(args.name)
        if account is None:
            print(f"错误：没有这个账号：{args.name}")
            return 2
        try:
            refreshed = billing.grant(account, args.plan, args.months)
        except AgentCodeError as exc:
            print(f"错误：{exc}")
            return 2
        log.record(
            AUDIT.ACCOUNT_PLAN,
            actor_name=OPERATOR,
            target=refreshed.name,
            detail={"plan": refreshed.plan, "months": args.months, "source": "grant"},
        )
        expires = refreshed.plan_expires_at or "不过期"
        print(f"已把 {refreshed.name} 设为套餐 {refreshed.plan}，到期时间：{expires}")
        return 0

    if command == "confirm":
        try:
            order = billing.confirm(args.order_id, args.reference)
        except AgentCodeError as exc:
            print(f"错误：{exc}")
            return 2
        log.record(
            AUDIT.BILLING_CONFIRM,
            actor_name=OPERATOR,
            target=order.id,
            detail={"plan": order.plan, "amount_cents": order.amount_cents},
        )
        print(f"订单 {order.id} 已确认到账，套餐 {order.plan} 生效 {order.months} 个月。")
        return 0

    print("错误：未知的 billing 子命令。")
    return 2


# ---------------------------------------------------------------------- backup


def _backup_command(args: argparse.Namespace) -> int:
    """执行 backup 子命令：备份、演练、恢复、列清单。

    ``verify`` / ``restore`` / ``list`` **不读配置**：.env 缺了、库坏了的时候，
    恰恰是你最需要它们的时候，不能因为读不到模型配置就一起用不了。
    """
    from agentcode.backup import BackupError

    handlers = {
        "create": _backup_create,
        "verify": _backup_verify,
        "restore": _backup_restore,
        "list": _backup_list,
    }
    handler = handlers.get(args.backup_command)
    if handler is None:
        print("错误：未知的 backup 子命令。")
        return 2
    try:
        return handler(args)
    except BackupError as exc:
        print(f"错误：{exc}")
        return 1
    except OSError as exc:
        # 写到一半磁盘满了、目录没权限、文件被占用——这些都要给句人话，
        # 而不是甩一段调用栈
        print(f"错误：{exc}（磁盘满了、没有权限，或文件正被占用？）")
        return 1


def _backup_create(args: argparse.Namespace) -> int:
    """备一份。默认落到 ``backups/agentcode-<时间>.tar.gz``。"""
    from agentcode.backup import create_backup

    settings = _load_settings(args)
    if args.out:
        target = Path(args.out)
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = Path(args.dir or "backups") / f"agentcode-{stamp}.tar.gz"

    manifest = create_backup(settings, target)
    if args.json:
        print(json.dumps({**manifest.to_dict(), "path": str(target)}, ensure_ascii=False))
        return 0

    print(f"备份完成：{target}")
    print(
        f"  账号 {manifest.accounts} 个，账本 {manifest.ledger_rows} 行，"
        f"会话 {manifest.session_files} 个文件，代码 {manifest.code_files} 个文件"
        f"（schema v{manifest.schema_version}）"
    )
    print(f"  包大小：{_human_size(target.stat().st_size)}")
    print(f'  别忘了演练：agentcode backup verify "{target}"')
    return 0


def _backup_verify(args: argparse.Namespace) -> int:
    """演练：把包解开、真的把库打开读一遍。"""
    from agentcode.backup import verify_backup

    info = verify_backup(args.archive, detailed=True)
    if args.json:
        print(json.dumps(info, ensure_ascii=False))
        return 0

    names = "、".join(info["account_names"]) or "（还没有账号）"
    print(f"备份可用：{info['path']}")
    print(f"  备份时间：{info['created_at'] or '（包里没写）'}")
    print(f"  账号 {info['account_count']} 个：{names}")
    print(
        f"  账本 {info['ledger_rows']} 行，会话 {info['session_files']} 个文件，"
        f"代码 {info['code_files']} 个文件"
    )
    print(f"  数据库：能打开、完整性检查通过，schema v{info['schema_version']}")
    return 0


def _backup_restore(args: argparse.Namespace) -> int:
    """恢复：默认要人工确认一次，就地覆盖是最危险的操作，值得多按一下。"""
    from agentcode.backup import restore_backup

    target = Path(args.to)
    occupied = target.is_dir() and any(target.iterdir())
    if not args.yes:
        print(f"准备把 {args.archive} 解到：{target}")
        if occupied:
            print(
                "注意：目标目录非空。"
                + ("旧数据会改名留一份。" if args.force else "不加 --force 会被拒绝。")
            )
        try:
            answer = input("确认恢复请输入 yes：").strip().lower()
        except EOFError:
            print("读不到确认输入（非交互环境）；确定要恢复请加 --yes。")
            return 1
        if answer not in {"yes", "y"}:
            print("已取消，磁盘上什么都没改。")
            return 1

    report = restore_backup(args.archive, target, force=args.force)
    print(f"恢复完成：{report['path']}")
    print(f"  账号 {report['accounts']} 个，写回 {report['files']} 个文件")
    if report["moved_aside"]:
        print(f"  旧数据留在：{report['moved_aside']}（确认新数据没问题再自己删）")
    print("  提醒：换数据之前先把服务停掉，别让两个进程同时写同一个库。")
    return 0


def _backup_list(args: argparse.Namespace) -> int:
    """列出目录里有哪些备份（只看清单，不解包）。"""
    from agentcode.backup import BackupError, read_manifest

    directory = Path(args.dir or "backups")
    if not directory.is_dir():
        print(f"还没有备份：{directory} 不存在。用 agentcode backup create 备一份。")
        return 0

    archives = sorted(directory.glob("*.tar.gz"))
    if not archives:
        print(f"还没有备份：{directory} 里没有 .tar.gz 文件。")
        return 0

    print(f"{directory} 里的备份（新的在上面）：")
    for archive in sorted(archives, key=lambda item: item.stat().st_mtime, reverse=True):
        size = _human_size(archive.stat().st_size)
        try:
            manifest = read_manifest(archive)
        except BackupError as exc:
            print(f"  {archive.name}  [{size}]  读不出来：{exc}")
            continue
        print(
            f"  {archive.name}  [{size}]  {manifest.created_at or '时间未知'}  "
            f"账号 {manifest.accounts} 个，会话 {manifest.session_files} 个文件，"
            f"代码 {manifest.code_files} 个文件（schema v{manifest.schema_version}）"
        )
    print("  这里只看了清单；能不能恢复要用 agentcode backup verify <包> 演练一遍。")
    return 0


def _human_size(size: int) -> str:
    """把字节数写成好读的大小。"""
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _costs_command(args: argparse.Namespace) -> int:
    """把用量换算成钱：花销、每账号成本、每个套餐还剩多少毛利。

    这是**运营数据**，只在命令行看——用户不需要看你的成本。
    """
    from agentcode.accounts import AccountStore
    from agentcode.config import DEFAULT_DB_PATH
    from agentcode.pricing import observed_output_ratio, plan_margins, price_from_settings

    settings = _load_settings(args)
    price = price_from_settings(settings)
    store = AccountStore(os.environ.get("AGENT_DB_PATH") or DEFAULT_DB_PATH)

    days = max(1, int(args.days))
    today = datetime.now(timezone.utc).date()
    start = (today - timedelta(days=days - 1)).isoformat()
    end = today.isoformat()

    accounts = store.list()
    if args.account:
        accounts = [item for item in accounts if item.name == args.account]
        if not accounts:
            print(f"错误：没有这个账号：{args.account}")
            return 2

    rows = []
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "unsplit_tokens": 0}
    for account in accounts:
        breakdown = store.usage_breakdown(account.id, start, end)
        for key in totals:
            totals[key] += breakdown[key]
        rows.append({"name": account.name, "plan": account.plan, **breakdown})

    observed = observed_output_ratio(totals["prompt_tokens"], totals["completion_tokens"])
    assumed = getattr(args, "output_ratio", None)
    # 有真实数据就用真实的；没有就用一个**标明是假设**的默认值——
    # 只给最坏情况的话，第一次看的人会以为这功能坏了
    ratio = observed if observed is not None else (assumed if assumed is not None else 0.2)
    ratio_is_observed = observed is not None
    estimated_cost = price.cost_cents(totals["prompt_tokens"], totals["completion_tokens"])
    if totals["unsplit_tokens"]:
        # 老数据没有输入/输出拆分，按上面那个比例估——并且如实说是估的
        prompt = int(totals["unsplit_tokens"] * (1 - ratio))
        estimated_cost += price.cost_cents(prompt, totals["unsplit_tokens"] - prompt)

    payload = {
        "window": {"start": start, "end": end, "days": days},
        "price": {
            "input_cents_per_million": price.input_cents_per_million,
            "output_cents_per_million": price.output_cents_per_million,
            "configured": price.configured,
        },
        "totals": {**totals, "estimated_cost_cents": estimated_cost},
        "observed_output_ratio": observed,
        "output_ratio_used": ratio,
        "output_ratio_is_observed": ratio_is_observed,
        "accounts": rows,
        "plans": [item.to_dict() for item in plan_margins(price, output_ratio=ratio)],
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"== 最近 {days} 天（{start} ~ {end}）==")
    if not price.configured:
        print("提示：没配单价，只看得到 token、算不出钱。在 .env 里加：")
        print("  AGENT_PRICE_INPUT_PER_MILLION=输入单价（分/百万 token）")
        print("  AGENT_PRICE_OUTPUT_PER_MILLION=输出单价（分/百万 token）")
        print("  例：DeepSeek 输入 ￥2/M、输出 ￥8/M → 填 200 和 800")
    print(
        f"输入 {_format_tokens(totals['prompt_tokens'])}，"
        f"输出 {_format_tokens(totals['completion_tokens'])}，"
        f"合计 {_format_tokens(totals['total_tokens'])}"
    )
    if ratio_is_observed:
        print(f"输出占比 {ratio:.1%}（实测）；估算成本 {_yuan(estimated_cost)}")
    else:
        print(
            f"输出占比 {ratio:.1%}（假设值——还没有真实的输入/输出数据，"
            f"可以用 --output-ratio 改）；估算成本 {_yuan(estimated_cost)}"
        )
    if totals["unsplit_tokens"]:
        print(
            f"注意：其中 {_format_tokens(totals['unsplit_tokens'])} token 是早期数据"
            "（没有输入/输出拆分），成本是按上面这个比例估的。"
        )

    if rows:
        print()
        print("== 按账号 ==")
        for row in sorted(rows, key=lambda item: item["total_tokens"], reverse=True):
            cost = price.cost_cents(row["prompt_tokens"], row["completion_tokens"])
            print(
                f"  {row['name']:<14}{row['plan']:<8}"
                f"{_format_tokens(row['total_tokens']):>8} token{_yuan(cost):>10}"
            )

    print()
    print("== 套餐毛利（按「用户天天跑满」算的月成本）==")
    if not ratio_is_observed:
        print(f"  「估算」两列用的是假设的输出占比 {ratio:.1%}；「最坏」两列假设全是输出。")
    print(f"  {'套餐':<6}{'售价':>8}{'月额度':>10}{'估算成本':>10}{'估算毛利':>10}{'最坏成本':>10}{'最坏毛利':>10}")
    for item in plan_margins(price, output_ratio=ratio):
        estimated = (
            f"{_yuan(item.estimated_cost_cents):>10}{_yuan(item.estimated_margin_cents):>10}"
            if item.estimated_cost_cents is not None
            else f"{'—':>10}{'—':>10}"
        )
        print(
            f"  {item.title:<6}{_yuan(item.price_cents):>8}"
            f"{_format_tokens(item.monthly_tokens):>10}{estimated}"
            f"{_yuan(item.worst_case_cost_cents):>10}{_yuan(item.worst_case_margin_cents):>10}"
        )
    return 0


def _yuan(cents: int | None) -> str:
    """分 → ￥ 显示。金额内部一律整数分，只在显示时除。

    用全角「￥」（U+FFE5）而不是半角「¥」（U+00A5）：后者在 Windows 的 GBK
    控制台下编不出来，把输出重定向到文件时会直接 UnicodeEncodeError 崩掉。
    """
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    return f"{sign}￥{abs(int(cents)) / 100:.2f}"


def _format_tokens(value: int | None) -> str:
    number = int(value or 0)
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if number >= 1000:
        return f"{number / 1000:.1f}k"
    return str(number)


def _testgen_command(args: argparse.Namespace) -> int:
    """执行 test-gen 子命令：给一批源码文件补 pytest 测试。

    退出码是一道门禁：**0 = 全都成（写好了或者已存在跳过）；1 = 有文件没搞定**。
    这样放进 CI 里就能拦住"生成了但跑不通"的情况。
    """
    from agentcode.testgen import discover_targets, generate_for

    settings = _load_settings(args)
    targets = discover_targets(args.path, limit=args.limit)
    if not targets:
        print(f"没有找到需要生成测试的 Python 文件：{args.path}")
        return 0

    if args.dry_run:
        print(f"将为这 {len(targets)} 个文件生成测试（--dry-run，没有真的跑）：")
        for target in targets:
            print(f"  {target}")
        return 0

    if args.llm != "mock":
        settings.validate()

    workspace = Path(settings.code_root)
    workspace.mkdir(parents=True, exist_ok=True)
    runner = _make_testgen_runner(settings, args)

    results = []
    for target in targets:
        if not args.json:
            print(f"→ 正在为 {target.name} 生成测试…")
        result = generate_for(
            target,
            workspace=workspace,
            run_agent=runner,
            out_dir=args.out,
            force=args.force,
        )
        results.append(result)
        if not args.json:
            # 用 GBK 里有的符号：✓/✗ 在 Windows 控制台重定向时会崩
            icon = {"written": "√", "skipped": "–", "failed": "×"}[result.status]
            detail = f"（{result.reason}）" if result.reason else ""
            print(f"  {icon} {result.dest}{detail}")

    failed = [item for item in results if item.status == "failed"]
    if args.json:
        print(
            json.dumps(
                {
                    "results": [item.to_dict() for item in results],
                    "written": sum(1 for i in results if i.status == "written"),
                    "skipped": sum(1 for i in results if i.status == "skipped"),
                    "failed": len(failed),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(_THIN_SEPARATOR)
        print(
            f"写好 {sum(1 for i in results if i.status == 'written')} 个，"
            f"跳过 {sum(1 for i in results if i.status == 'skipped')} 个，"
            f"失败 {len(failed)} 个"
        )
    return 1 if failed else 0


def _make_testgen_runner(settings: Settings, args: argparse.Namespace):
    """造一个"给个任务、跑一遍 test_gen"的函数。

    每个文件都用**全新的 agent 实例**——上一个文件的上下文不该影响下一个。
    """

    def run_agent(task: str, workspace: Path) -> Any:
        if args.llm == "mock":
            llm = demo_responses_llm("test_gen")
            tools = _build_tools(mock=True, settings=settings, agent_name="test_gen")
        else:
            llm = OpenAICompatibleLLM.from_settings(settings)
            tools = _build_tools(
                mock=False, settings=settings, agent_name="test_gen", allow_code=True
            )
        agent = default_registry.create(
            "test_gen",
            llm=llm,
            tools=tools,
            middlewares=_build_middlewares(settings, quiet=True),
            max_steps=settings.max_steps_for("test_gen"),
            memory=ShortTermMemory(max_turns=settings.memory_turns),
        )
        return agent.run(task)

    return run_agent


def _web_command(args: argparse.Namespace) -> int:
    """执行 web 子命令：启动本地可视化页面。"""
    from agentcode.web.server import serve

    settings = _load_settings(args)
    host, port, llm_mode = _web_target(args, settings)
    serve(
        host=host,
        port=port,
        llm_mode=llm_mode,
        env_file=args.env_file,
        open_browser=getattr(args, "open", False),
        quiet=not args.verbose,
        max_sessions=settings.max_sessions,
        session_dir=settings.web_session_dir,
        require_auth=not getattr(args, "no_auth", False),
    )
    return 0


def _open_command(args: argparse.Namespace) -> int:
    """执行 open 子命令：服务没起就顺手起来，然后打开浏览器。"""
    from agentcode.web.server import is_port_open, serve

    settings = _load_settings(args)
    host, port, llm_mode = _web_target(args, settings)
    url = f"http://{host}:{port}/"

    if is_port_open(host, port):
        print(f"服务已经在运行：{url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    print(f"页面入口：{url}")
    serve(
        host=host,
        port=port,
        llm_mode=llm_mode,
        env_file=args.env_file,
        open_browser=not args.no_browser,
        quiet=not args.verbose,
        max_sessions=settings.max_sessions,
        session_dir=settings.web_session_dir,
        require_auth=not getattr(args, "no_auth", False),
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """命令行主函数，返回进程退出码。"""
    _use_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "list":
            settings = _load_settings(args)
            print("可用智能体：")
            print(default_registry.describe())
            print("\n可用工具：")
            print(_build_tools(mock=False, settings=settings, create_root=False).describe())
            return 0

        if args.command == "config":
            settings = _load_settings(args)
            print("当前配置（密钥已脱敏）：")
            for key, value in settings.masked().items():
                print(f"- {key}: {value}")
            if settings.missing_keys():
                print("提示：缺少必需项，使用真实模型前请先补齐 .env。")
            return 0

        if args.command == "web":
            return _web_command(args)

        if args.command == "open":
            return _open_command(args)

        if args.command == "user":
            return _user_command(args)

        if args.command == "billing":
            return _billing_command(args)

        if args.command == "backup":
            return _backup_command(args)

        if args.command == "audit":
            return _audit_command(args)

        if args.command == "trash":
            return _trash_command(args)

        if args.command == "notify":
            return _notify_command(args)

        if args.command == "token":
            return _token_command(args)

        if args.command == "api":
            return _api_command(args)

        if args.command == "test-gen":
            return _testgen_command(args)

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
