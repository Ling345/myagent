"""命令行上的 API 任务与回调台账（运维侧排查用）。"""

from __future__ import annotations

import json
from pathlib import Path

from agentcode.accounts import AccountStore
from agentcode.cli import main


def _accounts(tmp_path: Path, monkeypatch) -> AccountStore:
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agentcode.db"))
    monkeypatch.setenv("AGENT_WEB_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("AGENT_CODE_ROOT", str(tmp_path / "sandbox"))
    return AccountStore(tmp_path / "agentcode.db")


def _finished_run(store: AccountStore, account, run_id: str, *, callback_url: str) -> None:
    store.create_api_run(
        run_id,
        account_id=account.id,
        agent="echo",
        task="你好",
        callback_url=callback_url,
    )
    store.finish_api_run(run_id, result={"run_id": run_id, "answer": "好"})


def test_api_list_shows_how_the_callback_went(tmp_path, monkeypatch, capsys):
    """用户说"我没收到回调"时，运营第一眼看的就是这一行。"""
    store = _accounts(tmp_path, monkeypatch)
    account = store.create("alice", "password123")
    _finished_run(store, account, "run-1", callback_url="https://hooks.example.com/x")
    store.record_callback_attempt(
        "run-1", status="succeeded", attempts=1, delivered_at="2026-10-10T00:00:05+00:00"
    )
    capsys.readouterr()

    assert main(["api", "list", "alice"]) == 0

    output = capsys.readouterr().out
    assert "run-1" in output
    assert "已发" in output


def test_api_list_shows_a_failed_callback(tmp_path, monkeypatch, capsys):
    store = _accounts(tmp_path, monkeypatch)
    account = store.create("alice", "password123")
    _finished_run(store, account, "run-1", callback_url="https://hooks.example.com/x")
    store.record_callback_attempt(
        "run-1", status="failed", attempts=4, error="收方返回 400，已放弃（共 4 次）"
    )
    capsys.readouterr()

    assert main(["api", "list", "alice"]) == 0

    assert "失败" in capsys.readouterr().out


def test_api_callbacks_lists_the_delivery_details(tmp_path, monkeypatch, capsys):
    store = _accounts(tmp_path, monkeypatch)
    account = store.create("alice", "password123")
    _finished_run(store, account, "run-1", callback_url="https://hooks.example.com/hook")
    store.record_callback_attempt(
        "run-1",
        status="pending",
        attempts=2,
        error="收方返回 503",
        next_at="2026-10-10T00:01:00+00:00",
    )
    capsys.readouterr()

    assert main(["api", "callbacks", "alice"]) == 0

    output = capsys.readouterr().out
    assert "run-1" in output
    assert "hooks.example.com" in output
    assert "收方返回 503" in output
    assert "2" in output


def test_api_callbacks_can_output_json(tmp_path, monkeypatch, capsys):
    store = _accounts(tmp_path, monkeypatch)
    account = store.create("alice", "password123")
    _finished_run(store, account, "run-1", callback_url="https://hooks.example.com/hook")
    capsys.readouterr()

    assert main(["api", "callbacks", "alice", "--json"]) == 0

    raw = capsys.readouterr().out
    payload = json.loads(raw[raw.index("{") :])
    assert payload["callbacks"][0]["run_id"] == "run-1"
    assert payload["callbacks"][0]["callback_status"] == "pending"


def test_api_callbacks_says_when_there_is_nothing_to_show(tmp_path, monkeypatch, capsys):
    store = _accounts(tmp_path, monkeypatch)
    store.create("alice", "password123")
    capsys.readouterr()

    assert main(["api", "callbacks", "alice"]) == 0

    assert "还没有" in capsys.readouterr().out


def test_api_callbacks_on_an_unknown_account_returns_two(tmp_path, monkeypatch, capsys):
    _accounts(tmp_path, monkeypatch)
    capsys.readouterr()

    assert main(["api", "callbacks", "查无此人"]) == 2

    assert "没有这个账号" in capsys.readouterr().out
