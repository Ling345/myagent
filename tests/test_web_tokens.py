"""网页上的 API 令牌管理。"""

from __future__ import annotations

import http.cookiejar
import json
import threading
import urllib.error
import urllib.request

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

    def api(self, path: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
        """带令牌调对外接口；``payload=None`` 就是 GET。"""
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST" if data is not None else "GET",
        )
        status, body = self._open(request)
        return status, (json.loads(body.decode("utf-8")) if body.startswith(b"{") else {})


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CODE_ROOT", str(tmp_path / "sandbox"))
    monkeypatch.setenv("AGENT_WEB_SESSION_DIR", str(tmp_path / "sessions"))
    accounts = AccountStore(tmp_path / "accounts.db")
    accounts.create("alice", "password123")
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
        yield base, accounts, _Client(base)
    finally:
        server.shutdown()
        server.server_close()


def test_create_list_and_revoke_a_token(web):
    base, accounts, client = web
    client.login("alice")

    status, created = client.post("/api/tokens/create", {"name": "我的脚本"})
    assert status == 200
    plaintext = created["token"]
    assert plaintext.startswith("agk_")
    assert created["record"]["name"] == "我的脚本"

    # 列表里能看见这把（但只有前缀，没有明文）
    status, listed = client.get("/api/tokens")
    assert status == 200
    assert len(listed["tokens"]) == 1
    assert listed["tokens"][0]["prefix"] == plaintext[: len(listed["tokens"][0]["prefix"])]
    assert plaintext not in json.dumps(listed, ensure_ascii=False)

    # 这把令牌真的能用
    assert client.api("/v1/me", plaintext)[0] == 200

    # 吊销之后立刻不能用了
    token_id = listed["tokens"][0]["id"]
    assert client.post("/api/tokens/revoke", {"id": token_id})[1]["revoked"] is True
    assert client.api("/v1/me", plaintext)[0] == 401


def test_token_management_needs_login(web):
    base, _, client = web
    assert client.get("/api/tokens")[0] == 401
    assert client.post("/api/tokens/create", {"name": "x"})[0] == 401
    assert client.post("/api/tokens/revoke", {"id": "x"})[0] == 401


def test_one_account_cannot_see_or_revoke_anothers_tokens(web):
    base, accounts, client = web
    accounts.create("bob", "password123")
    alice = _Client(base)
    alice.login("alice")
    plaintext = alice.post("/api/tokens/create", {"name": "alice 的"})[1]["token"]
    token_id = alice.get("/api/tokens")[1]["tokens"][0]["id"]

    bob = _Client(base)
    bob.login("bob")

    assert bob.get("/api/tokens")[1]["tokens"] == []
    assert bob.post("/api/tokens/revoke", {"id": token_id})[1]["revoked"] is False
    assert alice.api("/v1/me", plaintext)[0] == 200  # alice 的还能用


def test_token_actions_are_audited_without_the_plaintext(web):
    base, accounts, client = web
    client.login("alice")
    plaintext = client.post("/api/tokens/create", {"name": "我的脚本"})[1]["token"]
    token_id = client.get("/api/tokens")[1]["tokens"][0]["id"]
    client.post("/api/tokens/revoke", {"id": token_id})

    actions = [item["action"] for item in accounts.audit_entries(limit=20)]
    assert "api_token.create" in actions
    assert "api_token.revoke" in actions
    assert plaintext not in json.dumps(accounts.audit_entries(), ensure_ascii=False)
    create_entry = next(
        item for item in accounts.audit_entries() if item["action"] == "api_token.create"
    )
    assert create_entry["target"] == "我的脚本"
    assert create_entry["detail"]["prefix"] == plaintext[: len(create_entry["detail"]["prefix"])]


def test_revoking_an_unknown_token_reports_false(web):
    base, _, client = web
    client.login("alice")
    assert client.post("/api/tokens/revoke", {"id": "查无此令牌"})[1]["revoked"] is False
