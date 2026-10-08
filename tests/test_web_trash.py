"""网页上的回收站与操作审计（离线）。

重点：删错的能找回来，而且**每一步都留痕**——"谁在什么时候删了什么"
事后查得到，这正是用户来投诉时你唯一的凭据。
"""

from __future__ import annotations

import http.cookiejar
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agentcode.accounts import AccountStore
from agentcode.web.server import create_server


class _Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def _open(self, request: urllib.request.Request) -> tuple[int, bytes]:
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def get(self, path: str) -> tuple[int, dict]:
        status, body = self._open(urllib.request.Request(f"{self.base}{path}"))
        return status, (json.loads(body.decode("utf-8")) if body.startswith(b"{") else {})

    def post(self, path: str, payload: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        status, body = self._open(request)
        return status, (json.loads(body.decode("utf-8")) if body.startswith(b"{") else {})

    def login(self, name: str, password: str = "password123") -> int:
        return self.post("/api/login", {"name": name, "password": password})[0]


@pytest.fixture
def web(tmp_path, monkeypatch):
    accounts = AccountStore(tmp_path / "accounts.db")
    monkeypatch.setenv("AGENT_CODE_ROOT", str(tmp_path / "sandbox"))
    monkeypatch.setenv("AGENT_WEB_SESSION_DIR", str(tmp_path / "sessions"))
    server = create_server(
        host="127.0.0.1",
        port=0,
        llm_mode="mock",
        quiet=True,
        session_dir=str(tmp_path / "sessions"),
        accounts=accounts,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, accounts, tmp_path
    finally:
        server.shutdown()
        server.server_close()


def _seed_code(tmp_path: Path, accounts: AccountStore, name: str) -> str:
    account_id = accounts.get(name).id
    code_dir = tmp_path / "sandbox" / account_id
    code_dir.mkdir(parents=True, exist_ok=True)
    (code_dir / "calc.py").write_text("VALUE = 1\n", encoding="utf-8")
    return account_id


def _make_session(client: _Client, name: str = "北京游") -> str:
    """建一个会话并写进去一条消息（空会话会被自动清掉，所以要有内容）。"""
    session_id = client.post("/api/sessions/create", {})[1]["session"]["id"]
    client.post("/api/sessions/rename", {"session_id": session_id, "name": name})
    return session_id


# ---------------------------------------------------------------- 回收站


def test_deleted_session_shows_up_in_the_trash_and_can_come_back(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    client = _Client(base)
    client.login("alice")
    session_id = _make_session(client)

    client.post("/api/sessions/delete", {"session_id": session_id})
    assert client.get("/api/sessions")[1]["sessions"] == []

    status, trash = client.get("/api/trash")
    assert status == 200
    assert [item["name"] for item in trash["sessions"]] == ["北京游"]
    entry = trash["sessions"][0]["entry"]

    status, report = client.post("/api/trash/restore", {"kind": "session", "entry": entry})
    assert status == 200
    assert report["restored"] is True
    # 恢复之后马上就能在列表里看到（服务不用重启）
    assert [item["id"] for item in client.get("/api/sessions")[1]["sessions"]] == [session_id]
    assert client.get("/api/trash")[1]["sessions"] == []


def test_purging_from_the_trash_removes_it_for_good(web):
    base, accounts, tmp_path = web
    accounts.create("alice", "password123")
    client = _Client(base)
    client.login("alice")
    session_id = _make_session(client)
    client.post("/api/sessions/delete", {"session_id": session_id})
    entry = client.get("/api/trash")[1]["sessions"][0]["entry"]

    status, payload = client.post("/api/trash/purge", {"kind": "session", "entry": entry})

    assert status == 200
    assert payload["purged"] is True
    assert client.get("/api/trash")[1]["sessions"] == []
    assert list((tmp_path / "sessions").rglob("*.json")) == []


def test_purged_code_directory_can_be_restored_from_the_trash(web):
    base, accounts, tmp_path = web
    accounts.create("alice", "password123")
    account_id = _seed_code(tmp_path, accounts, "alice")
    client = _Client(base)
    client.login("alice")

    assert client.post("/api/account/purge-code")[1]["removed"] == 1
    trash = client.get("/api/trash")[1]
    assert len(trash["code"]) == 1
    assert trash["code"][0]["files"] == 1

    entry = trash["code"][0]["entry"]
    assert client.post("/api/trash/restore", {"kind": "code", "entry": entry})[0] == 200
    assert (tmp_path / "sandbox" / account_id / "calc.py").is_file()


def test_empty_trash_clears_everything(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    client = _Client(base)
    client.login("alice")
    session_id = _make_session(client)
    client.post("/api/sessions/delete", {"session_id": session_id})

    status, report = client.post("/api/trash/empty", {})

    assert status == 200
    assert report["removed"]["sessions"] == 1
    assert client.get("/api/trash")[1]["sessions"] == []


def test_trash_needs_login(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    client = _Client(base)

    assert client.get("/api/trash")[0] == 401
    assert client.post("/api/trash/restore", {"kind": "session", "entry": "x"})[0] == 401
    assert client.post("/api/trash/empty", {})[0] == 401


def test_one_account_cannot_touch_anothers_trash(web):
    base, accounts, tmp_path = web
    accounts.create("alice", "password123")
    accounts.create("bob", "password123")
    alice = _Client(base)
    alice.login("alice")
    session_id = _make_session(alice, "alice 的会话")
    alice.post("/api/sessions/delete", {"session_id": session_id})
    entry = alice.get("/api/trash")[1]["sessions"][0]["entry"]

    bob = _Client(base)
    bob.login("bob")

    assert bob.get("/api/trash")[1]["sessions"] == []
    # 拿别人的条目名来恢复也必须是"找不到"，而不是跨账号恢复
    status, payload = bob.post("/api/trash/restore", {"kind": "session", "entry": entry})
    assert status == 400
    assert "没有这一条" in payload["error"]
    assert alice.get("/api/sessions")[1]["sessions"] == []  # alice 的东西没被动
    assert bob.get("/api/sessions")[1]["sessions"] == []


def test_trash_rejects_a_path_like_entry(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    client = _Client(base)
    client.login("alice")

    status, payload = client.post("/api/trash/restore", {"kind": "session", "entry": "../evil"})

    assert status == 400
    assert "不合法" in payload["error"]


# ---------------------------------------------------------------- 审计


def test_login_attempts_are_audited(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    client = _Client(base)

    client.post("/api/login", {"name": "alice", "password": "错的密码"})
    client.login("alice")

    entries = accounts.audit_entries(action_prefix="login.")
    assert [item["action"] for item in entries] == ["login.ok", "login.fail"]
    assert entries[1]["actor_name"] == "alice"


def test_audit_never_records_the_password(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    client = _Client(base)

    client.post("/api/login", {"name": "alice", "password": "password123"})

    assert "password123" not in json.dumps(accounts.audit_entries(), ensure_ascii=False)


def test_deleting_and_restoring_are_audited(web):
    base, accounts, tmp_path = web
    accounts.create("alice", "password123")
    _seed_code(tmp_path, accounts, "alice")
    client = _Client(base)
    client.login("alice")
    session_id = _make_session(client, "要删的会话")

    client.post("/api/sessions/delete", {"session_id": session_id})
    entry = client.get("/api/trash")[1]["sessions"][0]["entry"]
    client.post("/api/trash/restore", {"kind": "session", "entry": entry})
    client.post("/api/account/purge-code")
    client.post("/api/trash/empty", {})

    actions = [item["action"] for item in accounts.audit_entries(limit=20)]
    assert "session.delete" in actions
    assert "trash.restore" in actions
    assert "code.purge" in actions
    assert "trash.purge" in actions
    target = next(item for item in accounts.audit_entries() if item["action"] == "session.delete")
    assert target["target"] == "要删的会话"
    assert target["actor_name"] == "alice"
