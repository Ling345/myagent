"""命令行上的 API 令牌管理。"""

from __future__ import annotations

import json
from pathlib import Path

from agentcode.accounts import AccountStore
from agentcode.cli import main
from agentcode.tokens import ApiTokenService


def _accounts(tmp_path: Path, monkeypatch) -> AccountStore:
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "agentcode.db"))
    monkeypatch.setenv("AGENT_WEB_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("AGENT_CODE_ROOT", str(tmp_path / "sandbox"))
    return AccountStore(tmp_path / "agentcode.db")


def test_create_prints_the_plaintext_once(tmp_path, monkeypatch, capsys):
    accounts = _accounts(tmp_path, monkeypatch)
    accounts.create("alice", "password123")
    capsys.readouterr()

    assert main(["token", "create", "alice", "--name", "CI"]) == 0
    output = capsys.readouterr().out
    assert "只显示这一次" in output
    plaintext = next(line.strip() for line in output.splitlines() if line.strip().startswith("agk_"))

    # 列表里只有前缀，永远拿不回明文
    assert main(["token", "list", "alice"]) == 0
    listed = capsys.readouterr().out
    assert "CI" in listed
    assert plaintext not in listed
    assert plaintext[: len(ApiTokenService(accounts).list(accounts.get("alice").id)[0]["prefix"])] in listed


def test_created_token_works_and_revoke_kills_it(tmp_path, monkeypatch, capsys):
    accounts = _accounts(tmp_path, monkeypatch)
    account = accounts.create("alice", "password123")
    service = ApiTokenService(accounts)
    capsys.readouterr()

    main(["token", "create", "alice", "--name", "脚本"])
    output = capsys.readouterr().out
    plaintext = next(line.strip() for line in output.splitlines() if line.strip().startswith("agk_"))
    assert service.resolve(plaintext) is not None

    token_id = service.list(account.id)[0]["id"]
    assert main(["token", "revoke", "alice", token_id]) == 0
    assert service.resolve(plaintext) is None


def test_token_commands_are_audited_without_the_plaintext(tmp_path, monkeypatch, capsys):
    accounts = _accounts(tmp_path, monkeypatch)
    account = accounts.create("alice", "password123")
    service = ApiTokenService(accounts)
    capsys.readouterr()

    main(["token", "create", "alice", "--name", "脚本"])
    output = capsys.readouterr().out
    plaintext = next(line.strip() for line in output.splitlines() if line.strip().startswith("agk_"))
    main(["token", "revoke", "alice", service.list(account.id)[0]["id"]])
    capsys.readouterr()

    assert main(["audit", "list", "--json"]) == 0
    raw = capsys.readouterr().out
    payload = json.loads(raw[raw.index("{") :])
    actions = {item["action"] for item in payload["entries"]}
    assert {"api_token.create", "api_token.revoke"} <= actions
    assert plaintext not in raw


def test_unknown_account_and_token_return_two_and_one(tmp_path, monkeypatch, capsys):
    accounts = _accounts(tmp_path, monkeypatch)
    accounts.create("alice", "password123")
    capsys.readouterr()

    assert main(["token", "create", "查无此人"]) == 2
    assert "没有这个账号" in capsys.readouterr().out

    assert main(["token", "revoke", "alice", "不存在的id"]) == 1
    assert "没有找到" in capsys.readouterr().out
