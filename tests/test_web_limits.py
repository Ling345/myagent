"""网页端的限流、并发与预算（真实 HTTP）。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from agentcode.web.server import create_server


def _call(base: str, path: str, payload: dict | None = None) -> tuple[int, dict, dict]:
    """发请求，返回 (状态码, JSON, 响应头)。"""
    def parse(body: str, headers) -> dict:
        """只解析 JSON 响应；SSE 流（例如 /api/run）不解析。"""
        if "application/json" not in (headers.get("Content-Type") or ""):
            return {}
        try:
            return json.loads(body or "{}")
        except json.JSONDecodeError:
            return {}

    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base}{path}",
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            headers = dict(response.headers)
            return response.status, parse(body, headers), headers
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8")
        headers = dict(error.headers)
        return error.code, parse(body, headers), headers


@pytest.fixture
def make_server(tmp_path):
    """创建免登录服务，返回 (地址, 服务对象) 以便直接操作闸门。"""
    servers = []

    def factory(**kwargs) -> tuple[str, object]:
        server = create_server(
            host="127.0.0.1",
            port=0,
            llm_mode="mock",
            quiet=True,
            require_auth=False,
            session_dir=str(tmp_path / "sessions"),
            **kwargs,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}", server

    yield factory
    for server in servers:
        server.shutdown()
        server.server_close()


def test_rate_limit_returns_429(make_server):
    base, _ = make_server(rate_limit_per_minute=2)

    assert _call(base, "/api/sessions")[0] == 200
    assert _call(base, "/api/sessions")[0] == 200
    status, payload, headers = _call(base, "/api/sessions")

    assert status == 429
    assert "过于频繁" in payload["error"]
    assert int(headers.get("Retry-After", "0")) >= 1


def test_concurrency_limit_returns_429(make_server):
    """已有一个任务在跑时，第二个请求应当被挡住。"""
    base, server = make_server(rate_limit_per_minute=100, max_concurrent_runs=1)
    key = "ip:127.0.0.1"

    assert server.guard.acquire(key).allowed is True  # 模拟第一个任务占用名额
    try:
        status, payload, _ = _call(
            base, "/api/run", {"agent": "echo", "task": "你好", "session_id": "s1"}
        )
        assert status == 429
        assert "同时最多 1 个" in payload["error"]
    finally:
        server.guard.release(key)

    # 名额释放后同一个请求应当恢复正常
    assert _call(base, "/api/run", {"agent": "echo", "task": "你好", "session_id": "s1"})[0] == 200


def test_login_is_rate_limited(make_server):
    base, _ = make_server(rate_limit_per_minute=100)
    codes = [
        _call(base, "/api/login", {"name": "nobody", "password": "wrong-password"})[0]
        for _ in range(11)
    ]
    assert codes[:10] == [401] * 10  # 前 10 次正常走鉴权流程
    assert codes[10] == 429  # 第 11 次被登录限流挡住
