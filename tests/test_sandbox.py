"""代码执行后端测试（全部离线，不需要真的装 Docker）。"""

from __future__ import annotations

import pytest

from agentcode.tools import ToolRegistry
from agentcode.tools.code import register_code_tools
from agentcode.tools.sandbox import (
    DockerBackend,
    ExecutionError,
    LocalBackend,
    describe_backend,
    make_backend,
    split_command,
    translate_mount_path,
)

#: 一个必然不存在的 docker 命令，用来模拟"没装 Docker"
MISSING_DOCKER = "definitely-not-a-docker-binary"


def _docker(root, **kwargs) -> DockerBackend:
    """构造一个指向不存在命令的 Docker 后端，便于离线测试。"""
    kwargs.setdefault("docker_executable", MISSING_DOCKER)
    return DockerBackend(root, "python:3.13-slim", **kwargs)


# ------------------------------------------------------------------ 命令拼装


def test_build_command_contains_all_hardening_flags(tmp_path):
    command = _docker(tmp_path).build_command("agentcode-test1234")
    joined = " ".join(command)

    assert "--rm" in command
    assert "--network none" in joined
    assert "--read-only" in command
    assert "/tmp:size=32m" in joined
    assert "--memory 256m" in joined
    assert "--cpus 0.5" in joined
    assert "--pids-limit 64" in joined
    assert "--user 65534:65534" in joined
    assert "--name agentcode-test1234" in joined


def test_build_command_mounts_only_workdir_writable(tmp_path):
    command = _docker(tmp_path).build_command("c")
    mounts = [command[i + 1] for i, item in enumerate(command) if item == "-v"]
    assert len(mounts) == 1
    assert mounts[0].endswith(":/workspace:rw")
    assert str(tmp_path) in mounts[0]


def test_build_command_reads_code_from_stdin(tmp_path):
    """代码走 stdin 而不是命令行，避免超长参数与转义问题。"""
    command = _docker(tmp_path).build_command("c")
    assert command[-2:] == ["python", "-"]
    assert "-i" in command
    assert command[-3].endswith("python:3.13-slim")


def test_build_command_omits_user_when_blank(tmp_path):
    joined = " ".join(_docker(tmp_path, container_user="").build_command("c"))
    assert "--user" not in joined


def test_container_user_is_configurable(tmp_path):
    joined = " ".join(_docker(tmp_path, container_user="1000:1000").build_command("c"))
    assert "--user 1000:1000" in joined


# ------------------------------------------------------------- 命令前缀与路径


def test_split_command_accepts_wsl_prefix(tmp_path):
    assert split_command("wsl -d Ubuntu -- docker") == ["wsl", "-d", "Ubuntu", "--", "docker"]


def test_split_command_falls_back_to_default():
    assert split_command("   ") == ["docker"]


def test_translate_mount_path_keeps_windows_path_for_plain_docker(tmp_path):
    prefix = split_command("docker")
    assert translate_mount_path(prefix, tmp_path) == str(tmp_path)


def test_translate_mount_path_converts_for_wsl(tmp_path):
    prefix = split_command("wsl -d Ubuntu -- docker")
    converted = translate_mount_path(prefix, tmp_path)
    assert converted.startswith("/mnt/")
    assert "\\" not in converted


# ------------------------------------------------------------------ 后端选择


def test_make_backend_local_by_default(tmp_path):
    assert isinstance(make_backend("local", tmp_path), LocalBackend)


def test_make_backend_docker(tmp_path):
    assert isinstance(make_backend("docker", tmp_path), DockerBackend)


def test_make_backend_rejects_unknown_kind(tmp_path):
    with pytest.raises(ExecutionError, match="未知的执行后端"):
        make_backend("vm", tmp_path)


def test_describe_backend_mentions_isolation(tmp_path):
    assert "容器隔离执行" in describe_backend(make_backend("docker", tmp_path))
    assert "非安全沙箱" in describe_backend(make_backend("local", tmp_path))


# ------------------------------------------------------------------ 可用性


def test_available_reports_missing_docker_command(tmp_path):
    ok, detail = _docker(tmp_path).available()
    assert ok is False
    assert detail


def test_run_raises_when_docker_missing(tmp_path):
    """选 docker 但环境不可用时必须报错，绝不悄悄退回本机执行。"""
    with pytest.raises(ExecutionError, match="Docker 不可用"):
        _docker(tmp_path).run("print('不该被执行')", 5)


def test_local_backend_reports_timeout(tmp_path):
    result = LocalBackend(tmp_path).run("while True: pass", 0.5)
    assert result.timed_out is True
    assert result.returncode != 0
    assert result.ok is False


# ------------------------------------------------------- 与工具注册的接线


def test_register_code_tools_accepts_docker_backend(tmp_path):
    registry = ToolRegistry()
    register_code_tools(
        registry,
        root=tmp_path,
        execution_backend="docker",
        docker_binary=MISSING_DOCKER,
    )
    output = registry.invoke("run_python", "print(1)")
    assert output.startswith("错误：")
    assert "Docker 不可用" in output


def test_register_code_tools_rejects_unknown_backend(tmp_path):
    with pytest.raises(ExecutionError, match="未知的执行后端"):
        register_code_tools(ToolRegistry(), root=tmp_path, execution_backend="vm")


def test_run_python_description_mentions_backend(tmp_path):
    registry = ToolRegistry()
    register_code_tools(registry, root=tmp_path, execution_backend="docker")
    spec = registry.get("run_python")
    assert spec is not None
    assert "容器隔离执行" in spec.description
