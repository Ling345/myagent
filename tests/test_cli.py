"""命令行入口测试。"""

from __future__ import annotations

import json

import pytest

from agentcode.cli import main
from agentcode.memory import JsonStore


def test_cli_list_shows_builtin_agents(capsys):
    assert main(["list"]) == 0
    output = capsys.readouterr().out
    assert "react" in output
    assert "plan_and_solve" in output
    assert "web_search" in output


def test_cli_run_with_mock_llm_prints_answer(capsys):
    code = main(["run", "--agent", "react", "--llm", "mock", "--task", "你好"])
    output = capsys.readouterr().out
    assert code == 0
    assert "最终答案" in output
    assert "get_weather" in output


def test_cli_run_writes_trace_file(tmp_path, capsys):
    trace = tmp_path / "trace.json"
    code = main(
        ["run", "--agent", "react", "--llm", "mock", "--task", "你好", "--trace", str(trace)]
    )
    capsys.readouterr()
    assert code == 0
    payload = JsonStore.load(trace)
    assert payload["agent"] == "react"
    assert payload["success"] is True
    assert len(payload["steps"]) >= 2


def test_cli_json_output_is_machine_readable(capsys):
    code = main(["run", "--agent", "echo", "--llm", "mock", "--task", "你好", "--json"])
    output = capsys.readouterr().out
    assert code == 0
    payload = json.loads(output[output.index("{") :])
    assert payload["answer"] == "已收到任务：你好"


def test_cli_unknown_agent_returns_two(capsys):
    code = main(["run", "--agent", "不存在", "--llm", "mock", "--task", "你好"])
    output = capsys.readouterr().out
    assert code == 2
    assert "未找到名为" in output


def test_cli_real_llm_without_config_returns_two(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    missing_env = tmp_path / "missing.env"
    code = main(
        [
            "run",
            "--agent",
            "react",
            "--task",
            "你好",
            "--env-file",
            str(missing_env),
        ]
    )
    output = capsys.readouterr().out
    assert code == 2
    assert "配置错误" in output


def test_cli_config_command_masks_secrets(monkeypatch, tmp_path, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_API_KEY=sk-1234567890abcdef\n"
        "LLM_BASE_URL=https://example.invalid/v1\n"
        "LLM_MODEL_ID=demo-model\n",
        encoding="utf-8",
    )
    code = main(["config", "--env-file", str(env_file)])
    output = capsys.readouterr().out
    assert code == 0
    assert "demo-model" in output
    assert "sk-1234567890abcdef" not in output


def test_cli_config_file_sets_default_agent(tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"agent": "echo", "max_steps": 2}), encoding="utf-8")
    code = main(["run", "--llm", "mock", "--task", "你好", "--config", str(config), "--json"])
    output = capsys.readouterr().out
    assert code == 0
    payload = json.loads(output[output.index("{") :])
    assert payload["agent"] == "echo"


def test_cli_requires_task(capsys):
    code = main(["run", "--agent", "echo", "--llm", "mock"])
    assert code == 2
    assert "任务不能为空" in capsys.readouterr().out
