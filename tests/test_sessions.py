"""上下文记忆与会话测试。"""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from agentcode.agents import ReActAgent
from agentcode.cli import main
from agentcode.llm import ScriptedLLM
from agentcode.memory import ShortTermMemory
from agentcode.memory.session_store import FileSessionStore
from agentcode.tools import ToolRegistry
from agentcode.web.server import create_server
from agentcode.web.sessions import AgentSessionStore

#: 每段脚本刚好够跑完一次 ReAct（一次工具调用 + 一次收尾）
SCRIPT_BEIJING = [
    "Thought: 先查北京\nAction: get_weather[北京]",
    "Thought: 够了\nAction: Finish[北京晴天]",
]
SCRIPT_SHANGHAI = [
    "Thought: 再查上海\nAction: get_weather[上海]",
    "Thought: 够了\nAction: Finish[上海多云]",
]


def _tools() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register_tool("get_weather", "查天气", lambda city: f"{city}天气已查到")
    return tools


def _agent(memory: ShortTermMemory | None = None) -> ReActAgent:
    """构造一个连着两段脚本的智能体，方便验证第二轮是否看到第一轮。"""
    return ReActAgent(
        llm=ScriptedLLM(SCRIPT_BEIJING + SCRIPT_SHANGHAI),
        tools=_tools(),
        memory=memory,
    )


# ------------------------------------------------------------------ 智能体


def test_second_run_prompt_contains_previous_answer():
    agent = _agent()
    agent.run("北京天气如何")
    agent.run("那上海呢")

    second_run_prompt = agent.llm.calls[2][-1]["content"]
    assert "北京晴天" in second_run_prompt
    assert "北京天气如何" in second_run_prompt


def test_memory_stores_one_complete_turn():
    agent = _agent()
    agent.run("北京天气如何")

    assert len(agent.memory) == 1
    messages = agent.memory.get_messages()
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "北京晴天"


def test_memory_trims_oldest_turns():
    agent = _agent(memory=ShortTermMemory(max_turns=1))
    agent.run("北京天气如何")
    agent.run("那上海呢")

    assert len(agent.memory) == 1
    dialogue = agent.memory.as_dialogue()
    assert "北京晴天" not in dialogue
    assert "上海多云" in dialogue


def test_first_run_prompt_has_no_history():
    agent = _agent()
    agent.run("北京天气如何")
    first_prompt = agent.llm.calls[0][-1]["content"]
    assert "第一轮" in first_prompt


def test_failed_run_does_not_write_memory():
    agent = ReActAgent(llm=ScriptedLLM([]), tools=_tools())
    result = agent.run("随便问问")
    assert result.success is False
    assert len(agent.memory) == 0


# ------------------------------------------------------------- 网页会话仓库


def test_agent_session_store_reuses_instance():
    store = AgentSessionStore(max_sessions=4)
    created = {"count": 0}

    def factory():
        created["count"] += 1
        return _agent()

    first = store.get("s1", factory)
    second = store.get("s1", factory)

    assert first is second
    assert created["count"] == 1


def test_agent_session_store_evicts_oldest():
    store = AgentSessionStore(max_sessions=2)
    for name in ("s1", "s2", "s3"):
        store.get(name, _agent)

    assert store.session_ids() == ["s2", "s3"]
    assert "s1" not in store


def test_agent_session_reset_drops_instance():
    store = AgentSessionStore()
    agent = store.get("s1", _agent)
    agent.run("北京天气如何")
    assert store.turns("s1") == 1

    assert store.reset("s1") is True
    assert store.reset("s1") is False
    assert store.get("s1", _agent).memory.get_messages() == []


def test_agent_session_rejects_empty_id():
    store = AgentSessionStore()
    with pytest.raises(ValueError):
        store.get("", _agent)


# ------------------------------------------------------------- 磁盘会话


def test_file_session_store_roundtrip(tmp_path):
    store = FileSessionStore(tmp_path)
    memory = ShortTermMemory(max_turns=5)
    memory.add_turn(
        [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ]
    )

    path = store.save("demo", memory)
    assert path.exists()

    loaded = store.load("demo")
    assert loaded.get_messages() == memory.get_messages()
    assert loaded.max_turns == 5


def test_file_session_store_returns_empty_for_unknown(tmp_path):
    assert FileSessionStore(tmp_path).load("没这个会话").get_messages() == []


def test_cli_run_with_session_persists_history(tmp_path, capsys):
    session_dir = tmp_path / "sessions"
    for _ in range(2):
        code = main(
            [
                "run",
                "--agent",
                "react",
                "--llm",
                "mock",
                "--task",
                "北京天气如何",
                "--session",
                "demo",
                "--session-dir",
                str(session_dir),
            ]
        )
        assert code == 0
    capsys.readouterr()

    payload = json.loads((session_dir / "demo.json").read_text(encoding="utf-8"))
    assert payload["session"] == "demo"
    assert len(payload["turns"]) == 2


# --------------------------------------------------------------- HTTP 接口


@pytest.fixture
def web_base() -> str:
    server = create_server(host="127.0.0.1", port=0, llm_mode="mock", quiet=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _post(url: str, payload: dict) -> str:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8")


def _answer_payload(sse_text: str) -> dict:
    """从 SSE 文本里取出 answer 事件的 JSON 载荷。"""
    payloads = []
    for block in sse_text.split("\n\n"):
        if "event: answer" not in block:
            continue
        for line in block.split("\n"):
            if line.startswith("data:"):
                payloads.append(json.loads(line[5:].strip()))
    return payloads[-1]


def test_web_run_accumulates_memory_in_session(web_base):
    body = {"agent": "react", "llm": "mock", "task": "北京天气如何", "session_id": "web-1"}

    first = _answer_payload(_post(f"{web_base}/api/run", body))
    second = _answer_payload(_post(f"{web_base}/api/run", body))

    assert first["memory_turns"] == 1
    assert second["memory_turns"] == 2


def test_web_sessions_are_isolated(web_base):
    base = {"agent": "react", "llm": "mock", "task": "北京天气如何"}

    first = _answer_payload(_post(f"{web_base}/api/run", {**base, "session_id": "web-a"}))
    other = _answer_payload(_post(f"{web_base}/api/run", {**base, "session_id": "web-b"}))

    assert first["memory_turns"] == 1
    assert other["memory_turns"] == 1


def test_web_session_reset_endpoint(web_base):
    body = {"agent": "react", "llm": "mock", "task": "北京天气如何", "session_id": "web-2"}
    _post(f"{web_base}/api/run", body)
    _post(f"{web_base}/api/run", body)

    reset_result = json.loads(_post(f"{web_base}/api/session/reset", {"session_id": "web-2"}))
    after = _answer_payload(_post(f"{web_base}/api/run", body))

    assert reset_result["reset"] is True
    assert after["memory_turns"] == 1
