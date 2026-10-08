"""命令行上的审计日志与回收站。"""

from __future__ import annotations

import json
from pathlib import Path

from agentcode.accounts import AccountStore
from agentcode.cli import main


def _env(tmp_path: Path, monkeypatch) -> AccountStore:
    """把库和两个数据目录都指到临时目录，返回账号库。"""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agentcode.db"))
    monkeypatch.setenv("AGENT_WEB_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("AGENT_CODE_ROOT", str(tmp_path / "sandbox"))
    return AccountStore(tmp_path / "agentcode.db")


# ---------------------------------------------------------------- 审计


def test_audit_list_shows_operator_actions(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    assert main(["user", "add", "alice", "--password", "password123"]) == 0
    assert main(["user", "limit", "alice", "1234"]) == 0
    capsys.readouterr()

    assert main(["audit", "list", "--limit", "10"]) == 0
    output = capsys.readouterr().out
    assert "account.create" in output
    assert "account.limit" in output
    assert "alice" in output


def test_audit_list_never_shows_a_password(tmp_path, monkeypatch, capsys):
    """审计表里出现明文密码是灾难：建账号时给的那个密码绝不能落进去。"""
    _env(tmp_path, monkeypatch)
    assert main(["user", "add", "alice", "--password", "password123"]) == 0
    assert main(["user", "passwd", "alice", "--password", "another-secret"]) == 0
    capsys.readouterr()

    assert main(["audit", "list", "--json"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output[output.index("{") :])
    assert "password123" not in output
    assert "another-secret" not in output
    assert any(item["action"] == "account.passwd" for item in payload["entries"])


def test_audit_list_filters_by_action_prefix(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    store = AccountStore(tmp_path / "agentcode.db")
    store.record_audit("login.ok", actor_name="alice")
    store.record_audit("session.delete", actor_name="alice", target="会话 A")

    assert main(["audit", "list", "--action-prefix", "login."]) == 0
    output = capsys.readouterr().out
    assert "login.ok" in output
    assert "session.delete" not in output


def test_audit_prune_drops_old_entries(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    store = AccountStore(tmp_path / "agentcode.db")
    store.record_audit("login.ok", actor_name="alice", at="2020-01-01T00:00:00+00:00")
    store.record_audit("login.ok", actor_name="alice")

    assert main(["audit", "prune", "--days", "180"]) == 0
    output = capsys.readouterr().out
    assert "1 条" in output
    assert len(store.audit_entries()) == 1


def test_audit_prune_without_days_uses_the_configured_retention(
    tmp_path, monkeypatch, capsys
):
    _env(tmp_path, monkeypatch)
    store = AccountStore(tmp_path / "agentcode.db")
    store.record_audit("login.ok", actor_name="alice", at="2020-01-01T00:00:00+00:00")

    assert main(["audit", "prune"]) == 0
    assert "1 条" in capsys.readouterr().out
    assert store.audit_entries() == []


# ---------------------------------------------------------------- 回收站


def test_trash_list_restore_and_purge(tmp_path, monkeypatch, capsys):
    from agentcode.web.sessions import SessionStore

    accounts = _env(tmp_path, monkeypatch)
    account = accounts.create("alice", "password123")
    sessions = SessionStore(tmp_path / "sessions" / account.id)
    session = sessions.create(name="被误删的会话")
    sessions.append_message(session.id, "user", "内容还在")
    sessions.delete(session.id)
    capsys.readouterr()

    assert main(["trash", "list", "alice"]) == 0
    output = capsys.readouterr().out
    assert "被误删的会话" in output

    assert main(["trash", "list", "alice", "--json"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output[output.index("{") :])
    entry = payload["sessions"][0]["entry"]

    assert main(["trash", "restore", "alice", "session", entry]) == 0
    assert "已恢复" in capsys.readouterr().out
    # 换一个全新的仓库（相当于重启服务）读得到，而且内容在
    fresh = SessionStore(tmp_path / "sessions" / account.id)
    assert [item["name"] for item in fresh.list()] == ["被误删的会话"]
    assert fresh.messages(fresh.list()[0]["id"])[0]["content"] == "内容还在"
    assert main(["trash", "list", "alice"]) == 0
    assert "空的" in capsys.readouterr().out


def test_trash_purge_without_an_entry_empties_the_bin(tmp_path, monkeypatch, capsys):
    from agentcode.web.sessions import SessionStore

    accounts = _env(tmp_path, monkeypatch)
    account = accounts.create("alice", "password123")
    sessions = SessionStore(tmp_path / "sessions" / account.id)
    session = sessions.create(name="不要了")
    sessions.append_message(session.id, "user", "内容")
    sessions.delete(session.id)
    capsys.readouterr()

    assert main(["trash", "purge", "alice", "--yes"]) == 0
    assert "已清空" in capsys.readouterr().out
    assert list((tmp_path / "sessions").rglob("*.json")) == []


def test_trash_purge_asks_before_emptying(tmp_path, monkeypatch, capsys):
    accounts = _env(tmp_path, monkeypatch)
    accounts.create("alice", "password123")
    monkeypatch.setattr("builtins.input", lambda _prompt="": "no")

    assert main(["trash", "purge", "alice"]) == 1
    assert "已取消" in capsys.readouterr().out


def test_trash_list_for_an_unknown_account_returns_two(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    assert main(["trash", "list", "查无此人"]) == 2
    assert "没有这个账号" in capsys.readouterr().out


def test_trash_restore_rejects_a_path_like_entry(tmp_path, monkeypatch, capsys):
    accounts = _env(tmp_path, monkeypatch)
    accounts.create("alice", "password123")

    assert main(["trash", "restore", "alice", "session", "../evil"]) == 1
    assert "不合法" in capsys.readouterr().out


def test_disabling_and_enabling_accounts_are_audited(tmp_path, monkeypatch, capsys):
    _env(tmp_path, monkeypatch)
    main(["user", "add", "alice", "--password", "password123"])
    main(["user", "disable", "alice"])
    main(["user", "enable", "alice"])
    capsys.readouterr()

    assert main(["audit", "list", "--json"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output[output.index("{") :])
    actions = {item["action"] for item in payload["entries"]}
    assert {"account.create", "account.disable", "account.enable"} <= actions
