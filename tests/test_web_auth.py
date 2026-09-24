"""网页端账号体系：登录、鉴权、会话按用户隔离、配额。"""

from __future__ import annotations

import http.cookiejar
import json
import threading
import urllib.error
import urllib.request

import pytest

from agentcode.accounts import AccountStore
from agentcode.config import Settings
from agentcode.web.runner import build_tools
from agentcode.web.server import create_server


class Client:
    """带 Cookie 的测试客户端。"""

    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def get(self, path: str) -> tuple[int, dict]:
        return self._call(path, None)

    def post(self, path: str, payload: dict | None = None) -> tuple[int, dict]:
        # 没有 body 的 POST（例如登出）也要真的用 POST
        return self._call(path, payload if payload is not None else {})

    def _call(self, path: str, payload: dict | None) -> tuple[int, dict]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        method = "POST" if data is not None else "GET"
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method=method,
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return response.status, (json.loads(body) if body.startswith("{") else {})
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8")
            return error.code, (json.loads(body) if body.startswith("{") else {})


@pytest.fixture
def web(tmp_path):
    """一个要求登录的服务，返回 (地址, 账号库)。"""
    accounts = AccountStore(tmp_path / "accounts.db")
    server = create_server(
        host="127.0.0.1",
        port=0,
        llm_mode="mock",
        quiet=True,
        session_dir=str(tmp_path / "sessions"),
        accounts=accounts,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", accounts, server
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- 鉴权


def test_healthz_is_public(web):
    base, _, _ = web
    status, payload = Client(base).get("/healthz")
    assert status == 200
    assert payload["status"] == "ok"


def test_me_and_apis_require_login(web):
    base, _, _ = web
    client = Client(base)
    assert client.get("/api/me")[0] == 401
    assert client.get("/api/sessions")[0] == 401
    assert client.post("/api/run", {"agent": "echo", "task": "你好"})[0] == 401


def test_login_with_wrong_password(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    status, payload = Client(base).post("/api/login", {"name": "alice", "password": "bad"})
    assert status == 401
    assert "不正确" in payload["error"]


def test_login_then_use_apis(web):
    base, accounts, _ = web
    account = accounts.create("alice", "password123")
    client = Client(base)

    status, payload = client.post("/api/login", {"name": "alice", "password": "password123"})
    assert status == 200
    assert payload["account"]["name"] == "alice"
    assert client.get("/api/me")[0] == 200
    assert client.get("/api/sessions")[1]["sessions"] == []

    # 跑一轮之后，用量要记到该账号名下
    status, _ = client.post(
        "/api/run", {"agent": "react", "task": "北京天气如何", "session_id": "s1"}
    )
    assert status == 200
    used, calls = accounts.usage_today(account.id)
    assert calls == 1
    assert used > 0


def test_logout_clears_session(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    client = Client(base)
    client.post("/api/login", {"name": "alice", "password": "password123"})
    assert client.get("/api/me")[0] == 200

    client.post("/api/logout")
    assert client.get("/api/me")[0] == 401


def test_logout_works_without_body(web):
    """回归：前端登出是不带 body 的 POST，不能被"请求体为空"挡掉。"""
    base, accounts, _ = web
    accounts.create("alice", "password123")
    client = Client(base)
    client.post("/api/login", {"name": "alice", "password": "password123"})
    assert client.get("/api/me")[0] == 200

    request = urllib.request.Request(f"{base}/api/logout", data=b"", method="POST")
    with client.opener.open(request, timeout=10) as response:
        assert response.status == 200
    assert client.get("/api/me")[0] == 401


def test_tampered_cookie_is_rejected(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    client = Client(base)
    client.post("/api/login", {"name": "alice", "password": "password123"})

    # 改掉签名再请求
    for cookie in client.jar:
        cookie.value = cookie.value[:-1] + ("0" if cookie.value[-1] != "0" else "1")
    assert client.get("/api/me")[0] == 401


# ------------------------------------------------------------ 会话隔离


def test_sessions_are_isolated_between_users(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    accounts.create("bob", "password123")

    alice = Client(base)
    alice.post("/api/login", {"name": "alice", "password": "password123"})
    alice.post("/api/run", {"agent": "echo", "task": "alice 的问题", "session_id": "a1"})
    assert len(alice.get("/api/sessions")[1]["sessions"]) == 1

    bob = Client(base)
    bob.post("/api/login", {"name": "bob", "password": "password123"})
    assert bob.get("/api/sessions")[1]["sessions"] == []
    # 也不能用知道 id 的方式读到别人的会话
    assert bob.get("/api/session?id=a1")[1]["session"] is None


# ---------------------------------------------------------------- 配额


def test_quota_blocks_run_when_exhausted(web):
    base, accounts, _ = web
    account = accounts.create("alice", "password123", daily_token_limit=100)
    accounts.record_usage(account.id, 100)

    client = Client(base)
    client.post("/api/login", {"name": "alice", "password": "password123"})
    status, payload = client.post("/api/run", {"agent": "echo", "task": "你好"})

    assert status == 402
    assert "额度已用完" in payload["error"]


def test_me_reports_remaining_quota(web):
    base, accounts, _ = web
    account = accounts.create("alice", "password123", daily_token_limit=500)
    accounts.record_usage(account.id, 200)

    client = Client(base)
    client.post("/api/login", {"name": "alice", "password": "password123"})
    payload = client.get("/api/me")[1]["account"]
    assert payload["used_today"] == 200
    assert payload["remaining"] == 300


# ------------------------------------------------------- 执行代码默认关闭


def test_code_tools_disabled_by_default(settings, tmp_path):
    configured = settings.apply_overrides(
        {"code_root": str(tmp_path / "sandbox"), "allow_code_tools": False}
    )
    tools = build_tools(mock=False, settings=configured, agent_name="coding")
    assert "run_python" not in tools.names()
    assert "write_file" not in tools.names()


def test_code_tools_can_be_enabled_explicitly(settings, tmp_path):
    configured = settings.apply_overrides(
        {"code_root": str(tmp_path / "sandbox"), "allow_code_tools": True}
    )
    tools = build_tools(mock=False, settings=configured, agent_name="coding")
    assert "run_python" in tools.names()
