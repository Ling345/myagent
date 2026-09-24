"""多会话：保留、命名、切换与磁盘持久化。"""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from agentcode.agents import EchoAgent, ReActAgent
from agentcode.llm import ScriptedLLM
from agentcode.tools import ToolRegistry
from agentcode.web.server import create_server
from agentcode.web.sessions import DEFAULT_SESSION_NAME, SessionStore


def _agent() -> ReActAgent:
    tools = ToolRegistry()
    tools.register_tool("get_weather", "查天气", lambda city: f"{city}晴")
    return ReActAgent(llm=ScriptedLLM([]), tools=tools)


# ------------------------------------------------------------------ 仓库


def test_create_and_list_sessions(tmp_path):
    store = SessionStore(tmp_path)
    first = store.create(name="北京游")
    store.append_message(first.id, "user", "北京怎么玩")
    second = store.create()
    store.append_message(second.id, "user", "上海怎么玩")

    listed = store.list()
    names = [item["name"] for item in listed]

    assert first.id != second.id
    assert "北京游" in names
    assert "上海怎么玩" in names  # 空名会话按首条提问自动命名
    assert set(listed[0]) >= {"id", "name", "created_at", "updated_at", "message_count"}


def test_rename_session(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()

    assert store.rename(session.id, "  上海行程  ") is True
    assert store.record(session.id).name == "上海行程"
    assert store.rename("不存在", "x") is False


def test_delete_session(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    assert store.delete(session.id) is True
    assert store.delete(session.id) is False
    assert store.record(session.id) is None


def test_auto_names_session_from_first_message(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()

    store.append_message(session.id, "user", "帮我查一下北京今天的天气，再推荐一个景点")
    assert store.record(session.id).name.startswith("帮我查一下北京")


def test_manual_name_is_not_overwritten(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create(name="我的会话")
    store.append_message(session.id, "user", "随便问问")
    assert store.record(session.id).name == "我的会话"


def test_messages_are_recorded_in_order(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    store.append_message(session.id, "user", "你好")
    store.append_message(session.id, "assistant", "你好呀")

    messages = store.messages(session.id)
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "你好呀"


def test_sessions_persist_across_store_instances(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create(name="持久化")
    store.append_message(session.id, "user", "记住这句话")

    reopened = SessionStore(tmp_path)
    record = reopened.record(session.id)

    assert record is not None
    assert record.name == "持久化"
    assert record.messages[0]["content"] == "记住这句话"
    assert [item["id"] for item in reopened.list()] == [session.id]


def test_agent_memory_is_rebuilt_from_transcript(tmp_path):
    store = SessionStore(tmp_path, max_turns=5)
    session = store.create()
    store.append_message(session.id, "user", "北京天气如何")
    store.append_message(session.id, "assistant", "北京晴天")

    reopened = SessionStore(tmp_path, max_turns=5)
    agent = reopened.get(session.id, _agent)

    assert len(agent.memory) == 1
    assert "北京晴天" in agent.memory.as_dialogue()


def test_agent_instance_is_reused_within_a_store(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    first = store.get(session.id, _agent)
    second = store.get(session.id, _agent)
    assert first is second


def test_prunes_oldest_session_beyond_limit(tmp_path):
    store = SessionStore(tmp_path, max_sessions=2)
    first = store.create(name="一")
    store.append_message(first.id, "user", "旧")
    second = store.create(name="二")
    store.append_message(second.id, "user", "中")
    third = store.create(name="三")
    store.append_message(third.id, "user", "新")

    ids = [item["id"] for item in store.list()]
    assert first.id not in ids
    assert set(ids) == {second.id, third.id}
    assert not (tmp_path / f"{first.id}.json").exists()


def test_create_drops_other_empty_sessions(tmp_path):
    """空会话不该堆积：否则列表会被一堆「新会话」淹没，还占满上限名额。"""
    store = SessionStore(tmp_path)
    old = store.create(name="聊过的")
    store.append_message(old.id, "user", "北京天气如何")
    store.append_message(old.id, "assistant", "北京晴天")
    blank_one = store.create()
    blank_two = store.create()

    ids = [item["id"] for item in store.list()]
    assert old.id in ids  # 有内容的会话必须保留
    assert blank_one.id not in ids  # 之前的空会话被清掉
    assert blank_two.id in ids  # 只剩当前这个空会话
    assert len(ids) == 2


def test_empty_session_survives_until_a_new_one_is_created(tmp_path):
    store = SessionStore(tmp_path)
    blank = store.create()
    assert [item["id"] for item in store.list()] == [blank.id]
    store.get(blank.id, _agent)  # 只是取过一次智能体，不算有内容
    assert [item["id"] for item in store.list()] == [blank.id]


def test_reset_clears_messages_and_agent(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    store.append_message(session.id, "user", "你好")
    store.get(session.id, _agent)

    assert store.reset(session.id) is True
    assert store.messages(session.id) == []
    assert store.record(session.id).agent is None


def test_record_survives_corrupted_file(tmp_path):
    (tmp_path / "broken.json").write_text("{ 这不是 JSON", encoding="utf-8")
    store = SessionStore(tmp_path)
    assert store.list() == []


# --------------------------------------------------------------- HTTP 接口


@pytest.fixture
def web_base(tmp_path) -> str:
    server = create_server(
        host="127.0.0.1",
        port=0,
        llm_mode="mock",
        quiet=True,
        session_dir=str(tmp_path / "web-sessions"),
        require_auth=False,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _run(base: str, task: str, session_id: str) -> dict:
    body = json.dumps(
        {"agent": "echo", "llm": "mock", "task": task, "session_id": session_id}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base}/api/run", data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    text = urllib.request.urlopen(request, timeout=60).read().decode("utf-8")
    payloads = []
    for block in text.split("\n\n"):
        if "event: answer" in block:
            line = [part for part in block.split("\n") if part.startswith("data:")][-1]
            payloads.append(json.loads(line[5:].strip()))
    return payloads[-1]


def test_sessions_lifecycle_over_http(web_base):
    created = _post(f"{web_base}/api/sessions/create", {"name": "接口会话"})
    session_id = created["session"]["id"]

    listed = _get(f"{web_base}/api/sessions")["sessions"]
    assert [item["id"] for item in listed] == [session_id]
    assert listed[0]["name"] == "接口会话"

    renamed = _post(
        f"{web_base}/api/sessions/rename", {"session_id": session_id, "name": "改名后"}
    )
    assert renamed["renamed"] is True
    assert _get(f"{web_base}/api/sessions")["sessions"][0]["name"] == "改名后"

    detail = _get(f"{web_base}/api/session?id={session_id}")
    assert detail["session"]["id"] == session_id
    assert detail["messages"] == []

    deleted = _post(f"{web_base}/api/sessions/delete", {"session_id": session_id})
    assert deleted["deleted"] is True
    assert _get(f"{web_base}/api/sessions")["sessions"] == []


def test_run_records_transcript_and_auto_name(web_base):
    session_id = _post(f"{web_base}/api/sessions/create", {})["session"]["id"]

    _run(web_base, "第一个问题：北京天气如何", session_id)
    _run(web_base, "第二个问题：那上海呢", session_id)

    detail = _get(f"{web_base}/api/session?id={session_id}")
    messages = detail["messages"]
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert messages[0]["content"] == "第一个问题：北京天气如何"
    assert detail["session"]["name"].startswith("第一个问题")
    assert detail["session"]["message_count"] == 4


def test_unknown_session_returns_empty_transcript(web_base):
    detail = _get(f"{web_base}/api/session?id=missing-session")
    assert detail["session"] is None
    assert detail["messages"] == []


def test_repeated_new_sessions_do_not_pile_up_empty_ones(web_base):
    first = _post(f"{web_base}/api/sessions/create", {})["session"]["id"]
    second = _post(f"{web_base}/api/sessions/create", {})["session"]["id"]
    third = _post(f"{web_base}/api/sessions/create", {})["session"]["id"]

    ids = [item["id"] for item in _get(f"{web_base}/api/sessions")["sessions"]]
    assert ids == [third]
    assert first not in ids and second not in ids


def test_echo_agent_is_available_in_web(tmp_path):
    """回归：新增会话接口后，智能体列表依然完整。"""
    store = SessionStore(tmp_path)
    session = store.create()
    agent = store.get(session.id, lambda: EchoAgent(llm=ScriptedLLM(["好的"]), tools=ToolRegistry()))
    assert agent.name == "echo"
