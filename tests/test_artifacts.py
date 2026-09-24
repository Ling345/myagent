"""运行产物：快照对比、会话记录、文件浏览接口，以及工具集裁剪。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from agentcode.web.runner import build_tools, changed_files, snapshot_files
from agentcode.web.server import create_server
from agentcode.web.sessions import SessionStore


# ------------------------------------------------------------ 快照与对比


def test_snapshot_lists_files_with_size(tmp_path):
    # 用 write_bytes 写入，避免 Windows 把 \n 翻成 \r\n 影响字节数断言
    (tmp_path / "a.py").write_bytes(b"print(1)\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_bytes(b"x = 1\n")

    snapshot = snapshot_files(tmp_path)
    assert set(snapshot) == {"a.py", "sub/b.py"}
    assert snapshot["a.py"][1] == 9


def test_snapshot_of_missing_directory_is_empty(tmp_path):
    assert snapshot_files(tmp_path / "不存在") == {}


def test_changed_files_reports_new_and_modified(tmp_path):
    (tmp_path / "old.py").write_text("v1\n", encoding="utf-8")
    before = snapshot_files(tmp_path)

    (tmp_path / "old.py").write_text("v2 变更\n", encoding="utf-8")
    (tmp_path / "new.py").write_text("new\n", encoding="utf-8")
    after = snapshot_files(tmp_path)

    changed = changed_files(before, after)
    assert [item["path"] for item in changed] == ["new.py", "old.py"]
    assert all(item["bytes"] > 0 for item in changed)


def test_changed_files_is_empty_without_edits(tmp_path):
    (tmp_path / "same.py").write_text("x\n", encoding="utf-8")
    assert changed_files(snapshot_files(tmp_path), snapshot_files(tmp_path)) == []


# ------------------------------------------------------------ 会话记录


def test_session_message_keeps_artifacts(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    store.append_message(
        session.id,
        "assistant",
        "已通过测试",
        artifacts=[{"path": "prime.py", "bytes": 172}],
    )

    messages = store.messages(session.id)
    assert messages[0]["artifacts"] == [{"path": "prime.py", "bytes": 172}]

    reopened = SessionStore(tmp_path)
    assert reopened.messages(session.id)[0]["artifacts"][0]["path"] == "prime.py"


def test_message_without_artifacts_has_no_key(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    store.append_message(session.id, "user", "你好")
    assert "artifacts" not in store.messages(session.id)[0]


# ------------------------------------------------------------ 工具集裁剪


def test_only_coding_agent_gets_code_tools(settings, tmp_path):
    configured = settings.apply_overrides(
        {"code_root": str(tmp_path / "sandbox"), "allow_code_tools": True}
    )

    chat_tools = build_tools(mock=False, settings=configured, agent_name="react")
    coding_tools = build_tools(mock=False, settings=configured, agent_name="coding")

    assert "run_python" not in chat_tools.names()
    assert "web_search" in chat_tools.names()
    assert "run_python" in coding_tools.names()
    assert "write_file" in coding_tools.names()


def test_mock_mode_keeps_demo_tools(settings):
    tools = build_tools(mock=True, settings=settings, agent_name="react")
    assert "get_weather" in tools.names()
    assert "run_python" not in tools.names()  # 离线演示不碰真实代码执行


# ------------------------------------------------------------ 文件浏览接口


@pytest.fixture
def file_server(tmp_path, monkeypatch):
    """起一个代码根目录受控的服务，用于测文件浏览接口。"""
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setenv("AGENT_CODE_ROOT", str(sandbox))
    server = create_server(
        host="127.0.0.1",
        port=0,
        llm_mode="mock",
        quiet=True,
        session_dir=str(tmp_path / "sessions"),
        require_auth=False,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", sandbox
    finally:
        server.shutdown()
        server.server_close()


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def test_file_endpoint_serves_sandbox_file(file_server):
    base, sandbox = file_server
    (sandbox / "prime.py").write_text("def is_prime(n):\n    return n > 1\n", encoding="utf-8")

    payload = _get_json(f"{base}/api/file?path=prime.py")
    assert payload["path"] == "prime.py"
    assert "def is_prime" in payload["content"]
    assert payload["truncated"] is False


def test_file_endpoint_reads_nested_file(file_server):
    base, sandbox = file_server
    (sandbox / "pkg").mkdir()
    (sandbox / "pkg" / "mod.py").write_text("VALUE = 42\n", encoding="utf-8")

    assert "VALUE = 42" in _get_json(f"{base}/api/file?path=pkg/mod.py")["content"]


def test_file_endpoint_rejects_escape(file_server):
    base, _ = file_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get_json(f"{base}/api/file?path=../secret.txt")
    assert excinfo.value.code == 400


def test_file_endpoint_reports_missing_file(file_server):
    base, _ = file_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get_json(f"{base}/api/file?path=nope.py")
    assert excinfo.value.code == 404


def test_file_endpoint_requires_path(file_server):
    base, _ = file_server
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get_json(f"{base}/api/file")
    assert excinfo.value.code == 400


def test_file_endpoint_truncates_long_file(file_server):
    base, sandbox = file_server
    (sandbox / "big.py").write_text("x = 1\n" * 5000, encoding="utf-8")

    payload = _get_json(f"{base}/api/file?path=big.py")
    assert payload["truncated"] is True
    assert len(payload["content"]) == 20000
