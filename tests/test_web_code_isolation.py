"""代码工作目录必须按用户隔离（离线）。

会话目录一直是按用户分的，代码目录曾经是全局共享的——也就是说 alice 让 agent
写进去的文件，bob 换个账号就能读到。加文件上传之后这个洞会被放大，
所以先把它堵上。
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
from agentcode.web.server import code_root_for, create_server


class _Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def _call(self, path: str, payload: dict | None) -> tuple[int, dict]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method="POST" if data is not None else "GET",
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return response.status, (json.loads(body) if body.startswith("{") else {})
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8")
            return error.code, (json.loads(body) if body.startswith("{") else {})

    def get(self, path: str) -> tuple[int, dict]:
        return self._call(path, None)

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        return self._call(path, payload)

    def login(self, name: str, password: str = "password123") -> int:
        return self.post("/api/login", {"name": name, "password": password})[0]


@pytest.fixture
def web(tmp_path, monkeypatch):
    """要求登录的服务，账号库与代码目录都隔离到临时目录。"""
    accounts = AccountStore(tmp_path / "accounts.db")
    code_root = tmp_path / "sandbox"
    monkeypatch.setenv("AGENT_CODE_ROOT", str(code_root))
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
        yield base, accounts, code_root
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- 路径计算


def test_code_root_for_is_per_account(tmp_path):
    root = code_root_for(tmp_path, "abc123")
    assert root == (tmp_path / "abc123").resolve()


def test_code_root_for_without_account_falls_back_to_shared(tmp_path):
    """免登录本地自用模式没有账号，行为不能变。"""
    assert code_root_for(tmp_path, None) == tmp_path.resolve()
    assert code_root_for(tmp_path, "") == tmp_path.resolve()


# ---------------------------------------------------------------- 跨用户隔离


def _seed(accounts: AccountStore, code_root: Path, account: str, filename: str, text: str):
    """直接把文件放进某个账号的代码目录里。"""
    target = code_root / accounts.get(account).id
    target.mkdir(parents=True, exist_ok=True)
    (target / filename).write_text(text, encoding="utf-8")
    return target / filename


def test_owner_can_read_their_own_file(web):
    base, accounts, code_root = web
    accounts.create("alice", "password123")
    _seed(accounts, code_root, "alice", "secret.py", "SECRET = 1\n")

    client = _Client(base)
    client.login("alice")
    status, payload = client.get("/api/file?path=secret.py")
    assert status == 200
    assert "SECRET = 1" in payload["content"]


def test_another_user_cannot_read_it(web):
    """这条是本次改动的核心：A 的文件 B 必须看不到。"""
    base, accounts, code_root = web
    accounts.create("alice", "password123")
    accounts.create("bob", "password123")
    _seed(accounts, code_root, "alice", "secret.py", "SECRET = 1\n")

    bob = _Client(base)
    bob.login("bob")
    status, _ = bob.get("/api/file?path=secret.py")
    assert status == 404


def test_each_user_sees_their_own_files(web):
    base, accounts, code_root = web
    accounts.create("alice", "password123")
    accounts.create("bob", "password123")
    _seed(accounts, code_root, "alice", "alice_only.py", "A = 1\n")
    _seed(accounts, code_root, "bob", "bob_only.py", "B = 1\n")

    alice, bob = _Client(base), _Client(base)
    alice.login("alice")
    bob.login("bob")

    assert alice.get("/api/file?path=alice_only.py")[0] == 200
    assert alice.get("/api/file?path=bob_only.py")[0] == 404
    assert bob.get("/api/file?path=bob_only.py")[0] == 200
    assert bob.get("/api/file?path=alice_only.py")[0] == 404


def test_shared_root_is_not_reachable_from_a_logged_in_account(web):
    """直接放在共享根下的文件，登录用户也不该通过接口读到。"""
    base, accounts, code_root = web
    accounts.create("alice", "password123")
    code_root.mkdir(parents=True, exist_ok=True)
    (code_root / "at_root.py").write_text("ROOT = 1\n", encoding="utf-8")

    client = _Client(base)
    client.login("alice")
    assert client.get("/api/file?path=at_root.py")[0] == 404


def test_path_escape_is_still_rejected(web):
    base, accounts, code_root = web
    accounts.create("alice", "password123")
    _seed(accounts, code_root, "alice", "ok.py", "X = 1\n")

    client = _Client(base)
    client.login("alice")
    assert client.get("/api/file?path=../../accounts.db")[0] == 400
