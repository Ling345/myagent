"""代码执行后端：本机直跑 与 容器隔离。

``run_python`` 通过这里执行代码，切换后端只改配置：

- ``local``：默认值。限制工作目录、超时、剥离密钥环境变量——防误伤，**不是**安全沙箱。
- ``docker``：一次性容器执行。断网、只读根文件系统、只有工作目录可写、CPU/内存/进程数受限、
  以非 root 用户运行、用完即删。

设计原则：选择 docker 但环境不可用时**明确报错**，绝不悄悄退回本机执行——
否则"以为隔离了其实没隔离"比不隔离更危险。
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

DEFAULT_DOCKER_IMAGE = "python:3.13-slim"
DEFAULT_DOCKER_BINARY = "docker"
DEFAULT_MEMORY = "256m"
DEFAULT_CPUS = "0.5"
DEFAULT_PIDS_LIMIT = 64
#: 容器内以非 root 身份跑代码（nobody:nogroup）
DEFAULT_CONTAINER_USER = "65534:65534"
DEFAULT_CONTAINER_WORKDIR = "/workspace"

#: 允许传给子进程的环境变量（白名单，绝不包含密钥）
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


@dataclass
class ExecutionResult:
    """一次代码执行的结果。"""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    backend: str = "local"

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class ExecutionError(RuntimeError):
    """后端本身不可用（例如 Docker 没装或没启动）。"""


def child_env(root: Path) -> dict[str, str]:
    """本机执行时传给子进程的环境变量。"""
    env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def split_command(executable: str) -> list[str]:
    """把配置里的命令拆成参数表。

    既支持 ``docker`` 这种单命令，也支持 ``wsl -d Ubuntu -- docker``
    这种"先进入 WSL 再调 docker"的前缀写法。
    """
    parts = [part for part in str(executable or "").strip().split() if part]
    return parts or [DEFAULT_DOCKER_BINARY]


def translate_mount_path(prefix: Sequence[str], path: Path) -> str:
    """把 Windows 路径翻译成挂载源路径。

    Docker Desktop 能直接吃 ``D:\\a\\b``；但走 ``wsl docker`` 时必须换成
    ``/mnt/d/a/b``，否则 WSL 里的 docker 找不到这个目录。
    """
    text = str(path)
    if not any(Path(part).name.lower().startswith("wsl") for part in prefix):
        return text
    drive, _, rest = text.partition(":")
    if len(drive) == 1 and rest:
        return "/mnt/{}/{}".format(drive.lower(), rest.replace("\\", "/").lstrip("/"))
    return text.replace("\\", "/")


class LocalBackend:
    """在本机子进程里执行（原有行为，非安全沙箱）。"""

    name = "local"

    def __init__(self, root: Path, python_executable: str | None = None) -> None:
        self.root = Path(root).resolve()
        self.python_executable = python_executable or sys.executable

    def available(self) -> tuple[bool, str]:
        """本机后端总是可用；解释器不存在时在 :meth:`run` 里报错。"""
        return True, self.python_executable

    def run(self, code: str, timeout: float) -> ExecutionResult:
        """执行代码并返回结果。"""
        try:
            completed = subprocess.run(
                [self.python_executable, "-c", str(code)],
                cwd=str(self.root),
                env=child_env(self.root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return ExecutionResult(124, "", "", timed_out=True, backend=self.name)
        except OSError as exc:
            raise ExecutionError(f"无法启动 Python 解释器（{exc}）") from exc
        return ExecutionResult(
            completed.returncode, completed.stdout or "", completed.stderr or "", backend=self.name
        )


class DockerBackend:
    """在一次性容器里执行。

    硬化参数：
    ``--network none`` 断网、``--read-only`` 根文件系统只读、只把工作目录挂成可写、
    ``--memory`` / ``--cpus`` / ``--pids-limit`` 限制资源（防内存爆炸与 fork 炸弹）、
    ``--user`` 非 root、``--rm`` 用完即删。

    代码用 stdin 喂给 ``python -``，避免超长命令行与转义问题。
    """

    name = "docker"

    def __init__(
        self,
        root: Path,
        image: str = DEFAULT_DOCKER_IMAGE,
        *,
        memory: str = DEFAULT_MEMORY,
        cpus: str = DEFAULT_CPUS,
        pids_limit: int = DEFAULT_PIDS_LIMIT,
        docker_executable: str = DEFAULT_DOCKER_BINARY,
        container_user: str = DEFAULT_CONTAINER_USER,
        workdir: str = DEFAULT_CONTAINER_WORKDIR,
    ) -> None:
        self.root = Path(root).resolve()
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = int(pids_limit)
        self.docker_prefix = split_command(docker_executable)
        self.container_user = (container_user or "").strip()
        self.workdir = workdir

    @property
    def docker_executable(self) -> str:
        """兼容旧名字：只用于展示。"""
        return " ".join(self.docker_prefix)

    def build_command(self, container_name: str) -> list[str]:
        """拼出 ``docker run`` 命令（单独抽出来便于测试与审查）。"""
        mount = translate_mount_path(self.docker_prefix, self.root)
        command = [
            *self.docker_prefix,
            "run",
            "--rm",
            "-i",
            "--name",
            container_name,
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:size=32m",
            "--memory",
            self.memory,
            "--cpus",
            self.cpus,
            "--pids-limit",
            str(self.pids_limit),
        ]
        if self.container_user:
            command += ["--user", self.container_user]
        command += [
            "--workdir",
            self.workdir,
            "-v",
            f"{mount}:{self.workdir}:rw",
            "-e",
            "PYTHONIOENCODING=utf-8",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-e",
            f"PYTHONPATH={self.workdir}",
            self.image,
            "python",
            "-",
        ]
        return command

    def available(self) -> tuple[bool, str]:
        """检查 docker 命令与守护进程是否可用。"""
        try:
            completed = subprocess.run(
                [*self.docker_prefix, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"无法执行 docker 命令（{exc}）"
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip().splitlines()
            return False, detail[0] if detail else "docker 守护进程未运行"
        return True, completed.stdout.strip()

    def run(self, code: str, timeout: float) -> ExecutionResult:
        """在容器里执行代码；超时则杀掉容器。"""
        ok, detail = self.available()
        if not ok:
            raise ExecutionError(
                f"Docker 不可用（{detail}）。请先启动 Docker，"
                "或把 AGENT_EXECUTION_BACKEND 改回 local。"
            )

        container_name = f"agentcode-{uuid.uuid4().hex[:8]}"
        command = self.build_command(container_name)
        try:
            completed = subprocess.run(
                command,
                input=str(code),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            # docker run 客户端被杀掉后容器可能还在跑，显式清理
            subprocess.run(
                [*self.docker_prefix, "kill", container_name],
                capture_output=True,
                timeout=30,
            )
            return ExecutionResult(124, "", "", timed_out=True, backend=self.name)
        except OSError as exc:
            raise ExecutionError(f"启动容器失败（{exc}）") from exc
        return ExecutionResult(
            completed.returncode, completed.stdout or "", completed.stderr or "", backend=self.name
        )


def make_backend(
    kind: str,
    root: Path,
    *,
    python_executable: str | None = None,
    docker_image: str = DEFAULT_DOCKER_IMAGE,
    docker_binary: str = DEFAULT_DOCKER_BINARY,
    docker_memory: str = DEFAULT_MEMORY,
    docker_cpus: str = DEFAULT_CPUS,
    docker_pids_limit: int = DEFAULT_PIDS_LIMIT,
    docker_user: str = DEFAULT_CONTAINER_USER,
) -> LocalBackend | DockerBackend:
    """按配置创建执行后端。"""
    normalized = (kind or "local").strip().lower()
    if normalized == "docker":
        return DockerBackend(
            root,
            docker_image,
            memory=docker_memory,
            cpus=docker_cpus,
            pids_limit=docker_pids_limit,
            docker_executable=docker_binary,
            container_user=docker_user,
        )
    if normalized != "local":
        raise ExecutionError(f"未知的执行后端：{kind}（可选 local / docker）")
    return LocalBackend(root, python_executable)


def describe_backend(backend: LocalBackend | DockerBackend) -> str:
    """给提示词用的一句话说明。"""
    if isinstance(backend, DockerBackend):
        return (
            f"容器隔离执行（镜像 {backend.image}，断网、只读根文件系统、"
            f"内存 {backend.memory}、CPU {backend.cpus}、进程数上限 {backend.pids_limit}）"
        )
    return "本机受限执行（限制目录与超时，非安全沙箱）"
