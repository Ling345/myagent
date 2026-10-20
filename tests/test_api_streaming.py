"""对外流式：/v1/run 的 SSE、OpenAI 兼容的 /v1/chat/completions 与 /v1/models（离线）。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from agentcode.accounts import AccountStore
from agentcode.llm.base import Message
from agentcode.memory.short_term import ShortTermMemory
from agentcode.tokens import ApiTokenService
from agentcode.web.server import chat_messages_to_task, create_server


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CODE_ROOT", str(tmp_path / "sandbox"))
    monkeypatch.setenv("AGENT_WEB_SESSION_DIR", str(tmp_path / "sessions"))
    accounts = AccountStore(tmp_path / "accounts.db")
    account = accounts.create("alice", "password123")
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
        yield base, accounts, account
    finally:
        server.shutdown()
        server.server_close()


def _token(accounts: AccountStore, account) -> str:
    _, plaintext = ApiTokenService(accounts).create(account, name="脚本")
    return plaintext


def _post(
    base: str,
    path: str,
    *,
    token: str,
    payload: dict,
) -> tuple[int, str, str]:
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Content-Type", ""), error.read().decode("utf-8")


def _get(base: str, path: str, *, token: str) -> tuple[int, str]:
    request = urllib.request.Request(
        f"{base}{path}", headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8")


def _sse_events(body: str) -> list[tuple[str, dict]]:
    """把 ``event: x\\ndata: {...}`` 解析成 [(事件名, 数据)]。"""
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        name = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :].strip()
            elif line.startswith("data: "):
                data = line[len("data: ") :].strip()
        if name and data:
            events.append((name, json.loads(data)))
    return events


def _chat_chunks(body: str) -> list[dict]:
    """OpenAI 流式：只有 ``data: ...``，最后一行是 ``[DONE]``。"""
    chunks = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: ") :].strip()
        if payload == "[DONE]":
            break
        chunks.append(json.loads(payload))
    return chunks


# ---------------------------------------------------------------- /v1/run 流式


def test_run_stream_emits_sse_events(web):
    base, accounts, account = web
    token = _token(accounts, account)

    status, content_type, body = _post(
        base,
        "/v1/run",
        token=token,
        payload={"agent": "echo", "task": "你好", "stream": True},
    )

    assert status == 200
    assert "text/event-stream" in content_type
    events = _sse_events(body)
    names = [name for name, _ in events]
    assert "start" in names
    assert "step" in names
    assert "answer" in names
    assert names[-1] == "done"
    done = events[-1][1]
    assert done["run_id"]
    assert done["status"] == "succeeded"
    assert done["success"] is True
    assert done["answer"] == "已收到任务：你好"
    assert done["usage"]["total_tokens"] >= 0


def test_run_stream_records_the_run_in_the_ledger(web):
    base, accounts, account = web
    token = _token(accounts, account)

    _status, _content_type, body = _post(
        base,
        "/v1/run",
        token=token,
        payload={"agent": "echo", "task": "你好", "stream": True},
    )
    run_id = _sse_events(body)[-1][1]["run_id"]

    status, raw = _get(base, f"/v1/runs/{run_id}", token=token)

    assert status == 200
    record = json.loads(raw)
    assert record["status"] == "succeeded"
    assert record["answer"] == "已收到任务：你好"


def test_run_stream_and_async_cannot_be_combined(web):
    base, accounts, account = web
    token = _token(accounts, account)

    status, _content_type, body = _post(
        base,
        "/v1/run",
        token=token,
        payload={"agent": "echo", "task": "你好", "stream": True, "async": True},
    )

    assert status == 400
    assert "stream" in body


def test_run_stream_needs_a_token(web):
    base, _accounts, _account = web

    status, _content_type, body = _post(
        base, "/v1/run", token="agk_fake", payload={"task": "你好", "stream": True}
    )

    assert status == 401


# ---------------------------------------------------------------- chat completions


def test_chat_completions_returns_the_openai_shape(web):
    base, accounts, account = web
    token = _token(accounts, account)

    status, _content_type, body = _post(
        base,
        "/v1/chat/completions",
        token=token,
        payload={
            "model": "my-model",
            "agent": "echo",
            "messages": [{"role": "user", "content": "你好"}],
        },
    )

    assert status == 200
    data = json.loads(body)
    assert data["object"] == "chat.completion"
    assert data["model"] == "my-model"
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert data["choices"][0]["message"]["content"] == "已收到任务：你好"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"]["total_tokens"] >= 0
    assert data["id"].startswith("chatcmpl-")


def test_chat_completions_streams_openai_chunks(web):
    base, accounts, account = web
    token = _token(accounts, account)

    status, content_type, body = _post(
        base,
        "/v1/chat/completions",
        token=token,
        payload={
            "stream": True,
            "agent": "echo",
            "messages": [{"role": "user", "content": "你好"}],
        },
    )

    assert status == 200
    assert "text/event-stream" in content_type
    assert body.rstrip().endswith("[DONE]")
    chunks = _chat_chunks(body)
    assert chunks[0]["object"] == "chat.completion.chunk"
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    content = "".join(
        chunk["choices"][0]["delta"].get("content", "") for chunk in chunks
    )
    assert content == "已收到任务：你好"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_chat_completions_needs_a_user_message(web):
    base, accounts, account = web
    token = _token(accounts, account)

    status, _content_type, body = _post(
        base,
        "/v1/chat/completions",
        token=token,
        payload={"messages": [{"role": "system", "content": "你是一个助手"}]},
    )

    assert status == 400
    assert "user" in body


def test_chat_completions_records_usage_and_the_run(web):
    base, accounts, account = web
    token = _token(accounts, account)
    calls_before = accounts.usage_today(account.id)[1]

    _post(
        base,
        "/v1/chat/completions",
        token=token,
        payload={"messages": [{"role": "user", "content": "你好"}]},
    )

    assert accounts.usage_today(account.id)[1] == calls_before + 1
    record = accounts.api_runs(account.id)[0]
    assert record["status"] == "succeeded"


def test_previous_turns_are_seeded_into_memory():
    task, memory = chat_messages_to_task(
        [
            {"role": "system", "content": "你是一个助手"},
            {"role": "user", "content": "北京天气如何"},
            {"role": "assistant", "content": "北京晴"},
            {"role": "user", "content": "那上海呢"},
        ]
    )

    assert task == "那上海呢"
    assert isinstance(memory, ShortTermMemory)
    assert len(memory) == 1
    messages = memory.get_messages()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "北京天气如何"
    assert messages[1]["content"] == "北京晴"


def test_an_unpaired_previous_turn_is_ignored():
    task, memory = chat_messages_to_task(
        [
            {"role": "user", "content": "没有回答的旧问题"},
            {"role": "user", "content": "新问题"},
        ]
    )

    assert task == "新问题"
    assert memory is not None
    assert len(memory) == 0


# ---------------------------------------------------------------- /v1/models


def test_models_endpoint_lists_the_configured_model(web):
    base, accounts, account = web
    token = _token(accounts, account)

    status, raw = _get(base, "/v1/models", token=token)

    assert status == 200
    data = json.loads(raw)
    assert data["object"] == "list"
    assert data["data"]
    assert data["data"][0]["object"] == "model"
