"""命令行上的邮箱与通知台账。"""

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


def test_user_email_can_be_set_shown_and_cleared(tmp_path, monkeypatch, capsys):
    accounts = _accounts(tmp_path, monkeypatch)
    accounts.create("alice", "password123")
    capsys.readouterr()

    assert main(["user", "email", "alice"]) == 0
    assert "没填" in capsys.readouterr().out

    assert main(["user", "email", "alice", "--set", "Alice@Example.com"]) == 0
    assert "alice@example.com" in capsys.readouterr().out
    assert accounts.get("alice").email == "alice@example.com"

    assert main(["user", "email", "alice", "--clear"]) == 0
    assert accounts.get("alice").email is None


def test_user_email_rejects_junk(tmp_path, monkeypatch, capsys):
    accounts = _accounts(tmp_path, monkeypatch)
    accounts.create("alice", "password123")
    capsys.readouterr()

    assert main(["user", "email", "alice", "--set", "不是邮箱"]) == 2
    assert "邮箱" in capsys.readouterr().out
    assert accounts.get("alice").email is None


def test_setting_an_email_is_audited_without_leaking_it_to_everyone(
    tmp_path, monkeypatch, capsys
):
    _accounts(tmp_path, monkeypatch)
    main(["user", "add", "alice", "--password", "password123"])
    main(["user", "email", "alice", "--set", "alice@example.com"])
    capsys.readouterr()

    assert main(["audit", "list", "--json"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output[output.index("{") :])
    entry = next(item for item in payload["entries"] if item["action"] == "account.email")
    # 被改的是账号，改成了什么写在 detail 里（和改额度、换套餐一个格式）
    assert entry["target"] == "alice"
    assert entry["detail"]["email"] == "alice@example.com"


def test_notify_list_shows_queued_notifications(tmp_path, monkeypatch, capsys):
    """没配渠道时最要紧的是"看得见它没发出去"。"""
    accounts = _accounts(tmp_path, monkeypatch)
    account = accounts.create("alice", "password123")
    accounts.set_email("alice", "alice@example.com")
    accounts.record_notification(
        event="plan.changed",
        status="queued",
        account_id=account.id,
        account_name=account.name,
        email=account.email,
        subject="套餐已更新",
    )
    capsys.readouterr()

    assert main(["notify", "list"]) == 0
    output = capsys.readouterr().out
    assert "plan.changed" in output
    assert "未发" in output

    assert main(["notify", "list", "--json"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output[output.index("{") :])
    assert payload["notifications"][0]["event"] == "plan.changed"
    assert payload["notifications"][0]["status"] == "queued"


def test_notify_list_can_filter_by_account_and_status(tmp_path, monkeypatch, capsys):
    accounts = _accounts(tmp_path, monkeypatch)
    alice = accounts.create("alice", "password123")
    bob = accounts.create("bob", "password123")
    accounts.record_notification(event="plan.changed", status="sent", account_id=alice.id, account_name="alice")
    accounts.record_notification(event="quota.exhausted", status="failed", account_id=bob.id, account_name="bob")
    capsys.readouterr()

    assert main(["notify", "list", "--account", "alice"]) == 0
    output = capsys.readouterr().out
    assert "plan.changed" in output
    assert "quota.exhausted" not in output

    assert main(["notify", "list", "--status", "failed"]) == 0
    output = capsys.readouterr().out
    assert "quota.exhausted" in output
    assert "plan.changed" not in output


def test_notify_test_tells_you_when_nothing_is_configured(tmp_path, monkeypatch, capsys):
    accounts = _accounts(tmp_path, monkeypatch)
    accounts.create("alice", "password123")
    capsys.readouterr()

    # 没填邮箱
    assert main(["notify", "test", "alice"]) == 1
    assert "还没填邮箱" in capsys.readouterr().out

    accounts.set_email("alice", "alice@example.com")
    assert main(["notify", "test", "alice"]) == 1
    output = capsys.readouterr().out
    assert "没配" in output or "没发出去" in output
    assert accounts.notifications()[0]["status"] == "queued"


def test_notify_test_reports_a_channel_failure(tmp_path, monkeypatch, capsys):
    accounts = _accounts(tmp_path, monkeypatch)
    accounts.create("alice", "password123")
    accounts.set_email("alice", "alice@example.com")
    monkeypatch.setenv("AGENT_NOTIFY_WEBHOOK", "http://127.0.0.1:1/notify")
    capsys.readouterr()

    assert main(["notify", "test", "alice"]) == 1
    output = capsys.readouterr().out
    assert "webhook" in output
    assert accounts.notifications()[0]["status"] == "failed"


def test_notify_test_for_an_unknown_account_returns_two(tmp_path, monkeypatch, capsys):
    _accounts(tmp_path, monkeypatch)
    assert main(["notify", "test", "查无此人"]) == 2
    assert "没有这个账号" in capsys.readouterr().out
