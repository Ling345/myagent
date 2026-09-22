"""受限代码执行与文件读写工具。

这不是安全沙箱：Python 子进程本身仍能访问文件系统与网络。
它的目的是"防误伤与跑飞"——固定在独立工作目录、限时、限输出、剥离密钥环境变量，
默认不碰项目源码。详见 docs/superpowers/specs/2026-09-22-coding-agent-design.md。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from agentcode.tools.base import ToolRegistry

DEFAULT_TIMEOUT = 10.0
DEFAULT_OUTPUT_LIMIT = 4000
DEFAULT_MAX_FILE_BYTES = 200 * 1024
MIN_TIMEOUT = 0.5
MAX_TIMEOUT = 60.0
MAX_LISTED_FILES = 100

#: 允许传给子进程的环境变量（白名单，绝不包含任何密钥）
ENV_ALLOWLIST = (
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "TEMP",
    "TMP",
    "COMSPEC",
    "PATHEXT",
    "NUMBER_OF_PROCESSORS",
    "OS",
)


def _child_env(root: Path) -> dict[str, str]:
    """构造子进程环境：白名单 + 让子进程能 import 根目录下的模块。"""
    env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _truncate(text: str, limit: int) -> str:
    """超长输出截断并标注，避免刷爆上下文。"""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…（已截断，原始长度 {len(text)} 字符）"


def resolve_in_root(root: Path, raw_path: str) -> Path:
    """把相对路径解析到根目录内；越界时抛 ``ValueError``。

    工具与网页的文件浏览接口共用这一份实现，避免两处规则不一致。
    """
    candidate = Path(str(raw_path or "").strip() or ".")
    target = (root / candidate).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"路径超出代码根目录：{raw_path}")
    return target


#: 兼容内部旧名字
_resolve_in_root = resolve_in_root


def register_code_tools(
    registry: ToolRegistry,
    root: str | Path,
    timeout: float = DEFAULT_TIMEOUT,
    output_limit: int = DEFAULT_OUTPUT_LIMIT,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    python_executable: str | None = None,
    create: bool = True,
) -> ToolRegistry:
    """把代码执行与文件工具注册进工具箱。

    ``timeout`` 是单次执行的默认时限（秒），每次调用可以用 ``timeout_seconds``
    小幅提高，但不会超过 ``MAX_TIMEOUT``；``create=False`` 时不会创建工作目录
    （只想列工具清单时用）。
    """
    code_root = Path(root).resolve()
    if create:
        code_root.mkdir(parents=True, exist_ok=True)
    executable = python_executable or sys.executable
    location = f"（工作目录：{code_root}）"

    def run_python(code: str, timeout_seconds: float | None = None) -> str:
        """在代码工作目录里执行一段 Python 代码。"""
        requested = float(timeout_seconds) if timeout_seconds else float(timeout)
        limit = max(MIN_TIMEOUT, min(requested, MAX_TIMEOUT))
        try:
            completed = subprocess.run(
                [executable, "-c", str(code)],
                cwd=str(code_root),
                env=_child_env(code_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=limit,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return f"错误：代码执行超时（超过 {limit:g} 秒），进程已终止。"
        except OSError as exc:
            return f"错误：无法启动 Python 解释器（{exc}）。"

        parts = [f"退出码：{completed.returncode}"]
        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if stdout:
            parts.append("--- stdout ---\n" + _truncate(stdout, output_limit))
        if stderr:
            parts.append("--- stderr ---\n" + _truncate(stderr, output_limit))
        if not stdout and not stderr:
            parts.append("（没有输出）")
        return "\n".join(parts)

    def read_file(path: str) -> str:
        """读取代码工作目录里的文件。"""
        try:
            target = _resolve_in_root(code_root, path)
        except ValueError as exc:
            return f"错误：{exc}"
        if not target.is_file():
            return f"错误：文件不存在：{path}"
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"错误：读取文件失败（{exc}）。"
        return _truncate(text, output_limit)

    def write_file(path: str, content: str = "") -> str:
        """把内容写入代码工作目录里的文件，目录会自动创建。"""
        text = str(content)
        size = len(text.encode("utf-8"))
        if size > max_file_bytes:
            return (
                f"错误：内容过大（{size} 字节，超过 {max_file_bytes // 1024} KB），"
                "请拆成多个文件或分段写入。"
            )
        try:
            target = _resolve_in_root(code_root, path)
        except ValueError as exc:
            return f"错误：{exc}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        except OSError as exc:
            return f"错误：写入文件失败（{exc}）。"
        return f"已写入 {path}（{len(text)} 字符）"

    def list_files(path: str = ".") -> str:
        """列出代码工作目录里的文件。"""
        try:
            target = _resolve_in_root(code_root, path)
        except ValueError as exc:
            return f"错误：{exc}"
        if not target.is_dir():
            return f"错误：目录不存在：{path}"
        files = sorted(
            candidate.relative_to(code_root).as_posix()
            for candidate in target.rglob("*")
            if candidate.is_file()
        )
        if not files:
            return "（这个目录下没有文件）"
        shown = files[:MAX_LISTED_FILES]
        suffix = (
            f"\n…（共 {len(files)} 个文件，仅列出前 {MAX_LISTED_FILES} 个）"
            if len(files) > MAX_LISTED_FILES
            else ""
        )
        return "\n".join(shown) + suffix

    registry.register_tool(
        "run_python",
        "在代码工作目录执行一段 Python 代码，返回退出码与 stdout/stderr。改完代码必须用它跑一遍。",
        run_python,
        {"code": "要执行的 Python 代码", "timeout_seconds": "可选，超时秒数（上限 60）"},
    )
    registry.register_tool(
        "read_file",
        "读取代码工作目录里的文件，内容过长会截断（目录见 write_file 的说明）。",
        read_file,
        {"path": "相对代码工作目录的文件路径"},
    )
    registry.register_tool(
        "write_file",
        f"把内容写入代码工作目录里的文件，父目录会自动创建。{location}",
        write_file,
        {"path": "相对代码工作目录的文件路径", "content": "文件内容"},
    )
    registry.register_tool(
        "list_files",
        "列出代码工作目录里的文件，用于确认当前有哪些文件（目录见 write_file 的说明）。",
        list_files,
        {"path": "可选，相对路径的目录，默认根目录"},
    )
    return registry
