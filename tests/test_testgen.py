"""批量测试生成：挑文件、跑 agent、把测试写回源文件旁边（离线）。

这是产品收窄后的主打场景——不是"什么都能聊的 agent"，
而是"给你没测试的 Python 代码自动补测试"。
"""

from __future__ import annotations

from pathlib import Path

from agentcode.testgen import (
    GenerationResult,
    default_test_path,
    discover_targets,
    generate_for,
    is_test_file,
)


def _write(path: Path, text: str = "def f():\n    return 1\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- 挑文件


def test_is_test_file_recognises_the_usual_shapes():
    assert is_test_file(Path("test_calc.py")) is True
    assert is_test_file(Path("calc_test.py")) is True
    assert is_test_file(Path("conftest.py")) is True
    assert is_test_file(Path("tests/test_x.py")) is True
    assert is_test_file(Path("calc.py")) is False


def test_discover_targets_finds_python_files_in_a_directory(tmp_path):
    _write(tmp_path / "a.py")
    _write(tmp_path / "pkg" / "b.py")
    targets = discover_targets(tmp_path)
    assert [p.name for p in targets] == ["a.py", "b.py"]


def test_discover_targets_skips_tests_and_noise(tmp_path):
    _write(tmp_path / "calc.py")
    _write(tmp_path / "test_calc.py")
    _write(tmp_path / "conftest.py")
    _write(tmp_path / "calc_test.py")
    _write(tmp_path / "__pycache__" / "junk.py")
    _write(tmp_path / ".hidden" / "secret.py")
    _write(tmp_path / "notes.txt", "hi")

    targets = discover_targets(tmp_path)
    assert [p.name for p in targets] == ["calc.py"]


def test_discover_targets_accepts_a_single_file(tmp_path):
    source = _write(tmp_path / "calc.py")
    assert discover_targets(source) == [source.resolve()]


def test_discover_targets_on_a_test_file_yields_nothing(tmp_path):
    """你已经写好的测试不该被当成"待补测试的源码"。"""
    assert discover_targets(_write(tmp_path / "test_calc.py")) == []


def test_discover_targets_skips_scripts_that_are_obviously_entrypoints(tmp_path):
    _write(tmp_path / "calc.py")
    _write(tmp_path / "setup.py")
    targets = discover_targets(tmp_path)
    assert [p.name for p in targets] == ["calc.py"]


def test_discover_targets_respects_the_limit(tmp_path):
    for index in range(5):
        _write(tmp_path / f"m{index}.py")
    assert len(discover_targets(tmp_path, limit=2)) == 2


def test_discover_targets_sorts_for_stable_output(tmp_path):
    _write(tmp_path / "z.py")
    _write(tmp_path / "a.py")
    assert [p.name for p in discover_targets(tmp_path)] == ["a.py", "z.py"]


def test_discover_targets_on_a_missing_path_is_empty(tmp_path):
    assert discover_targets(tmp_path / "nope") == []


# ---------------------------------------------------------------- 目标路径


def test_default_test_path_sits_next_to_the_source(tmp_path):
    assert default_test_path(tmp_path / "calc.py") == tmp_path / "test_calc.py"


# ---------------------------------------------------------------- 跑一个文件


def _stub_agent(text: str = "def test_f():\n    assert True\n", *, filename: str | None = None):
    """假 agent：往工作目录里写一个测试文件。"""

    def run(task: str, workspace: Path):
        name = filename or "test_unknown.py"
        (workspace / name).write_text(text, encoding="utf-8")
        return type("R", (), {"success": True, "answer": "好了", "error": None})()

    return run


def test_generate_writes_the_test_next_to_the_source(tmp_path):
    source = _write(tmp_path / "src" / "calc.py")
    workspace = tmp_path / "work"
    workspace.mkdir()

    result = generate_for(
        source, workspace=workspace, run_agent=_stub_agent(filename="test_calc.py")
    )
    assert result.status == "written"
    assert result.dest == tmp_path / "src" / "test_calc.py"
    assert result.dest.is_file()
    assert "def test_f" in result.dest.read_text(encoding="utf-8")


def test_generate_copies_the_source_into_the_workspace(tmp_path):
    """agent 只能在自己的工作目录里干活，源文件得先送进去。"""
    source = _write(tmp_path / "calc.py", "VALUE = 42\n")
    workspace = tmp_path / "work"
    workspace.mkdir()
    seen: dict = {}

    def spy(task: str, work: Path):
        seen["task"] = task
        seen["exists"] = (work / "calc.py").is_file()
        (work / "test_calc.py").write_text("def test_x(): pass\n", encoding="utf-8")
        return type("R", (), {"success": True, "answer": "好了", "error": None})()

    generate_for(source, workspace=workspace, run_agent=spy)
    assert seen["exists"] is True
    assert "calc.py" in seen["task"]


def test_generate_skips_when_a_test_already_exists(tmp_path):
    """绝不静默覆盖用户已经写好的测试。"""
    source = _write(tmp_path / "calc.py")
    existing = _write(tmp_path / "test_calc.py", "# 我自己写的\n")
    workspace = tmp_path / "work"
    workspace.mkdir()

    result = generate_for(
        source, workspace=workspace, run_agent=_stub_agent(filename="test_calc.py")
    )
    assert result.status == "skipped"
    assert "force" in result.reason
    assert existing.read_text(encoding="utf-8") == "# 我自己写的\n"


def test_generate_overwrites_with_force(tmp_path):
    source = _write(tmp_path / "calc.py")
    _write(tmp_path / "test_calc.py", "# 旧的\n")
    workspace = tmp_path / "work"
    workspace.mkdir()

    result = generate_for(
        source, workspace=workspace, run_agent=_stub_agent(filename="test_calc.py"), force=True
    )
    assert result.status == "written"
    assert "def test_f" in (tmp_path / "test_calc.py").read_text(encoding="utf-8")


def test_generate_honours_an_output_directory(tmp_path):
    source = _write(tmp_path / "calc.py")
    workspace = tmp_path / "work"
    workspace.mkdir()
    out = tmp_path / "tests"

    result = generate_for(
        source, workspace=workspace, run_agent=_stub_agent(filename="test_calc.py"), out_dir=out
    )
    assert result.dest == out / "test_calc.py"
    assert result.dest.is_file()


def test_generate_reports_failure_when_the_agent_fails(tmp_path):
    source = _write(tmp_path / "calc.py")
    workspace = tmp_path / "work"
    workspace.mkdir()

    def failing(task: str, work: Path):
        return type("R", (), {"success": False, "answer": "", "error": "跑不通"})()

    result = generate_for(source, workspace=workspace, run_agent=failing)
    assert result.status == "failed"
    assert "跑不通" in result.reason
    assert not (tmp_path / "test_calc.py").exists()


def test_generate_reports_failure_when_no_test_file_was_written(tmp_path):
    """agent 说成功了，但工作目录里没有测试文件——这也算失败，
    不能报个"成功"让人白高兴。"""
    source = _write(tmp_path / "calc.py")
    workspace = tmp_path / "work"
    workspace.mkdir()

    def chatty(task: str, work: Path):
        return type("R", (), {"success": True, "answer": "我建议你可以这样写…", "error": None})()

    result = generate_for(source, workspace=workspace, run_agent=chatty)
    assert result.status == "failed"
    assert "没写出" in result.reason or "没有生成" in result.reason


def test_generate_falls_back_to_the_newest_test_file(tmp_path):
    """模型可能起个别的名字（比如 test_calc_extra.py），别因此白跑一趟。"""
    source = _write(tmp_path / "calc.py")
    workspace = tmp_path / "work"
    workspace.mkdir()

    result = generate_for(
        source, workspace=workspace, run_agent=_stub_agent(filename="test_calc_extra.py")
    )
    assert result.status == "written"
    assert result.dest.name == "test_calc.py"  # 落到规范名字上
    assert result.dest.is_file()


def test_generate_does_not_pick_up_a_stale_file_from_the_workspace(tmp_path):
    """工作目录里可能留着上一轮跑出来的 test_calc.py。

    回归：判断"哪个是刚写的"不能用 `st_mtime >= time.time()`——文件时间戳走的是
    粗粒度时钟，可能比同一瞬间的 time.time() 早几毫秒，刚写好的文件会被判成旧的。
    这个坑 Windows 上碰不到，一到 Linux 就现形（实测差 6 毫秒）。
    现在改成跑之前拍快照、跑完比差异。
    """
    source = _write(tmp_path / "calc.py")
    workspace = tmp_path / "work"
    workspace.mkdir()
    # 上一轮留下的、名字正好是约定的那个
    (workspace / "test_calc.py").write_text("# 上一轮的老文件\n", encoding="utf-8")

    result = generate_for(
        source, workspace=workspace, run_agent=_stub_agent(filename="test_calc_fresh.py")
    )
    assert result.status == "written"
    assert "def test_f" in result.dest.read_text(encoding="utf-8")  # 新写的那份


def test_generate_records_duration(tmp_path):
    source = _write(tmp_path / "calc.py")
    workspace = tmp_path / "work"
    workspace.mkdir()
    result = generate_for(source, workspace=workspace, run_agent=_stub_agent(filename="test_calc.py"))
    assert result.duration_ms >= 0


def test_generation_result_is_serialisable(tmp_path):
    result = GenerationResult(
        source=tmp_path / "calc.py", dest=tmp_path / "test_calc.py", status="written"
    )
    payload = result.to_dict()
    assert payload["status"] == "written"
    assert isinstance(payload["source"], str)


# ---------------------------------------------------------------- 命令行


def _calculator(tmp_path: Path) -> Path:
    """把示例源码拷进临时目录当被测文件。"""
    sample = Path(__file__).resolve().parent.parent / "examples" / "sample_code" / "calculator.py"
    target = tmp_path / "proj" / "calculator.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(sample.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def test_cli_dry_run_lists_targets_without_running(tmp_path, monkeypatch, capsys):
    from agentcode.cli import main

    _calculator(tmp_path)
    code = main(["test-gen", str(tmp_path / "proj"), "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "calculator.py" in out
    assert not (tmp_path / "proj" / "test_calculator.py").exists()


def test_cli_generates_a_test_next_to_the_source(tmp_path, monkeypatch, capsys):
    """端到端（离线）：mock 模型配套 calculator.py，走完整条链路。"""
    from agentcode.cli import main

    _calculator(tmp_path)
    monkeypatch.setenv("AGENT_CODE_ROOT", str(tmp_path / "sandbox"))
    code = main(["test-gen", str(tmp_path / "proj"), "--llm", "mock"])
    out = capsys.readouterr().out

    assert code == 0
    generated = tmp_path / "proj" / "test_calculator.py"
    assert generated.is_file()
    assert "def test_add" in generated.read_text(encoding="utf-8")
    assert "写好 1 个" in out


def test_cli_skips_an_existing_test_file(tmp_path, monkeypatch, capsys):
    from agentcode.cli import main

    _calculator(tmp_path)
    existing = tmp_path / "proj" / "test_calculator.py"
    existing.write_text("# 我自己写的测试\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_CODE_ROOT", str(tmp_path / "sandbox"))

    code = main(["test-gen", str(tmp_path / "proj"), "--llm", "mock"])
    assert code == 0  # 跳过不算失败
    assert existing.read_text(encoding="utf-8") == "# 我自己写的测试\n"


def test_cli_writes_to_the_out_directory(tmp_path, monkeypatch, capsys):
    from agentcode.cli import main

    _calculator(tmp_path)
    monkeypatch.setenv("AGENT_CODE_ROOT", str(tmp_path / "sandbox"))
    code = main(
        ["test-gen", str(tmp_path / "proj"), "--llm", "mock", "--out", str(tmp_path / "tests")]
    )
    assert code == 0
    assert (tmp_path / "tests" / "test_calculator.py").is_file()


def test_cli_returns_zero_when_there_is_nothing_to_do(tmp_path, capsys):
    from agentcode.cli import main

    code = main(["test-gen", str(tmp_path), "--dry-run"])
    assert code == 0
    assert "没有找到" in capsys.readouterr().out


def test_cli_json_output_is_machine_readable(tmp_path, monkeypatch, capsys):
    import json

    from agentcode.cli import main

    _calculator(tmp_path)
    monkeypatch.setenv("AGENT_CODE_ROOT", str(tmp_path / "sandbox"))
    code = main(["test-gen", str(tmp_path / "proj"), "--llm", "mock", "--json"])
    assert code == 0

    # --json 模式下只输出这一段 JSON，别的什么都不打
    payload = json.loads(capsys.readouterr().out)
    assert payload["written"] == 1
    assert payload["failed"] == 0
    assert payload["results"][0]["status"] == "written"
