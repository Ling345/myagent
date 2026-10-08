"""对外 API 的任务台账、异步任务与幂等键（离线）。"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from agentcode.accounts import AccountStore
from agentcode.tokens import ApiTokenService
from agentcode.web.server import create_server


def _call(
    base: str,
    path: str,
    *,
    token: str,
    payload: dict | None = None,
    key: str | None = None,
    method: str = "POST",
) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    if key:
        headers["Idempotency-Key"] = key
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{base}{path}", data=body, headers=headers, method=method
    )
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
        yield base, accounts, account
    finally:
        server.shutdown()
        server.server_close()


def _token(accounts: AccountStore, account) -> str:
    _, plaintext = ApiTokenService(accounts).create(account, name="脚本")
    return plaintext


def _wait_for(base: str, token: str, run_id: str, *, timeout: float = 30.0) -> dict:
    """轮询到任务结束（或超时），返回最后一次响应。"""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        _status, last = _call(
            base, f"/v1/runs/{run_id}", token=token, method="GET"
        )
        if last.get("status") in ("succeeded", "failed"):
            return last
        time.sleep(0.05)
    return last


# ---------------------------------------------------------------- 异步任务


def test_async_run_returns_immediately_and_can_be_polled(web):
    base, accounts, account = web
    token = _token(accounts, account)

    status, submitted = _call(
        base,
        "/v1/run",
        token=token,
        payload={"agent": "echo", "task": "你好", "async": True},
    )

    assert status == 202
    assert submitted["status"] in ("running", "succeeded")
    assert submitted["run_id"]
    assert submitted["poll"] == f"/v1/runs/{submitted['run_id']}"

    final = _wait_for(base, token, submitted["run_id"])
    assert final["status"] == "succeeded"
    assert final["answer"] == "已收到任务：你好"
    assert final["usage"]["total_tokens"] >= 0
    assert final["finished_at"]


def test_a_finished_job_survives_a_restart_because_it_is_in_the_database(web):
    """服务重启后，已经跑完的任务还查得到——这是异步能用的前提。"""
    base, accounts, account = web
    token = _token(accounts, account)
    _call(base, "/v1/run", token=token, payload={"agent": "echo", "task": "你好", "async": True})
    run_id = accounts.api_runs(account.id)[0]["run_id"]
    _wait_for(base, token, run_id)

    # 直接读库（绕过内存）：模拟"换了个进程来查"
    record = accounts.api_run(run_id)
    assert record["status"] == "succeeded"
    assert record["result"]["answer"] == "已收到任务：你好"


def test_polling_reports_not_found_for_unknown_or_foreign_runs(web):
    base, accounts, account = web
    token = _token(accounts, account)
    bob = accounts.create("bob", "password123")
    bob_token = _token(accounts, bob)

    assert _call(base, "/v1/runs/no-such-run", token=token, method="GET")[0] == 404

    # alice 的任务，bob 拿着自己的令牌查不到
    _call(base, "/v1/run", token=token, payload={"agent": "echo", "task": "你好", "async": True})
    run_id = accounts.api_runs(account.id)[0]["run_id"]
    assert _call(base, f"/v1/runs/{run_id}", token=bob_token, method="GET")[0] == 404


def test_recent_runs_can_be_listed(web):
    base, accounts, account = web
    token = _token(accounts, account)
    _call(base, "/v1/run", token=token, payload={"agent": "echo", "task": "第一个"})
    _call(base, "/v1/run", token=token, payload={"agent": "echo", "task": "第二个"})

    status, payload = _call(base, "/v1/runs", token=token, method="GET")

    assert status == 200
    tasks = [item["task"] for item in payload["runs"]]
    assert tasks[:2] == ["第二个", "第一个"]
    # 列表不带结果正文（那是详细查询的事）
    assert "result" not in payload["runs"][0]


# ---------------------------------------------------------------- 幂等键


def test_the_same_idempotency_key_does_not_run_twice(web):
    """客户端超时重发是最常见的重试方式：它绝不能变成"跑两次、扣两次"。"""
    base, accounts, account = web
    token = _token(accounts, account)

    first_status, first = _call(
        base, "/v1/run", token=token, payload={"agent": "echo", "task": "只跑一次"}, key="ci-42"
    )
    second_status, second = _call(
        base, "/v1/run", token=token, payload={"agent": "echo", "task": "只跑一次"}, key="ci-42"
    )

    assert first_status == 200
    assert second_status == 200
    assert first["run_id"] == second["run_id"]
    assert second["idempotent_replay"] is True
    assert second["answer"] == first["answer"]
    # 只记了一次用量
    assert accounts.usage_today(account.id)[1] == 1
    assert len(accounts.api_runs(account.id)) == 1


def test_a_retry_while_the_first_is_still_running_gets_the_same_job(web):
    base, accounts, account = web
    token = _token(accounts, account)

    _status, submitted = _call(
        base,
        "/v1/run",
        token=token,
        payload={"agent": "echo", "task": "长任务", "async": True},
        key="ci-43",
    )
    again_status, again = _call(
        base,
        "/v1/run",
        token=token,
        payload={"agent": "echo", "task": "长任务", "async": True},
        key="ci-43",
    )

    # 还在跑：告诉调用方"就是这一单，去轮询"，而不是再开一单
    assert again_status in (200, 202)
    assert again["run_id"] == submitted["run_id"]
    assert again["idempotent_replay"] is True
    assert len(accounts.api_runs(account.id)) == 1


def test_different_keys_or_accounts_are_different_jobs(web):
    base, accounts, account = web
    token = _token(accounts, account)
    bob = accounts.create("bob", "password123")
    bob_token = _token(accounts, bob)

    first = _call(base, "/v1/run", token=token, payload={"agent": "echo", "task": "A"}, key="k1")[1]
    second = _call(base, "/v1/run", token=token, payload={"agent": "echo", "task": "B"}, key="k2")[1]
    other = _call(
        base, "/v1/run", token=bob_token, payload={"agent": "echo", "task": "C"}, key="k1"
    )[1]

    assert len({first["run_id"], second["run_id"], other["run_id"]}) == 3


def test_without_a_key_every_call_is_a_new_job(web):
    base, accounts, account = web
    token = _token(accounts, account)

    first = _call(base, "/v1/run", token=token, payload={"agent": "echo", "task": "同样的任务"})[1]
    second = _call(base, "/v1/run", token=token, payload={"agent": "echo", "task": "同样的任务"})[1]

    assert first["run_id"] != second["run_id"]
    assert accounts.usage_today(account.id)[1] == 2


# ---------------------------------------------------------------- 清理


def test_stale_running_jobs_are_marked_failed(tmp_path):
    """卡在 running 的老任务要标出来，否则调用方会一直轮询一个死任务。"""
    store = AccountStore(tmp_path / "agentcode.db")
    account = store.create("alice", "password123")
    store.create_api_run("stale-1", account_id=account.id, agent="echo", task="卡住的")
    with store._lock, store._conn:  # noqa: SLF001 - 测试要伪造"很久以前"
        old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
        store._conn.execute("UPDATE api_runs SET created_at = ? WHERE run_id = 'stale-1'", (old,))

    marked = store.mark_stale_api_runs(older_than_seconds=3600)

    assert marked == 1
    record = store.api_run("stale-1")
    assert record["status"] == "failed"
    assert "服务重启" in record["error"]


def test_old_jobs_are_pruned_but_recent_ones_stay(tmp_path):
    store = AccountStore(tmp_path / "agentcode.db")
    account = store.create("alice", "password123")
    store.create_api_run("old", account_id=account.id, agent="echo", task="很久以前")
    store.create_api_run("new", account_id=account.id, agent="echo", task="刚刚")
    with store._lock, store._conn:  # noqa: SLF001
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
        store._conn.execute("UPDATE api_runs SET created_at = ? WHERE run_id = 'old'", (old,))

    assert store.prune_api_runs(days=7) == 1

    assert store.api_run("old") is None
    assert store.api_run("new") is not None
    assert store.prune_api_runs(days=0) == 0  # 0 = 永久保留


def test_job_records_are_removed_when_the_account_is_deleted(tmp_path):
    store = AccountStore(tmp_path / "agentcode.db")
    account = store.create("alice", "password123")
    store.create_api_run("r1", account_id=account.id, agent="echo", task="任务")

    store.delete_account(account.id)

    assert store.api_run("r1") is None


def test_housekeeping_marks_stale_runs_and_prunes_old_ones(tmp_path):
    from agentcode.config import Settings
    from agentcode.lifecycle import housekeeping_once

    store = AccountStore(tmp_path / "agentcode.db")
    account = store.create("alice", "password123")
    store.create_api_run("stuck", account_id=account.id, agent="echo", task="卡住的")
    store.create_api_run("ancient", account_id=account.id, agent="echo", task="上个月的")
    with store._lock, store._conn:  # noqa: SLF001 - 伪造"很久以前"
        long_ago = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
        store._conn.execute(
            "UPDATE api_runs SET created_at = ? WHERE run_id IN ('stuck', 'ancient')", (long_ago,)
        )
    settings = Settings(
        model="m",
        api_key="sk-x",
        base_url="https://x.invalid",
        db_path=str(tmp_path / "agentcode.db"),
        api_runs_days=7,
    )

    report = housekeeping_once(store, settings)

    assert report["api_runs"]["stale"] == 2  # 两个都卡在 running
    assert report["api_runs"]["pruned"] == 2  # 都超过 7 天，一起清掉
    assert store.api_runs(account.id) == []
