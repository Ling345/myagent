"""指标端点与采集点（离线）。"""

from __future__ import annotations

import http.cookiejar
import json
import threading
import urllib.error
import urllib.request

import pytest

from agentcode.accounts import AccountStore
from agentcode.metrics import METRICS
from agentcode.web.server import create_server, is_loopback_host


@pytest.fixture(autouse=True)
def _clean_metrics():
    """指标是进程内全局的，用例之间必须清干净。"""
    METRICS.reset()
    yield
    METRICS.reset()


class _Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def _call(self, path: str, payload: dict | None) -> tuple[int, bytes, str]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method="POST" if data is not None else "GET",
        )
        try:
            with self.opener.open(request, timeout=60) as response:
                return response.status, response.read(), response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as error:
            return error.code, error.read(), ""

    def get(self, path: str) -> tuple[int, bytes, str]:
        return self._call(path, None)

    def post(self, path: str, payload: dict) -> tuple[int, bytes, str]:
        return self._call(path, payload)

    def login(self, name: str, password: str = "password123") -> int:
        return self.post("/api/login", {"name": name, "password": password})[0]


def _server(tmp_path, **extra):
    accounts = AccountStore(tmp_path / "accounts.db")
    server = create_server(
        host="127.0.0.1",
        port=0,
        llm_mode="mock",
        quiet=True,
        session_dir=str(tmp_path / "sessions"),
        accounts=accounts,
        **extra,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, accounts, f"http://127.0.0.1:{server.server_port}"


# ---------------------------------------------------------------- 回环判断


def test_is_loopback_host():
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("::1") is True
    assert is_loopback_host("0.0.0.0") is False
    assert is_loopback_host("10.0.0.7") is False
    assert is_loopback_host("example.com") is False


# ---------------------------------------------------------------- 端点本身


def test_metrics_endpoint_is_public_on_loopback(tmp_path):
    """绑在 127.0.0.1 时不用登录就能 curl——本地自用要方便。"""
    server, accounts, base = _server(tmp_path)
    accounts.create("alice", "password123")
    try:
        status, body, content_type = _Client(base).get("/metrics")
        assert status == 200
        assert "text/plain" in content_type
        assert b"# TYPE agentcode_http_requests_total counter" in body
    finally:
        server.shutdown()
        server.server_close()


def test_metrics_endpoint_requires_login_when_not_loopback(tmp_path):
    """监听到别的地址上就必须登录：指标里有路径、账号名和 key 指纹。"""
    server, accounts, base = _server(tmp_path)
    accounts.create("alice", "password123")
    server.bind_host = "0.0.0.0"  # type: ignore[attr-defined]
    try:
        status, body, _ = _Client(base).get("/metrics")
        assert status == 401
        assert b"agentcode_http_requests_total" not in body
    finally:
        server.shutdown()
        server.server_close()


def test_metrics_endpoint_works_after_login_when_not_loopback(tmp_path):
    server, accounts, base = _server(tmp_path)
    accounts.create("alice", "password123")
    server.bind_host = "0.0.0.0"  # type: ignore[attr-defined]
    try:
        client = _Client(base)
        assert client.login("alice") == 200
        status, body, _ = client.get("/metrics")
        assert status == 200
        assert b"agentcode_http_requests_total" in body
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- 采集点


def test_http_requests_are_counted_by_path_and_status(tmp_path):
    server, accounts, base = _server(tmp_path, require_auth=False)
    try:
        client = _Client(base)
        client.get("/api/agents")
        client.get("/api/agents")
        client.get("/api/config")

        _, body, _ = client.get("/metrics")
        text = body.decode("utf-8")
        assert 'agentcode_http_requests_total{path="/api/agents",status="200"} 2.0' in text
        assert 'agentcode_http_requests_total{path="/api/config",status="200"} 1.0' in text
    finally:
        server.shutdown()
        server.server_close()


def test_unknown_paths_do_not_explode_label_cardinality(tmp_path):
    """有人拿随机 URL 扫站时，标签不能无限膨胀。"""
    server, accounts, base = _server(tmp_path, require_auth=False)
    try:
        client = _Client(base)
        for index in range(30):
            client.get(f"/random-{index}")

        _, body, _ = client.get("/metrics")
        text = body.decode("utf-8")
        assert 'agentcode_http_requests_total{path="other",status="404"} 30.0' in text
        assert "/random-0" not in text
    finally:
        server.shutdown()
        server.server_close()


def test_static_requests_are_bucketed_together(tmp_path):
    server, accounts, base = _server(tmp_path, require_auth=False)
    try:
        client = _Client(base)
        client.get("/static/app.js")
        client.get("/static/style.css")

        _, body, _ = client.get("/metrics")
        text = body.decode("utf-8")
        assert 'agentcode_http_requests_total{path="/static",status="200"} 2.0' in text
    finally:
        server.shutdown()
        server.server_close()


def test_request_duration_is_observed(tmp_path):
    server, accounts, base = _server(tmp_path, require_auth=False)
    try:
        client = _Client(base)
        client.get("/api/agents")
        _, body, _ = client.get("/metrics")
        text = body.decode("utf-8")
        assert 'agentcode_http_request_seconds_count{path="/api/agents"} 1' in text
    finally:
        server.shutdown()
        server.server_close()


def test_quota_rejection_is_counted(tmp_path):
    server, accounts, base = _server(tmp_path)
    account = accounts.create("alice", "password123", daily_token_limit=10)
    accounts.record_usage(account.id, 10)
    try:
        client = _Client(base)
        client.login("alice")
        assert client.post("/api/run", {"agent": "react", "task": "北京天气如何", "llm": "mock"})[0] == 402

        _, body, _ = client.get("/metrics")
        text = body.decode("utf-8")
        assert 'agentcode_rejections_total{reason="quota"} 1.0' in text
    finally:
        server.shutdown()
        server.server_close()


def test_login_failure_is_counted(tmp_path):
    server, accounts, base = _server(tmp_path)
    accounts.create("alice", "password123")
    try:
        client = _Client(base)
        assert client.login("alice", "wrong-password") == 401
        _, body, _ = client.get("/metrics")
        text = body.decode("utf-8")
        # 登录失败单独记一个原因：这是暴力试探的信号，跟"没登录"不是一回事
        assert 'agentcode_rejections_total{reason="bad_credentials"} 1.0' in text
    finally:
        server.shutdown()
        server.server_close()
