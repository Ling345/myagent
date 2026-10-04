"""网页上的数据导出与注销（离线）。

这些用例的重点不是"接口返回 200"，而是**删完真的不在了**：
库里的记录没了、磁盘上的文件也没了、账号登不上了。
"""

from __future__ import annotations

import http.cookiejar
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile
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

    def _open(self, request: urllib.request.Request) -> tuple[int, bytes, str]:
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.status, response.read(), response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as error:
            return error.code, error.read(), ""

    def get_raw(self, path: str) -> tuple[int, bytes, str]:
        return self._open(urllib.request.Request(f"{self.base}{path}"))

    def get(self, path: str) -> tuple[int, dict]:
        status, body, _ = self.get_raw(path)
        return status, (json.loads(body.decode("utf-8")) if body.startswith(b"{") else {})

    def post(self, path: str, payload: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(payload or {}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        status, body, _ = self._open(request)
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


def _seed(tmp_path: Path, accounts: AccountStore, name: str, *, session: str, code: str) -> str:
    """给某个账号造点数据。"""
    account_id = accounts.get(name).id
    sessions = tmp_path / "sessions" / account_id
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{session}.json").write_text(json.dumps({"id": session}), encoding="utf-8")
    code_dir = tmp_path / "sandbox" / account_id
    code_dir.mkdir(parents=True, exist_ok=True)
    (code_dir / code).write_text("VALUE = 1\n", encoding="utf-8")
    return account_id


# ---------------------------------------------------------------- 导出


def test_export_returns_a_zip_with_my_data(web):
    base, accounts, tmp_path = web
    accounts.create("alice", "password123")
    _seed(tmp_path, accounts, "alice", session="chat-1", code="calc.py")

    client = _Client(base)
    client.login("alice")
    status, body, content_type = client.get_raw("/api/export")

    assert status == 200
    assert "zip" in content_type
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        names = set(archive.namelist())
    assert "README.txt" in names
    assert "sessions/chat-1.json" in names
    assert "code/calc.py" in names


def test_export_needs_login(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    assert _Client(base).get_raw("/api/export")[0] == 401


def test_export_does_not_contain_other_users(web):
    base, accounts, tmp_path = web
    accounts.create("alice", "password123")
    accounts.create("bob", "password123")
    _seed(tmp_path, accounts, "alice", session="a-chat", code="a.py")
    _seed(tmp_path, accounts, "bob", session="b-chat", code="b.py")

    client = _Client(base)
    client.login("alice")
    with zipfile.ZipFile(io.BytesIO(client.get_raw("/api/export")[1])) as archive:
        joined = "\n".join(archive.namelist())
    assert "a-chat" in joined
    assert "b-chat" not in joined


# ---------------------------------------------------------------- 清空代码目录


def test_purge_code_removes_files_but_keeps_sessions(web):
    base, accounts, tmp_path = web
    accounts.create("alice", "password123")
    account_id = _seed(tmp_path, accounts, "alice", session="chat-1", code="calc.py")

    client = _Client(base)
    client.login("alice")
    status, payload = client.post("/api/account/purge-code")
    assert status == 200
    assert payload["removed"] == 1
    assert not (tmp_path / "sandbox" / account_id).exists()
    assert (tmp_path / "sessions" / account_id / "chat-1.json").is_file()


def test_purge_code_needs_login(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    assert _Client(base).post("/api/account/purge-code")[0] == 401


# ---------------------------------------------------------------- 注销


def test_delete_account_requires_the_exact_username(web):
    """二次确认：用户名不对就不删。"""
    base, accounts, tmp_path = web
    accounts.create("alice", "password123")
    account_id = _seed(tmp_path, accounts, "alice", session="chat-1", code="calc.py")

    client = _Client(base)
    client.login("alice")
    for wrong in ("", "bob", "alic", "alice2"):
        status, payload = client.post("/api/account/delete", {"confirm": wrong})
        assert status == 400
        assert "用户名" in payload["error"]

    assert accounts.get("alice") is not None
    assert (tmp_path / "sandbox" / account_id / "calc.py").is_file()


def test_delete_account_tolerates_surrounding_whitespace(web):
    """粘贴用户名时带个空格很常见，没必要卡住——而且毫无歧义。"""
    base, accounts, _ = web
    accounts.create("alice", "password123")

    client = _Client(base)
    client.login("alice")
    status, payload = client.post("/api/account/delete", {"confirm": "  alice\n"})
    assert status == 200
    assert payload["deleted"]["account_removed"] is True


def test_delete_account_removes_everything(web):
    base, accounts, tmp_path = web
    accounts.create("alice", "password123")
    account_id = _seed(tmp_path, accounts, "alice", session="chat-1", code="calc.py")

    client = _Client(base)
    client.login("alice")
    status, payload = client.post("/api/account/delete", {"confirm": "alice"})
    assert status == 200
    assert payload["deleted"]["sessions"] == 1
    assert payload["deleted"]["code"] == 1
    assert payload["deleted"]["account_removed"] is True

    # 库里没了
    assert accounts.get("alice") is None
    # 磁盘上没了
    assert not (tmp_path / "sessions" / account_id).exists()
    assert not (tmp_path / "sandbox" / account_id).exists()
    # 登不上了
    assert _Client(base).login("alice") == 401


def test_delete_account_leaves_others_alone(web):
    base, accounts, tmp_path = web
    accounts.create("alice", "password123")
    accounts.create("bob", "password123")
    _seed(tmp_path, accounts, "alice", session="a", code="a.py")
    bob_id = _seed(tmp_path, accounts, "bob", session="b", code="b.py")

    client = _Client(base)
    client.login("alice")
    client.post("/api/account/delete", {"confirm": "alice"})

    assert accounts.get("bob") is not None
    assert (tmp_path / "sessions" / bob_id / "b.json").is_file()
    assert (tmp_path / "sandbox" / bob_id / "b.py").is_file()


def test_deleting_requires_login(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    assert _Client(base).post("/api/account/delete", {"confirm": "alice"})[0] == 401


# ---------------------------------------------------------------- 政策页面


def test_privacy_and_terms_are_publicly_readable(web):
    """没登录的人也有权知道你怎么处理他的数据——所以这两页不能要登录。"""
    base, _, _ = web
    client = _Client(base)

    status, body, content_type = client.get_raw("/privacy")
    assert status == 200
    assert "html" in content_type
    text = body.decode("utf-8")
    assert "隐私政策" in text
    # 运营方与联系方式是政策的必备项
    assert "吴咏翰" in text
    assert "824346648@qq.com" in text
    # 第三方披露：这是 AI 产品必须写清楚的一条
    assert "大语言模型服务商" in text

    status, body, _ = client.get_raw("/terms")
    assert status == 200
    assert "用户协议" in body.decode("utf-8")


def test_privacy_page_is_also_reachable_with_the_html_suffix(web):
    base, _, _ = web
    assert _Client(base).get_raw("/privacy.html")[0] == 200


def test_privacy_policy_explains_where_the_data_lives(web):
    """政策得说清楚"存多久"和"怎么删"，不能只有泛泛的承诺。"""
    _base, _, _ = web
    status, body, _ = _Client(_base).get_raw("/privacy")
    page = body.decode("utf-8")
    assert status == 200
    for topic in ("存多久", "保留到你主动删除", "导出", "注销账号", "AGENT_RETENTION_DAYS"):
        assert topic in page, f"隐私政策里应该讲到 {topic}"
