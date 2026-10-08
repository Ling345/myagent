"""对外开放 API：令牌鉴权、POST /v1/run、GET /v1/me（离线）。"""

from __future__ import annotations

import json
import secrets
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agentcode.accounts import AccountStore
from agentcode.tokens import ApiTokenService
from agentcode.web.server import create_server


def _request(
    base: str, path: str, *, token: str | None = None, payload: dict | None = None, method: str = "POST"
) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"{base}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return response.status, (json.loads(raw.decode("utf-8")) if raw else {})
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, (json.loads(raw.decode("utf-8")) if raw else {})


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
        yield base, accounts, account, tmp_path
    finally:
        server.shutdown()
        server.server_close()


def _token(accounts: AccountStore, account) -> str:
    _, plaintext = ApiTokenService(accounts).create(account, name="测试脚本")
    return plaintext


# ---------------------------------------------------------------- /v1/me


def test_me_reports_the_plan_and_remaining_quota(web):
    base, accounts, account, _ = web
    token = _token(accounts, account)

    status, payload = _request(base, "/v1/me", token=token, method="GET")

    assert status == 200
    assert payload["account"] == "alice"
    assert payload["plan"] == "free"
    assert payload["daily_token_limit"] == account.daily_token_limit
    assert payload["used_today"] == 0
    assert payload["token"]["name"] == "测试脚本"
    assert payload["token"]["prefix"].startswith("agk_")


def test_me_requires_a_token(web):
    base, _, _, _ = web
    status, payload = _request(base, "/v1/me", method="GET")
    assert status == 401
    assert "令牌" in payload["error"]


def test_me_rejects_a_revoked_token(web):
    base, accounts, account, _ = web
    service = ApiTokenService(accounts)
    record, token = service.create(account, name="要被吊销的")
    service.revoke(account.id, record["id"])

    assert _request(base, "/v1/me", token=token, method="GET")[0] == 401


@pytest.mark.parametrize(
    "token",
    [
        "agk_not-a-real-token-but-right-shape-xxxxxxxx",  # 形状对、库里没有
        "junk",
        "agk_" + secrets.token_urlsafe(32),  # 真的随机、但没登记过
    ],
)
def test_me_rejects_junk_tokens(web, token):
    base, _, _, _ = web
    assert _request(base, "/v1/me", token=token, method="GET")[0] == 401


# ---------------------------------------------------------------- /v1/run


def test_run_returns_the_answer_and_the_usage(web):
    """一次调用：给一个任务，拿回答案 + 这次花了多少 token。"""
    base, accounts, account, _ = web
    token = _token(accounts, account)

    status, payload = _request(
        base, "/v1/run", token=token, payload={"agent": "echo", "task": "你好"}
    )

    assert status == 200, payload
    assert payload["success"] is True
    assert payload["answer"] == "已收到任务：你好"
    assert payload["run_id"]
    assert payload["agent"] == "echo"
    assert payload["elapsed_ms"] >= 0
    assert payload["usage"]["total_tokens"] >= 0
    # 这次调用要记在这个账号头上（和网页走同一条账）；
    # mock 后端不产生真实 token，所以看调用次数，不看 token 数
    used, calls = accounts.usage_today(account.id)
    assert calls == 1
    assert used == payload["usage"]["total_tokens"]


def test_run_needs_a_token(web):
    base, _, _, _ = web
    status, payload = _request(base, "/v1/run", payload={"task": "你好"})
    assert status == 401
    assert "令牌" in payload["error"]


def test_run_rejects_an_empty_task_and_unknown_agent(web):
    base, accounts, account, _ = web
    token = _token(accounts, account)

    status, payload = _request(base, "/v1/run", token=token, payload={"task": "   "})
    assert status == 400
    assert "任务" in payload["error"]

    status, payload = _request(
        base, "/v1/run", token=token, payload={"task": "你好", "agent": "查无此智能体"}
    )
    assert status == 400
    assert "智能体" in payload["error"]


def test_run_stops_when_the_daily_quota_is_gone(web):
    base, accounts, account, tmp_path = web
    token = _token(accounts, account)
    accounts.set_limit("alice", 5)
    accounts.record_usage(account.id, 100, prompt_tokens=100, completion_tokens=0, run_id="r1")

    status, payload = _request(base, "/v1/run", token=token, payload={"task": "你好", "agent": "echo"})

    assert status == 402
    assert "额度" in payload["error"]


def test_run_can_continue_a_session_so_the_context_sticks(web):
    """带 session_id 连着调两次：第二次要能记住第一次说的话。"""
    base, accounts, account, _ = web
    token = _token(accounts, account)

    first = _request(
        base,
        "/v1/run",
        token=token,
        payload={"agent": "echo", "task": "记住：我叫吴咏翰", "session_id": "api-chat"},
    )[1]
    second = _request(
        base,
        "/v1/run",
        token=token,
        payload={"agent": "echo", "task": "我叫什么", "session_id": "api-chat"},
    )[1]

    assert first["session_id"] == "api-chat"
    assert second["session_id"] == "api-chat"
    # 会话列表里能看到这个会话，而且攒了两轮
    store = accounts
    assert store.usage_today(account.id)[1] == 2  # 计了两次调用
    sessions = Path(str(accounts.path)).parent / "sessions" / account.id
    assert (sessions / "api-chat.json").is_file()


def test_api_calls_are_audited_with_the_token_name(web):
    """"哪个令牌在什么时候调了什么"要查得到。"""
    base, accounts, account, _ = web
    token = _token(accounts, account)

    _request(base, "/v1/run", token=token, payload={"agent": "echo", "task": "你好"})

    entry = accounts.audit_entries(action="api.run")[0]
    assert entry["actor_name"] == "alice"
    assert entry["detail"]["token_name"] == "测试脚本"
    assert entry["detail"]["agent"] == "echo"
    # 审计里绝不能出现令牌明文——那是能直接花钱的凭据
    assert token not in json.dumps(accounts.audit_entries(), ensure_ascii=False)


def test_one_accounts_token_cannot_touch_anothers_quota(web):
    base, accounts, account, _ = web
    bob = accounts.create("bob", "password123")
    alice_token = _token(accounts, account)

    _request(base, "/v1/run", token=alice_token, payload={"agent": "echo", "task": "你好"})

    assert accounts.usage_today(account.id)[1] == 1
    assert accounts.usage_today(bob.id)[1] == 0


def test_api_can_be_turned_off_entirely(web, monkeypatch):
    """公开部署上想彻底关掉对外接口时，应当 404 而不是悄悄留着。"""
    base, accounts, account, _ = web
    token = _token(accounts, account)
    monkeypatch.setenv("AGENT_API_ENABLED", "false")

    status, payload = _request(base, "/v1/run", token=token, payload={"task": "你好"})
    assert status == 404
    assert "关闭" in payload["error"]


def test_the_server_decides_the_model_not_the_caller(web):
    """调用方不能在请求里挑后端（比如挑 mock 拿假答案）。"""
    base, accounts, account, _ = web
    token = _token(accounts, account)

    status, payload = _request(
        base, "/v1/run", token=token, payload={"agent": "echo", "task": "你好", "llm": "openai"}
    )

    assert status == 200
    assert payload["llm_mode"] == "mock"  # 服务端跑的是 mock，就不许被改
