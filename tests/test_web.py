"""网页服务测试：全部使用离线 mock 模型，不产生网络请求。"""

from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.request

import pytest

from agentcode.cli import main
from agentcode.web.runner import run_stream
from agentcode.web.server import create_server, is_port_open


@pytest.fixture
def web_base() -> str:
    """在随机端口启动一个只服务本机的网页服务。"""
    server = create_server(
        host="127.0.0.1", port=0, llm_mode="mock", quiet=True, require_auth=False
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _host_port(web_base: str) -> tuple[str, int]:
    host, port = web_base.removeprefix("http://").split(":")
    return host, int(port)


def _get(url: str) -> tuple[int, str, bytes]:
    """发起 GET 请求，返回状态码、Content-Type 与原始字节。"""
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.status, response.headers.get("Content-Type", ""), response.read()


def _post(url: str, payload: dict) -> tuple[int, str, bytes]:
    """发起 POST 请求，返回状态码、Content-Type 与原始字节。"""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.status, response.headers.get("Content-Type", ""), response.read()


# ------------------------------------------------------------------ 运行器


def test_run_stream_emits_step_events_then_answer():
    events = list(run_stream("react", "北京天气如何", llm_mode="mock"))
    types = [event["type"] for event in events]

    assert types[-1] == "answer"
    assert "step" in types
    steps = [event["data"] for event in events if event["type"] == "step"]
    assert steps[0]["tool"] == "get_weather"
    assert events[-1]["data"]["answer"].startswith("北京当前晴")


def test_run_stream_emits_status_events():
    events = list(run_stream("react", "北京天气如何", llm_mode="mock"))
    statuses = [event["data"]["message"] for event in events if event["type"] == "status"]
    assert any("模型" in message for message in statuses)
    assert any("get_weather" in message for message in statuses)


def test_run_stream_reports_unknown_agent_as_error_event():
    events = list(run_stream("不存在的智能体", "你好", llm_mode="mock"))
    assert events[-1]["type"] == "error"
    assert "未找到名为" in events[-1]["data"]["message"]


# -------------------------------------------------------------------- 接口


def test_agents_endpoint_lists_builtin_agents(web_base):
    status, content_type, body = _get(f"{web_base}/api/agents")
    payload = json.loads(body)
    names = [agent["name"] for agent in payload["agents"]]

    assert status == 200
    assert "application/json" in content_type
    assert {"react", "coding", "plan_and_solve", "reflection"} <= set(names)
    assert any(tool["name"] == "get_weather" for tool in payload["tools"]["mock"])
    assert any(tool["name"] == "web_search" for tool in payload["tools"]["real"])
    assert any(tool["name"] == "run_python" for tool in payload["tools"]["real"])


def test_config_endpoint_masks_api_key(web_base, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-1234567890abcdef")
    monkeypatch.setenv("LLM_MODEL_ID", "demo-model")
    status, _, body = _get(f"{web_base}/api/config")
    payload = json.loads(body)

    assert status == 200
    assert payload["model"] == "demo-model"
    assert "sk-1234567890abcdef" not in body.decode("utf-8")


def test_index_page_shows_result_only(web_base):
    status, content_type, body = _get(f"{web_base}/")
    html = body.decode("utf-8")

    assert status == 200
    assert "text/html" in content_type
    assert "AgentCode" in html
    assert "新会话" in html
    # 页面上不再有任何推理过程的入口
    assert "推理过程" not in html
    assert "思考" not in html


def test_static_stylesheet_is_served(web_base):
    status, content_type, body = _get(f"{web_base}/static/style.css")
    assert status == 200
    assert "text/css" in content_type
    assert b"--accent" in body


def test_static_path_traversal_is_blocked(web_base):
    host, port = _host_port(web_base)
    connection = http.client.HTTPConnection(host, port, timeout=10)
    try:
        connection.request("GET", "/static/../agentcode/config.py")
        response = connection.getresponse()
        assert response.status == 404
    finally:
        connection.close()


def test_run_endpoint_streams_sse(web_base):
    status, content_type, body = _post(
        f"{web_base}/api/run",
        {"agent": "react", "task": "北京天气如何", "llm": "mock"},
    )
    text = body.decode("utf-8")

    assert status == 200
    assert "text/event-stream" in content_type
    assert "event: step" in text
    assert "event: answer" in text
    assert "get_weather" in text


def test_run_endpoint_rejects_empty_task(web_base):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(f"{web_base}/api/run", {"agent": "react", "task": "   ", "llm": "mock"})
    assert excinfo.value.code == 400
    assert "任务" in excinfo.value.read().decode("utf-8")


def test_unknown_route_returns_404(web_base):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(f"{web_base}/nope")
    assert excinfo.value.code == 404


# ---------------------------------------------------------------- 网站入口


def test_is_port_open_detects_running_server(web_base):
    host, port = _host_port(web_base)
    assert is_port_open(host, port) is True
    assert is_port_open(host, 1) is False


def test_open_command_reuses_running_server(web_base, capsys):
    host, port = _host_port(web_base)
    code = main(["open", "--host", host, "--port", str(port), "--no-browser"])
    output = capsys.readouterr().out

    assert code == 0
    assert "服务已经在运行" in output
