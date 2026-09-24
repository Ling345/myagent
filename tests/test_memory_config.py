"""记忆轮数与网页会话上限的可配置性测试。"""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from agentcode.cli import main
from agentcode.config import Settings
from agentcode.core.errors import ConfigError
from agentcode.memory import ShortTermMemory
from agentcode.web.runner import create_backend
from agentcode.web.server import create_server


def test_settings_default_memory_values():
    settings = Settings()
    assert settings.memory_turns == 5
    assert settings.max_sessions == 20


def test_config_defaults_match_component_defaults():
    """配置里的默认值必须与各组件自身的默认值一致，避免悄悄漂移。"""
    from agentcode.core.agent import DEFAULT_MEMORY_TURNS as agent_default
    from agentcode.memory.session_store import DEFAULT_MAX_TURNS as store_default
    from agentcode.web.sessions import DEFAULT_SESSION_DIR as web_dir_default
    from agentcode.web.sessions import DEFAULT_MAX_SESSIONS as session_default

    assert Settings().memory_turns == agent_default
    assert Settings().memory_turns == store_default
    assert Settings().max_sessions == session_default
    assert Settings().web_session_dir == web_dir_default


def test_env_overrides_memory_values(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MEMORY_TURNS", "12")
    monkeypatch.setenv("AGENT_MAX_SESSIONS", "3")
    settings = Settings.from_env(env_file=str(tmp_path / "missing.env"), search_parents=False)
    assert settings.memory_turns == 12
    assert settings.max_sessions == 3


def test_invalid_memory_turns_falls_back_to_default(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_MEMORY_TURNS", "不是数字")
    settings = Settings.from_env(env_file=str(tmp_path / "missing.env"), search_parents=False)
    assert settings.memory_turns == 5


def test_validate_rejects_non_positive_memory_turns():
    with pytest.raises(ConfigError):
        Settings(memory_turns=0).validate()
    with pytest.raises(ConfigError):
        Settings(max_sessions=0).validate()


def test_masked_reports_memory_values(settings):
    masked = settings.apply_overrides({"memory_turns": 8}).masked()
    assert masked["AGENT_MEMORY_TURNS"] == "8"
    assert masked["AGENT_MAX_SESSIONS"] == "20"


def test_memory_counts_total_turns_beyond_limit():
    memory = ShortTermMemory(max_turns=2)
    for index in range(4):
        memory.add_turn(
            [
                {"role": "user", "content": f"第 {index} 轮"},
                {"role": "assistant", "content": "好"},
            ]
        )
    assert len(memory) == 2
    assert memory.total_turns == 4

    memory.clear()
    assert memory.total_turns == 0


def test_web_backend_uses_configured_memory_turns(settings):
    configured = settings.apply_overrides({"memory_turns": 2})
    agent = create_backend("react", "mock", configured)
    assert agent.memory.max_turns == 2


def test_cli_session_respects_configured_memory_turns(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_MEMORY_TURNS", "1")
    session_dir = tmp_path / "sessions"
    for index in range(3):
        code = main(
            [
                "run",
                "--agent",
                "echo",
                "--llm",
                "mock",
                "--task",
                f"第 {index} 轮",
                "--session",
                "ring",
                "--session-dir",
                str(session_dir),
            ]
        )
        assert code == 0
    capsys.readouterr()

    payload = json.loads((session_dir / "ring.json").read_text(encoding="utf-8"))
    assert len(payload["turns"]) == 1
    assert payload["turns"][0][0]["content"] == "第 2 轮"


def test_cli_memory_turns_flag_overrides_env(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_MEMORY_TURNS", "5")
    session_dir = tmp_path / "sessions"
    for index in range(3):
        code = main(
            [
                "run",
                "--agent",
                "echo",
                "--llm",
                "mock",
                "--task",
                f"第 {index} 轮",
                "--memory-turns",
                "2",
                "--session",
                "flag",
                "--session-dir",
                str(session_dir),
            ]
        )
        assert code == 0
    capsys.readouterr()

    payload = json.loads((session_dir / "flag.json").read_text(encoding="utf-8"))
    assert len(payload["turns"]) == 2


def test_web_server_respects_configured_session_limit():
    server = create_server(
        host="127.0.0.1", port=0, llm_mode="mock", quiet=True, max_sessions=1, require_auth=False
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}/api/run"
        for session in ("a", "b"):
            body = json.dumps(
                {"agent": "echo", "llm": "mock", "task": "你好", "session_id": session}
            ).encode("utf-8")
            request = urllib.request.Request(
                base_url, data=body, headers={"Content-Type": "application/json"}, method="POST"
            )
            urllib.request.urlopen(request, timeout=30).read()

        assert server.sessions.session_ids() == ["b"]  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        server.server_close()


def test_web_turn_index_survives_memory_trimming(monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_TURNS", "2")
    server = create_server(
        host="127.0.0.1", port=0, llm_mode="mock", quiet=True, require_auth=False
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/api/run"
        body = json.dumps(
            {"agent": "echo", "llm": "mock", "task": "你好", "session_id": "trim"}
        ).encode("utf-8")
        payload = {}
        for _ in range(3):
            request = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}, method="POST"
            )
            text = urllib.request.urlopen(request, timeout=30).read().decode("utf-8")
            for block in text.split("\n\n"):
                if "event: answer" in block:
                    line = [l for l in block.split("\n") if l.startswith("data:")][-1]
                    payload = json.loads(line[5:].strip())

        assert payload["turn_index"] == 3  # 真实轮次
        assert payload["memory_turns"] == 2  # 但只留 2 轮
        assert payload["memory_limit"] == 2
    finally:
        server.shutdown()
        server.server_close()
