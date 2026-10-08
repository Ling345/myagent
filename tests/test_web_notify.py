"""把通知接到真实业务流上：填邮箱、额度用尽、登录被猜、注销。"""

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
    """真实服务 + 假的 webhook 通道（把"发出去的东西"收在列表里）。"""
    monkeypatch.setenv("AGENT_CODE_ROOT", str(tmp_path / "sandbox"))
    monkeypatch.setenv("AGENT_WEB_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("AGENT_NOTIFY_WEBHOOK", "https://hook.invalid/notify")
    accounts = AccountStore(tmp_path / "accounts.db")
    server = create_server(
        host="127.0.0.1",
        port=0,
        llm_mode="mock",
        quiet=True,
        session_dir=str(tmp_path / "sessions"),
        accounts=accounts,
    )
    inbox: list[dict] = []
    # 拦截真实网络：直接替掉 server 里那条"怎么发"的路径
    import agentcode.notify as notify_module

    monkeypatch.setattr(
        notify_module, "_default_poster", lambda _url, payload: inbox.append(payload)
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, accounts, tmp_path, inbox
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- 邮箱设置


def test_email_can_be_set_from_the_page(web):
    base, accounts, _, _ = web
    accounts.create("alice", "password123")
    client = _Client(base)
    client.login("alice")

    status, payload = client.post("/api/account/email", {"email": "Alice@Example.com"})

    assert status == 200
    assert payload["email"] == "alice@example.com"
    assert accounts.get("alice").email == "alice@example.com"
    # 页面上能读回来
    assert client.get("/api/me")[1]["account"]["email"] == "alice@example.com"


def test_email_can_be_cleared_and_junk_is_rejected(web):
    base, accounts, _, _ = web
    accounts.create("alice", "password123")
    client = _Client(base)
    client.login("alice")
    client.post("/api/account/email", {"email": "alice@example.com"})

    status, payload = client.post("/api/account/email", {"email": "不是邮箱"})
    assert status == 400
    assert "邮箱" in payload["error"]
    assert accounts.get("alice").email == "alice@example.com"  # 没被改坏

    assert client.post("/api/account/email", {"email": ""})[1]["email"] == ""
    assert accounts.get("alice").email is None


def test_email_endpoint_needs_login(web):
    base, accounts, _, _ = web
    accounts.create("alice", "password123")
    assert _Client(base).post("/api/account/email", {"email": "a@b.com"})[0] == 401


# ---------------------------------------------------------------- 触发通知


def test_running_out_of_quota_notifies_once(web):
    base, accounts, _, inbox = web
    account = accounts.create("alice", "password123", daily_token_limit=5)
    accounts.set_email("alice", "alice@example.com")
    accounts.record_usage(account.id, 100, prompt_tokens=100, completion_tokens=0, run_id="r1")
    client = _Client(base)
    client.login("alice")

    first = client.post("/api/run", {"task": "你好", "agent": "echo"})
    second = client.post("/api/run", {"task": "再问一次", "agent": "echo"})

    assert first[0] == 402
    assert second[0] == 402
    subjects = [item["subject"] for item in inbox]
    assert len(subjects) == 1
    assert "额度" in subjects[0]
    assert accounts.notifications()[0]["event"] == "quota.exhausted"


def test_repeated_login_failures_notify_the_owner(web):
    base, accounts, _, inbox = web
    accounts.create("alice", "password123")
    accounts.set_email("alice", "alice@example.com")
    client = _Client(base)

    for _ in range(5):
        assert client.post("/api/login", {"name": "alice", "password": "错的"})[0] == 401

    assert [item["event"] for item in inbox] == ["security.login_failures"]
    assert "登录" in inbox[0]["subject"]
    # 审计里也看得到这几次失败
    assert len(accounts.audit_entries(action="login.fail")) == 5


def test_one_or_two_failures_do_not_notify(web):
    base, accounts, _, inbox = web
    accounts.create("alice", "password123")
    accounts.set_email("alice", "alice@example.com")
    client = _Client(base)

    for _ in range(2):
        client.post("/api/login", {"name": "alice", "password": "错的"})

    assert inbox == []


def test_deleting_the_account_sends_a_farewell(web):
    base, accounts, _, inbox = web
    accounts.create("alice", "password123")
    accounts.set_email("alice", "alice@example.com")
    client = _Client(base)
    client.login("alice")

    status, _ = client.post("/api/account/delete", {"confirm": "alice"})

    assert status == 200
    assert accounts.get("alice") is None
    assert [item["event"] for item in inbox] == ["account.deleted"]
    assert "注销" in inbox[0]["subject"]
    # 最后一条消息要能发出去：邮箱是删除前抓下来的
    assert inbox[0]["email"] == "alice@example.com"


def test_notifications_are_not_sent_to_other_accounts(web):
    base, accounts, _, inbox = web
    accounts.create("alice", "password123")
    accounts.create("bob", "password123")
    accounts.set_email("bob", "bob@example.com")
    client = _Client(base)

    for _ in range(5):
        client.post("/api/login", {"name": "alice", "password": "错的"})

    # alice 没填邮箱 → 不会发；bob 的邮箱不该被用到
    assert inbox == []
    assert accounts.notifications()[0]["status"] == "skipped"
