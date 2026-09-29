"""运行注册表与断线续传（全部离线）。"""

from __future__ import annotations

import http.cookiejar
import http.client
import json
import queue
import threading
import time
import urllib.error
import urllib.request

import pytest

from agentcode.accounts import AccountStore
from agentcode.web.runs import RunRegistry
from agentcode.web.server import create_server


def _queue_stream(box: "queue.Queue"):
    """用队列驱动的事件流：测试想什么时候产事件就什么时候产。"""

    def make_stream():
        while True:
            item = box.get()
            if item is None:
                return
            yield item

    return make_stream


def _steps(count: int):
    return [{"type": "step", "data": {"i": index}} for index in range(count)]


# ------------------------------------------------------------------ 注册表


def test_subscribe_replays_everything_when_already_finished():
    box: queue.Queue = queue.Queue()
    registry = RunRegistry()
    record = registry.start(_queue_stream(box), agent="react", task="t")
    for event in _steps(3):
        box.put(event)
    box.put(None)
    record.thread.join(timeout=5)

    seen = list(registry.subscribe(record, 0))
    assert [event["data"]["i"] for event in seen] == [0, 1, 2]


def test_subscribe_from_index_only_replays_the_tail():
    """断线重连的核心：给个下标，只补后面漏掉的。"""
    box: queue.Queue = queue.Queue()
    registry = RunRegistry()
    record = registry.start(_queue_stream(box), agent="react", task="t")
    for event in _steps(4):
        box.put(event)
    box.put(None)
    record.thread.join(timeout=5)

    seen = list(registry.subscribe(record, 2))
    assert [event["data"]["i"] for event in seen] == [2, 3]


def test_run_keeps_going_after_the_subscriber_leaves():
    """浏览器断开只是少了个订阅者，任务本身照跑。"""
    box: queue.Queue = queue.Queue()
    registry = RunRegistry()
    record = registry.start(_queue_stream(box), agent="react", task="t")

    box.put({"type": "status", "data": {"message": "开始"}})
    stream = registry.subscribe(record, 0)
    assert next(stream)["type"] == "status"
    stream.close()  # 模拟浏览器把连接掐了

    box.put({"type": "answer", "data": {"answer": "跑完了"}})
    box.put(None)
    record.thread.join(timeout=5)

    assert record.finished is True
    assert [event["type"] for event in record.events] == ["status", "answer"]


def test_finish_hook_runs_even_without_subscribers():
    """用量结算挂在收尾钩子上：没人在看也要扣额度、放闸门。"""
    box: queue.Queue = queue.Queue()
    registry = RunRegistry()
    seen: list[str] = []
    record = registry.start(
        _queue_stream(box), agent="react", task="t", on_finish=lambda rec: seen.append(rec.id)
    )
    box.put({"type": "answer", "data": {"answer": "好了"}})
    box.put(None)
    record.thread.join(timeout=5)

    assert seen == [record.id]
    assert record.answer_event()["data"]["answer"] == "好了"


def test_old_events_are_dropped_with_a_gap_notice():
    """缓冲区有上限；丢了老事件必须告诉订阅者，否则下标会串位。"""
    box: queue.Queue = queue.Queue()
    registry = RunRegistry(event_limit=3)
    record = registry.start(_queue_stream(box), agent="react", task="t")
    for event in _steps(5):
        box.put(event)
    box.put(None)
    record.thread.join(timeout=5)

    seen = list(registry.subscribe(record, 0))
    assert seen[0] == {"type": "truncated", "data": {"skipped": 2, "from": 2}}
    assert [event["data"]["i"] for event in seen[1:]] == [2, 3, 4]


def test_stream_errors_become_error_events():
    def boom():
        yield {"type": "status", "data": {"message": "开始"}}
        raise RuntimeError("内部炸了")

    registry = RunRegistry()
    record = registry.start(boom, agent="react", task="t")
    record.thread.join(timeout=5)

    assert record.finished is True
    assert record.events[-1]["type"] == "error"
    assert "内部炸了" in record.events[-1]["data"]["message"]


def test_finished_runs_are_pruned_after_ttl():
    registry = RunRegistry(ttl_seconds=1.0)
    box: queue.Queue = queue.Queue()
    old = registry.start(_queue_stream(box), agent="react", task="旧任务")
    box.put(None)
    old.thread.join(timeout=5)
    old.finished_at = time.monotonic() - 10  # 假装已经放很久了

    other: queue.Queue = queue.Queue()
    registry.start(_queue_stream(other), agent="react", task="新任务")
    assert registry.get(old.id) is None


def test_registry_size_is_bounded():
    registry = RunRegistry(max_runs=3)
    for index in range(6):
        box: queue.Queue = queue.Queue()
        box.put(None)
        record = registry.start(_queue_stream(box), agent="react", task=f"t{index}")
        record.thread.join(timeout=5)
    assert len(registry.list(limit=100)) <= 3


def test_running_for_session_finds_the_unfinished_one():
    registry = RunRegistry()
    box: queue.Queue = queue.Queue()
    record = registry.start(_queue_stream(box), agent="react", task="t", session_id="s1")

    assert registry.running_for_session("s1").id == record.id
    assert registry.running_for_session("s2") is None
    assert registry.running_for_session(None) is None

    box.put(None)
    record.thread.join(timeout=5)
    assert registry.running_for_session("s1") is None


# ------------------------------------------------------------------ HTTP


@pytest.fixture
def web() -> str:
    """免登录的网页服务，账号库与会话目录都隔离到临时目录。"""
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


def _post_sse(url: str, payload: dict) -> list[tuple[str, dict]]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return _parse_sse(response.read().decode("utf-8"))


def _get_sse(url: str) -> list[tuple[str, dict]]:
    with urllib.request.urlopen(url, timeout=60) as response:
        return _parse_sse(response.read().decode("utf-8"))


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    """把 SSE 文本拆成 (事件类型, 载荷) 列表。"""
    events: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        kind = "message"
        data: dict | None = None
        for line in block.split("\n"):
            if line.startswith("event:"):
                kind = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if data is not None:
            events.append((kind, data))
    return events


def test_run_is_announced_and_can_be_replayed(web):
    events = _post_sse(
        f"{web}/api/run",
        {"agent": "react", "task": "北京天气如何", "llm": "mock", "session_id": "resume-1"},
    )
    # 首帧必须是 run：前端要拿到编号，后面才有得续
    assert events[0][0] == "run"
    run_id = events[0][1]["id"]
    assert any(kind == "answer" for kind, _ in events)

    # 断线后从第 1 条事件续上：应该正好补回剩下的
    resumed = _get_sse(f"{web}/api/run/stream?run_id={run_id}&from=1")
    assert resumed[0][0] == "run"
    assert [kind for kind, _ in resumed[1:]] == [kind for kind, _ in events[2:]]


def test_runs_endpoint_lists_the_run(web):
    _post_sse(
        f"{web}/api/run",
        {"agent": "react", "task": "北京天气如何", "llm": "mock", "session_id": "resume-2"},
    )
    payload = _get_json(f"{web}/api/runs?session_id=resume-2")
    assert payload["runs"], "应该有运行记录"
    listing = payload["runs"][0]
    assert listing["agent"] == "react"
    assert listing["status"] == "done"
    assert listing["event_count"] >= 1


def test_resume_reports_unknown_run(web):
    with pytest.raises(urllib.error.HTTPError) as info:
        _get_sse(f"{web}/api/run/stream?run_id=not-a-real-run&from=0")
    assert info.value.code == 404


def test_disconnecting_mid_run_does_not_lose_the_result(web):
    """最真实的那种断线：连着连着把连接掐了，任务必须照样跑完且结果拿得回来。"""
    host, port = web.removeprefix("http://").split(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=30)
    connection.request(
        "POST",
        "/api/run",
        body=json.dumps(
            {
                "agent": "react",
                "task": "北京天气如何",
                "llm": "mock",
                "session_id": "drop-1",
            }
        ),
        headers={"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    assert response.status == 200
    response.read(32)  # 只读一点点就掐断，模拟用户刷新页面
    connection.close()

    # 任务在服务端继续跑；等它结束
    run_id = None
    for _ in range(100):
        runs = _get_json(f"{web}/api/runs?session_id=drop-1")["runs"]
        if runs:
            run_id = runs[0]["id"]
            if runs[0]["status"] == "done":
                break
        time.sleep(0.1)
    assert run_id, "断线后运行记录应该还在"

    # 用编号把整段事件补回来
    replay = _get_sse(f"{web}/api/run/stream?run_id={run_id}&from=0")
    answers = [data for kind, data in replay if kind == "answer"]
    assert answers, "补回来的事件里必须有最终答案"
    assert answers[0]["answer"]

    # 结果也落进了会话记录，切回会话看得到
    detail = _get_json(f"{web}/api/session?id=drop-1")
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]


# ------------------------------------------------------- 运行记录也要按用户隔离


class _Client:
    """带 Cookie 的测试客户端。"""

    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def post_json(self, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return response.status, (json.loads(body) if body.startswith("{") else {})
        except urllib.error.HTTPError as error:
            return error.code, {}

    def post_sse(self, path: str, payload: dict) -> list[tuple[str, dict]]:
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.opener.open(request, timeout=60) as response:
            return _parse_sse(response.read().decode("utf-8"))

    def get(self, path: str) -> tuple[int, bytes, str]:
        try:
            with self.opener.open(f"{self.base}{path}", timeout=30) as response:
                return response.status, response.read(), response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as error:
            return error.code, error.read(), ""


def test_runs_are_private_to_their_owner(tmp_path):
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
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        for name in ("alice", "bob"):
            accounts.create(name, "password123")

        alice = _Client(base)
        assert alice.post_json("/api/login", {"name": "alice", "password": "password123"})[0] == 200
        events = alice.post_sse(
            "/api/run",
            {"agent": "react", "task": "北京天气如何", "llm": "mock", "session_id": "alice-1"},
        )
        run_id = events[0][1]["id"]

        bob = _Client(base)
        assert bob.post_json("/api/login", {"name": "bob", "password": "password123"})[0] == 200
        status, _, _ = bob.get(f"/api/run/stream?run_id={run_id}&from=0")
        assert status == 403

        assert bob.get("/api/runs?session_id=alice-1")[0] == 200
        listing = json.loads(bob.get("/api/runs?session_id=alice-1")[1].decode("utf-8"))
        assert listing["runs"] == []  # 别人的运行不出现在我的列表里
    finally:
        server.shutdown()
        server.server_close()
