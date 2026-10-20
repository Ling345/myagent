"""运行追踪页：静态页面 + 数据来源（离线）。"""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from agentcode.web.server import STATIC_DIR, create_server


@pytest.fixture
def web_base() -> str:
    server = create_server(
        host="127.0.0.1", port=0, llm_mode="mock", quiet=True, require_auth=False
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_trace_page_is_served(web_base):
    with urllib.request.urlopen(f"{web_base}/trace", timeout=30) as response:
        html = response.read().decode("utf-8")

    assert response.status == 200
    assert "运行追踪" in html
    assert "/static/trace.js" in html
    # 页面自己要讲清楚：这是排查视图，不是普通对话页
    assert "排查视图" in html


def test_trace_script_uses_the_existing_run_endpoints():
    script = (STATIC_DIR / "trace.js").read_text(encoding="utf-8")

    assert "/api/runs" in script
    assert "/api/run/stream?run_id=" in script
    assert "EventSource" in script
    assert "status" in script and "step" in script and "answer" in script


def test_trace_page_has_its_own_styles():
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")

    for selector in (".tr-layout", ".tr-step", ".tr-observation", ".tr-answer"):
        assert selector in css


def test_trace_page_is_linked_from_the_sidebar():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert 'href="/trace"' in html
    assert "运行追踪" in html


def test_the_web_run_stream_replays_events_for_the_trace_page(web_base):
    """追踪页靠 /api/run/stream 的"从 0 重放"拿完整轨迹。"""
    request = urllib.request.Request(
        f"{web_base}/api/run",
        data=json.dumps({"agent": "echo", "task": "你好", "llm": "mock"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()  # 先把这次运行跑完

    with urllib.request.urlopen(f"{web_base}/api/runs", timeout=30) as response:
        run_id = json.loads(response.read().decode("utf-8"))["runs"][0]["id"]

    with urllib.request.urlopen(
        f"{web_base}/api/run/stream?run_id={run_id}&from=0", timeout=30
    ) as response:
        body = response.read().decode("utf-8")

    assert "event: run" in body
    assert "event: step" in body
    assert "event: answer" in body
