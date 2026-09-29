"""文件上传（离线）。

设计：`POST /api/upload?path=名字`，请求体就是文件的原始文本。
不走 multipart——Python 3.13 已经把 `cgi` 删了，而我们要的只是纯文本，
原始 body 最省事也最不容易出差错。
"""

from __future__ import annotations

import http.cookiejar
import json
import threading
import urllib.error
import urllib.parse
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

    def _open(self, request: urllib.request.Request) -> tuple[int, dict]:
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return response.status, (json.loads(body) if body.startswith("{") else {})
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8")
            return error.code, (json.loads(body) if body.startswith("{") else {})

    def get(self, path: str) -> tuple[int, dict]:
        return self._open(urllib.request.Request(f"{self.base}{path}"))

    def post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        return self._open(request)

    def upload(self, filename: str, content: bytes) -> tuple[int, dict]:
        """按原始 body 上传一个文件。"""
        query = urllib.parse.urlencode({"path": filename})
        request = urllib.request.Request(
            f"{self.base}/api/upload?{query}",
            data=content,
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        return self._open(request)

    def login(self, name: str, password: str = "password123") -> int:
        return self.post_json("/api/login", {"name": name, "password": password})[0]


@pytest.fixture
def web(tmp_path, monkeypatch):
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


# ---------------------------------------------------------------- 正常路径


def test_upload_stores_the_file_in_the_users_own_directory(web):
    base, accounts, code_root = web
    accounts.create("alice", "password123")
    client = _Client(base)
    client.login("alice")

    status, payload = client.upload("calc.py", b"def add(a, b):\n    return a + b\n")
    assert status == 200
    assert payload["path"] == "calc.py"
    assert payload["bytes"] == 32
    assert payload["overwritten"] is False

    stored = code_root / accounts.get("alice").id / "calc.py"
    assert stored.is_file()
    assert "return a + b" in stored.read_text(encoding="utf-8")


def test_uploaded_file_is_readable_through_the_file_endpoint(web):
    """传完就能直接说"给这个文件生成测试"——agent 的 read_file 就在那儿。"""
    base, accounts, _ = web
    accounts.create("alice", "password123")
    client = _Client(base)
    client.login("alice")
    client.upload("calc.py", b"VALUE = 42\n")

    status, payload = client.get("/api/file?path=calc.py")
    assert status == 200
    assert "VALUE = 42" in payload["content"]


def test_reuploading_the_same_name_overwrites(web):
    base, accounts, code_root = web
    accounts.create("alice", "password123")
    client = _Client(base)
    client.login("alice")

    client.upload("calc.py", b"OLD = 1\n")
    status, payload = client.upload("calc.py", b"NEW = 2\n")
    assert status == 200
    assert payload["overwritten"] is True

    stored = code_root / accounts.get("alice").id / "calc.py"
    assert stored.read_text(encoding="utf-8") == "NEW = 2\n"


def test_upload_into_a_subdirectory(web):
    base, accounts, code_root = web
    accounts.create("alice", "password123")
    client = _Client(base)
    client.login("alice")

    status, payload = client.upload("pkg/mod.py", b"X = 1\n")
    assert status == 200
    assert payload["path"] == "mod.py"  # 只取文件名，目录由服务端决定
    assert (code_root / accounts.get("alice").id / "mod.py").is_file()


# ---------------------------------------------------------------- 隔离


def test_uploaded_file_is_private_to_its_owner(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    accounts.create("bob", "password123")

    alice = _Client(base)
    alice.login("alice")
    alice.upload("secret.py", b"SECRET = 1\n")

    bob = _Client(base)
    bob.login("bob")
    assert bob.get("/api/file?path=secret.py")[0] == 404


def test_upload_requires_login(tmp_path, monkeypatch):
    accounts = AccountStore(tmp_path / "accounts.db")
    accounts.create("alice", "password123")
    monkeypatch.setenv("AGENT_CODE_ROOT", str(tmp_path / "sandbox"))
    server = create_server(
        host="127.0.0.1",
        port=0,
        llm_mode="mock",
        quiet=True,
        session_dir=str(tmp_path / "sessions"),
        accounts=accounts,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = _Client(f"http://127.0.0.1:{server.server_port}")
        assert client.upload("x.py", b"X = 1\n")[0] == 401
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- 拒绝的输入


def test_oversized_file_is_rejected(web, monkeypatch):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    monkeypatch.setenv("AGENT_UPLOAD_MAX_BYTES", "100")
    client = _Client(base)
    client.login("alice")

    status, payload = client.upload("big.py", b"x" * 200)
    assert status == 413
    assert "太大" in payload["error"]


def test_non_utf8_file_is_rejected(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    client = _Client(base)
    client.login("alice")

    status, payload = client.upload("bin.dat", b"\xff\xfe\x00\x01")
    assert status == 400
    assert "文本" in payload["error"] or "二进制" in payload["error"]


def test_file_with_nul_bytes_is_treated_as_binary(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    client = _Client(base)
    client.login("alice")

    status, payload = client.upload("weird.py", b"X = 1\x00\n")
    assert status == 400
    assert "二进制" in payload["error"]


def test_empty_file_is_rejected(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    client = _Client(base)
    client.login("alice")
    assert client.upload("empty.py", b"")[0] == 400


def test_dotdot_filename_is_rejected(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    client = _Client(base)
    client.login("alice")

    for name in ("..", ".", "  "):
        assert client.upload(name, b"X = 1\n")[0] == 400


def test_absolute_path_is_reduced_to_a_filename(web):
    """不能靠绝对路径写到别处去。"""
    base, accounts, code_root = web
    accounts.create("alice", "password123")
    client = _Client(base)
    client.login("alice")

    status, payload = client.upload("C:\\Windows\\evil.py", b"X = 1\n")
    assert status == 200
    assert payload["path"] == "evil.py"
    assert (code_root / accounts.get("alice").id / "evil.py").is_file()


def test_quota_is_enforced(web, monkeypatch):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    monkeypatch.setenv("AGENT_UPLOAD_QUOTA_BYTES", "150")
    client = _Client(base)
    client.login("alice")

    assert client.upload("a.py", b"x" * 100)[0] == 200
    status, payload = client.upload("b.py", b"x" * 100)
    assert status == 413
    assert "上限" in payload["error"]


def test_quota_allows_overwriting_an_existing_file(web, monkeypatch):
    """覆盖不算新增占用，否则用户连自己那个文件都改不了。"""
    base, accounts, _ = web
    accounts.create("alice", "password123")
    monkeypatch.setenv("AGENT_UPLOAD_QUOTA_BYTES", "150")
    client = _Client(base)
    client.login("alice")

    assert client.upload("a.py", b"x" * 100)[0] == 200
    assert client.upload("a.py", b"y" * 140)[0] == 200


def test_unknown_path_is_still_404(web):
    base, accounts, _ = web
    accounts.create("alice", "password123")
    client = _Client(base)
    client.login("alice")
    assert client.post_json("/api/nope", {})[0] == 404
