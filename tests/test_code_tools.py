"""受限代码执行与文件工具测试（全部离线）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentcode.tools import ToolRegistry
from agentcode.tools.code import register_code_tools


@pytest.fixture
def sandbox(tmp_path):
    """一个以临时目录为根的工具集。"""
    registry = ToolRegistry()
    register_code_tools(registry, root=tmp_path, timeout=5, output_limit=200)
    return registry, tmp_path


# ---------------------------------------------------------------- 代码执行


def test_run_python_returns_stdout(sandbox):
    registry, _ = sandbox
    output = registry.invoke("run_python", "print(1 + 1)")
    assert "退出码：0" in output
    assert "2" in output


def test_run_python_reports_traceback(sandbox):
    registry, _ = sandbox
    output = registry.invoke("run_python", "raise ValueError('故意失败')")
    assert "退出码：1" in output
    assert "ValueError" in output
    assert "故意失败" in output


def test_run_python_runs_inside_code_root(sandbox):
    registry, root = sandbox
    output = registry.invoke("run_python", "import os; print(os.getcwd())")
    # 拿最后一行（stdout 里的 cwd）比较，避免 Windows/Linux 分隔符差异
    printed = output.strip().splitlines()[-1]
    assert Path(printed).resolve() == root.resolve()


def test_run_python_hides_secrets_from_child(monkeypatch, sandbox):
    monkeypatch.setenv("LLM_API_KEY", "sk-should-not-leak")
    registry, _ = sandbox
    output = registry.invoke("run_python", "import os; print('KEY=', os.environ.get('LLM_API_KEY'))")
    assert "sk-should-not-leak" not in output
    assert "KEY= None" in output


def test_run_python_times_out(sandbox):
    registry, _ = sandbox
    output = registry.invoke("run_python", "while True: pass")
    assert "超时" in output


def test_run_python_truncates_long_output(sandbox):
    registry, _ = sandbox
    output = registry.invoke("run_python", "print('x' * 5000)")
    assert "已截断" in output
    assert len(output) < 2000


def test_run_python_reports_interpreter_start_failure(monkeypatch, sandbox):
    registry, _ = sandbox
    registry2 = ToolRegistry()
    register_code_tools(registry2, root=sandbox[1], python_executable="definitely-not-a-python")
    output = registry2.invoke("run_python", "print(1)")
    assert "无法启动" in output


def test_run_python_accepts_keyword_form_via_registry(sandbox):
    registry, _ = sandbox
    output = registry.invoke("run_python", 'code="print(\'关键字形式\')"')
    assert "关键字形式" in output


# ------------------------------------------------------------------ 文件


def test_write_then_read_file_roundtrip(sandbox):
    registry, _ = sandbox
    written = registry.invoke("write_file", 'path="pkg/mod.py", content="VALUE = 42\\n"')
    assert "已写入" in written
    content = registry.invoke("read_file", "pkg/mod.py")
    assert "VALUE = 42" in content


def test_write_file_creates_nested_directories(sandbox):
    registry, root = sandbox
    registry.invoke("write_file", 'path="a/b/c.txt", content="x"')
    assert (root / "a" / "b" / "c.txt").read_text(encoding="utf-8") == "x"


def test_read_file_reports_missing_file(sandbox):
    registry, _ = sandbox
    assert "不存在" in registry.invoke("read_file", "nope.txt")


def test_write_file_rejects_parent_escape(sandbox):
    registry, root = sandbox
    output = registry.invoke("write_file", 'path="../escape.txt", content="x"')
    assert "超出代码根目录" in output
    assert not (root.parent / "escape.txt").exists()


def test_write_file_rejects_absolute_path_outside_root(sandbox, tmp_path):
    registry, _ = sandbox
    outside = tmp_path.parent / "outside.txt"
    output = registry.invoke("write_file", f'path="{outside}", content="x"')
    assert "超出代码根目录" in output
    assert not outside.exists()


def test_read_file_rejects_escape(sandbox):
    registry, _ = sandbox
    assert "超出代码根目录" in registry.invoke("read_file", "../../etc/passwd")


def test_write_file_rejects_oversized_content(sandbox):
    registry, _ = sandbox
    big = "x" * (300 * 1024)
    output = registry.invoke("write_file", f'path="big.txt", content="{big}"')
    assert "过大" in output


def test_list_files_shows_created_files(sandbox):
    registry, _ = sandbox
    registry.invoke("write_file", 'path="one.py", content="x"')
    listing = registry.invoke("list_files", ".")
    assert "one.py" in listing


def test_list_files_reports_empty_directory(sandbox):
    registry, _ = sandbox
    assert "没有文件" in registry.invoke("list_files", ".")


def test_register_code_tools_adds_expected_names(sandbox):
    registry, _ = sandbox
    assert set(registry.names()) == {"run_python", "read_file", "write_file", "list_files"}


def test_child_process_uses_same_interpreter(sandbox):
    registry, _ = sandbox
    output = registry.invoke("run_python", "import sys; print(sys.version_info.major)")
    assert sys.version_info.major == 3
    assert "3" in output
