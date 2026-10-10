"""任务完成回调：URL 校验、签名、投递与重试（离线）。

异步任务跑完之后，调用方不该一直轮询。给一个 ``callback_url``，
我们主动把结果 POST 过去——但这件事有三个风险，测试就盯这三件事：

1. **打内网**（SSRF）：回调 URL 是调用方给的，不查地址就等于给别人一个内网探针；
2. **别人伪造**：收方得能确认这条请求真是我们发的，所以要有签名；
3. **发丢**：网络抖一下不能就算了，得重试；服务重启也不能把待发的回调弄丢。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agentcode.accounts import AccountStore
from agentcode.callbacks import (
    CallbackSender,
    callback_signature,
    deliver_due,
    run_callback_loop,
    validate_callback_url,
)
from agentcode.config import Settings
from agentcode.core.errors import AgentCodeError
from agentcode.tokens import ApiTokenService
from agentcode.web.server import create_server

#: 一个真实的公网地址（example.com 的历史 IP）：字面量 IP 不需要真的查 DNS，
#: 测试因此不依赖网络。
PUBLIC_IP = "93.184.216.34"


def _resolver(*addresses: str):
    return lambda host: list(addresses)


def _settings(**overrides) -> Settings:
    values = {
        "model": "test-model",
        "api_key": "sk-test-1234567890",
        "base_url": "https://example.invalid/v1",
        "callback_secret": "s3cret",
    }
    values.update(overrides)
    return Settings(**values)


# ---------------------------------------------------------------- URL 校验


def test_a_public_https_hook_is_accepted():
    url = "https://hooks.example.com/agentcode"
    assert validate_callback_url(url, resolver=_resolver(PUBLIC_IP)) == url


def test_a_public_http_hook_is_accepted():
    url = "http://hooks.example.com/agentcode"
    assert validate_callback_url(url, resolver=_resolver(PUBLIC_IP)) == url


@pytest.mark.parametrize("url", ["ftp://hooks.example.com/x", "file:///etc/passwd"])
def test_non_http_schemes_are_refused(url):
    with pytest.raises(AgentCodeError):
        validate_callback_url(url, resolver=_resolver(PUBLIC_IP))


def test_a_url_with_credentials_is_refused():
    """``https://user:pass@host/`` 这种地址会把账号密码塞进请求行，不收。"""
    with pytest.raises(AgentCodeError):
        validate_callback_url("https://user:pass@hooks.example.com/x", resolver=_resolver(PUBLIC_IP))


def test_a_port_outside_the_allowlist_is_refused():
    with pytest.raises(AgentCodeError):
        validate_callback_url("https://hooks.example.com:8443/x", resolver=_resolver(PUBLIC_IP))


def test_an_explicitly_allowed_port_is_accepted():
    url = "https://hooks.example.com:8443/x"
    assert validate_callback_url(url, ports=(8443,), resolver=_resolver(PUBLIC_IP)) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook",
        "http://10.1.2.3/hook",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/hook",
        "http://0.0.0.0/hook",
    ],
)
def test_private_and_loopback_literals_are_refused(url):
    with pytest.raises(AgentCodeError):
        validate_callback_url(url)


def test_a_hostname_pointing_at_an_internal_address_is_refused():
    """域名是外壳：真正的地址得解析出来再查。"""
    with pytest.raises(AgentCodeError):
        validate_callback_url("https://internal.example.com/hook", resolver=_resolver("10.0.0.5"))


def test_a_hostname_with_any_private_answer_is_refused():
    """一条公网、一条内网（DNS 轮询投毒）也要拒——不能只看第一条。"""
    with pytest.raises(AgentCodeError):
        validate_callback_url(
            "https://mixed.example.com/hook", resolver=_resolver(PUBLIC_IP, "10.0.0.5")
        )


def test_an_unresolvable_hostname_is_refused():
    def _boom(host: str):
        raise socket.gaierror("查不到")

    with pytest.raises(AgentCodeError):
        validate_callback_url("https://nowhere.example.com/hook", resolver=_boom)


def test_allow_private_lets_local_development_through():
    """本地开发/测试要能回调自己；这个口子由配置显式打开，默认关着。"""
    url = "http://127.0.0.1:9000/hook"
    assert validate_callback_url(url, allow_private=True, ports=(9000,)) == url


# ---------------------------------------------------------------- 签名


def test_the_signature_is_hmac_sha256_over_timestamp_and_body():
    """已知答案：签名口径一变，收方就验不过，这条会立刻红。"""
    body = b'{"hello":"world"}'
    digest = callback_signature("s3cret", 1700000000, body)
    assert digest == "6f4351a15224248663bdabf854a428c6942e6be080ed368c1833e5d8a1d5e9a8"


def test_the_signature_changes_with_the_body():
    first = callback_signature("s3cret", 1700000000, b'{"a":1}')
    second = callback_signature("s3cret", 1700000000, b'{"a":2}')
    assert first != second


# ---------------------------------------------------------------- 投递


def _store_with_run(
    tmp_path,
    *,
    callback_url: str = "https://hooks.example.com/agentcode",
    error: str = "",
):
    """造一条已经跑完的任务（带/不带回调 URL）。"""
    store = AccountStore(tmp_path / "accounts.db")
    account = store.create("alice", "password123")
    store.create_api_run(
        "run-1",
        account_id=account.id,
        agent="echo",
        task="你好",
        callback_url=callback_url,
    )
    result = {
        "run_id": "run-1",
        "agent": "echo",
        "success": not error,
        "answer": "" if error else "已收到任务：你好",
        "artifacts": [],
        "usage": {"total_tokens": 7, "prompt_tokens": 5, "completion_tokens": 2},
        "elapsed_ms": 12,
    }
    if error:
        result["error"] = error
    store.finish_api_run("run-1", result=result, error=error)
    return store, account


def test_a_finished_run_is_posted_with_a_verifiable_signature(tmp_path):
    store, _account = _store_with_run(tmp_path)
    seen: dict = {}

    def poster(url: str, body: bytes, headers: dict) -> int:
        seen.update(url=url, body=body, headers=dict(headers))
        return 204

    sender = CallbackSender(store, _settings(), poster=poster, resolver=_resolver(PUBLIC_IP))
    outcome = sender.send("run-1")

    assert outcome["delivered"] is True
    assert seen["url"] == "https://hooks.example.com/agentcode"
    payload = json.loads(seen["body"])
    assert payload["event"] == "run.finished"
    assert payload["run_id"] == "run-1"
    assert payload["status"] == "succeeded"
    assert payload["success"] is True
    assert payload["answer"] == "已收到任务：你好"
    assert payload["usage"]["total_tokens"] == 7
    timestamp = seen["headers"]["X-AgentCode-Timestamp"]
    assert seen["headers"]["X-AgentCode-Delivery"] == "run-1"
    assert seen["headers"]["X-AgentCode-Signature"] == "sha256=" + callback_signature(
        "s3cret", timestamp, seen["body"]
    )

    record = store.api_run("run-1")
    assert record["callback_status"] == "succeeded"
    assert record["callback_attempts"] == 1
    assert record["callback_delivered_at"]
    assert record["callback_error"] == ""


def test_a_failed_run_is_reported_as_a_failure(tmp_path):
    store, _account = _store_with_run(tmp_path, error="模型侧错误：超时")
    seen: dict = {}

    def poster(url: str, body: bytes, headers: dict) -> int:
        seen["body"] = body
        return 200

    sender = CallbackSender(store, _settings(), poster=poster, resolver=_resolver(PUBLIC_IP))
    sender.send("run-1")

    payload = json.loads(seen["body"])
    assert payload["event"] == "run.finished"
    assert payload["status"] == "failed"
    assert payload["success"] is False
    assert payload["error"] == "模型侧错误：超时"


def test_a_5xx_answer_is_retried_later(tmp_path):
    store, _account = _store_with_run(tmp_path)
    sender = CallbackSender(
        store, _settings(), poster=lambda *args: 503, resolver=_resolver(PUBLIC_IP)
    )

    outcome = sender.send("run-1")

    assert outcome["delivered"] is False
    record = store.api_run("run-1")
    assert record["callback_status"] == "pending"
    assert record["callback_attempts"] == 1
    assert "503" in record["callback_error"]
    assert datetime.fromisoformat(record["callback_next_at"]) > datetime.now(timezone.utc)
    # 排到未来，所以现在不该被捞出来重发
    assert store.due_callbacks() == []


def test_a_4xx_answer_is_not_retried(tmp_path):
    """收方说"你这个请求不对"，重发多少次都一样，别浪费。"""
    store, _account = _store_with_run(tmp_path)
    sender = CallbackSender(
        store, _settings(), poster=lambda *args: 400, resolver=_resolver(PUBLIC_IP)
    )

    outcome = sender.send("run-1")

    assert outcome["delivered"] is False
    record = store.api_run("run-1")
    assert record["callback_status"] == "failed"
    assert record["callback_attempts"] == 1
    assert "400" in record["callback_error"]
    assert record["callback_next_at"] == ""


def test_a_callback_gives_up_after_the_attempt_limit(tmp_path):
    store, _account = _store_with_run(tmp_path)
    settings = _settings(callback_max_attempts=3)
    sender = CallbackSender(
        store, settings, poster=lambda *args: 500, resolver=_resolver(PUBLIC_IP)
    )

    for _ in range(3):
        sender.send("run-1", now=time.time() + 86_400)

    record = store.api_run("run-1")
    assert record["callback_attempts"] == 3
    assert record["callback_status"] == "failed"
    assert record["callback_next_at"] == ""


def test_an_already_delivered_callback_is_not_sent_again(tmp_path):
    store, _account = _store_with_run(tmp_path)
    calls: list[int] = []

    def poster(url: str, body: bytes, headers: dict) -> int:
        calls.append(1)
        return 204

    sender = CallbackSender(store, _settings(), poster=poster, resolver=_resolver(PUBLIC_IP))
    sender.send("run-1")
    again = sender.send("run-1")

    assert calls == [1]
    assert again["delivered"] is False
    assert store.api_run("run-1")["callback_attempts"] == 1


def test_a_run_without_a_callback_is_left_alone(tmp_path):
    store, _account = _store_with_run(tmp_path, callback_url="")
    calls: list[int] = []

    def poster(url: str, body: bytes, headers: dict) -> int:
        calls.append(1)
        return 200

    sender = CallbackSender(store, _settings(), poster=poster, resolver=_resolver(PUBLIC_IP))
    outcome = sender.send("run-1")

    assert calls == []
    assert outcome["delivered"] is False
    assert store.api_run("run-1")["callback_status"] == "none"


def test_a_still_running_job_is_not_delivered_early(tmp_path):
    """任务还没结束就回调，等于告诉调用方一个还不存在的结果。"""
    store = AccountStore(tmp_path / "accounts.db")
    account = store.create("alice", "password123")
    store.create_api_run(
        "run-1",
        account_id=account.id,
        agent="echo",
        task="还在跑",
        callback_url="https://hooks.example.com/x",
    )

    assert store.due_callbacks() == []

    store.finish_api_run("run-1", result={"run_id": "run-1", "answer": "好了"})
    assert [item["run_id"] for item in store.due_callbacks()] == ["run-1"]


def test_a_pending_callback_survives_a_restart(tmp_path):
    """换了进程也得接着发：待发状态在库里，不在内存里。"""
    _store_with_run(tmp_path)
    seen: dict = {}

    def poster(url: str, body: bytes, headers: dict) -> int:
        seen["body"] = body
        return 200

    restarted = AccountStore(tmp_path / "accounts.db")  # 模拟"重启后的新进程"
    report = deliver_due(
        restarted,
        _settings(),
        sender=CallbackSender(
            restarted, _settings(), poster=poster, resolver=_resolver(PUBLIC_IP)
        ),
    )

    assert report["delivered"] == 1
    assert json.loads(seen["body"])["run_id"] == "run-1"
    assert restarted.api_run("run-1")["callback_status"] == "succeeded"


def test_deliver_due_only_picks_what_is_due(tmp_path):
    store, _account = _store_with_run(tmp_path)
    failing = CallbackSender(
        store, _settings(), poster=lambda *args: 503, resolver=_resolver(PUBLIC_IP)
    )
    failing.send("run-1")  # 第一次失败，排到未来重试

    report = deliver_due(
        store,
        _settings(),
        sender=CallbackSender(
            store, _settings(), poster=lambda *args: 200, resolver=_resolver(PUBLIC_IP)
        ),
    )

    assert report["due"] == 0
    assert store.api_run("run-1")["callback_attempts"] == 1


def test_a_pending_callback_is_not_deleted_by_the_retention_sweep(tmp_path):
    """清理老记录时不能把还欠着的回调一起删掉——那等于吞掉一个承诺。"""
    from datetime import timedelta

    store, account = _store_with_run(tmp_path)
    with store._lock, store._conn:  # noqa: SLF001 - 伪造"很久以前的任务"
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
        store._conn.execute("UPDATE api_runs SET created_at = ? WHERE run_id = 'run-1'", (old,))

    assert store.prune_api_runs(days=7) == 0
    assert store.api_run("run-1") is not None

    # 一旦发完（或放弃），它就只是普通的老记录，该清就清
    store.record_callback_attempt("run-1", status="succeeded", attempts=1, delivered_at=old)
    assert store.prune_api_runs(days=7) == 1


def test_the_callback_loop_survives_a_failure(tmp_path, monkeypatch, capsys):
    from agentcode import callbacks as callbacks_module

    store = AccountStore(tmp_path / "accounts.db")

    class _Stop(Exception):
        """把循环从 sleep 里拽出来。"""

    def _boom(*args, **kwargs):
        raise RuntimeError("数据库锁住了")

    monkeypatch.setattr(callbacks_module, "deliver_due", _boom)
    monkeypatch.setattr(
        callbacks_module.time, "sleep", lambda _seconds: (_ for _ in ()).throw(_Stop)
    )
    with pytest.raises(_Stop):
        run_callback_loop(store, _settings(), interval_seconds=5)

    assert "数据库锁住了" in capsys.readouterr().out


def test_a_delivery_is_counted_in_the_metrics(tmp_path):
    from agentcode.metrics import CALLBACKS

    store, _account = _store_with_run(tmp_path)
    before = CALLBACKS.value(result="ok")

    sender = CallbackSender(
        store, _settings(), poster=lambda *args: 204, resolver=_resolver(PUBLIC_IP)
    )
    sender.send("run-1")

    assert CALLBACKS.value(result="ok") == before + 1


def test_the_audit_keeps_the_host_but_not_the_secret_path(tmp_path):
    """审计不能成为泄露源：Slack 那种把密钥塞在路径里的地址只记主机名。"""
    store, _account = _store_with_run(
        tmp_path, callback_url="https://hooks.example.com/services/T00/B00/s3cretpath"
    )
    sender = CallbackSender(
        store, _settings(), poster=lambda *args: 204, resolver=_resolver(PUBLIC_IP)
    )
    sender.send("run-1")

    entries = store.audit_entries(action="api.callback")
    assert len(entries) == 1
    assert entries[0]["target"] == "https://hooks.example.com"
    assert "s3cretpath" not in json.dumps(entries, ensure_ascii=False)


# ---------------------------------------------------------------- 接口


def _call(
    base: str,
    path: str,
    *,
    token: str,
    payload: dict | None = None,
    key: str | None = None,
    method: str = "POST",
) -> tuple[int, dict]:
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    if key:
        headers["Idempotency-Key"] = key
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"{base}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, (json.loads(raw.decode("utf-8")) if raw else {})
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, (json.loads(raw.decode("utf-8")) if raw else {})


def _token(accounts: AccountStore, account) -> str:
    _, plaintext = ApiTokenService(accounts).create(account, name="脚本")
    return plaintext


def _start_web(
    tmp_path, monkeypatch, *, env_file: str | None = None, with_secret: bool = True
):
    monkeypatch.setenv("AGENT_CODE_ROOT", str(tmp_path / "sandbox"))
    monkeypatch.setenv("AGENT_WEB_SESSION_DIR", str(tmp_path / "sessions"))
    if with_secret:
        monkeypatch.setenv("AGENT_CALLBACK_SECRET", "s3cret-test")
    else:
        monkeypatch.delenv("AGENT_CALLBACK_SECRET", raising=False)
    monkeypatch.setenv("AGENT_CALLBACK_ALLOW_PRIVATE", "true")
    accounts = AccountStore(tmp_path / "accounts.db")
    account = accounts.create("alice", "password123")
    server = create_server(
        host="127.0.0.1",
        port=0,
        llm_mode="mock",
        quiet=True,
        session_dir=str(tmp_path / "sessions"),
        accounts=accounts,
        env_file=env_file,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}", accounts, account


@pytest.fixture
def web(tmp_path, monkeypatch):
    server, base, accounts, account = _start_web(tmp_path, monkeypatch)
    try:
        yield base, accounts, account
    finally:
        server.shutdown()
        server.server_close()


def test_an_async_job_with_a_callback_reports_it_as_pending(web):
    base, accounts, account = web
    token = _token(accounts, account)

    status, submitted = _call(
        base,
        "/v1/run",
        token=token,
        payload={
            "agent": "echo",
            "task": "你好",
            "async": True,
            "callback_url": f"https://{PUBLIC_IP}/hook",
        },
    )

    assert status == 202
    assert submitted["callback"]["status"] == "pending"
    assert submitted["callback"]["host"] == PUBLIC_IP

    _status, detail = _call(base, f"/v1/runs/{submitted['run_id']}", token=token, method="GET")
    assert detail["callback"]["status"] in ("pending", "succeeded")
    assert detail["callback"]["attempts"] >= 0


def test_a_sync_request_cannot_use_a_callback(web):
    """同步响应里已经有结果了，再回调一次是多余的——直接说清楚。"""
    base, accounts, account = web
    token = _token(accounts, account)

    status, payload = _call(
        base,
        "/v1/run",
        token=token,
        payload={
            "agent": "echo",
            "task": "你好",
            "callback_url": f"https://{PUBLIC_IP}/hook",
        },
    )

    assert status == 400
    assert "async" in payload["error"]
    assert accounts.api_runs(account.id) == []  # 没接单，也没扣额度


def test_a_private_callback_url_is_refused_at_submit(web, monkeypatch):
    base, accounts, account = web
    monkeypatch.setenv("AGENT_CALLBACK_ALLOW_PRIVATE", "false")
    token = _token(accounts, account)

    status, payload = _call(
        base,
        "/v1/run",
        token=token,
        payload={
            "agent": "echo",
            "task": "你好",
            "async": True,
            "callback_url": "http://10.0.0.5/hook",
        },
    )

    assert status == 400
    assert accounts.api_runs(account.id) == []


def test_a_callback_without_a_signing_secret_is_refused(tmp_path, monkeypatch):
    """签不了名就别接单：收方没法确认来源的回调等于没有防伪造。"""
    monkeypatch.delenv("AGENT_CALLBACK_SECRET", raising=False)
    monkeypatch.delenv("AGENT_SECRET_KEY", raising=False)
    env_file = tmp_path / "bare.env"
    env_file.write_text("AGENT_API_ENABLED=true\n", encoding="utf-8")
    server, base, accounts, account = _start_web(
        tmp_path, monkeypatch, env_file=str(env_file), with_secret=False
    )
    try:
        token = _token(accounts, account)
        status, payload = _call(
            base,
            "/v1/run",
            token=token,
            payload={
                "agent": "echo",
                "task": "你好",
                "async": True,
                "callback_url": f"https://{PUBLIC_IP}/hook",
            },
        )
    finally:
        server.shutdown()
        server.server_close()

    assert status == 400
    assert "签名" in payload["error"]


def test_an_idempotent_replay_does_not_revalidate_the_url(web, monkeypatch):
    """同一单的重发不该因为"地址现在解析不通"被拒——那一单早就收下了，
    而且用的是**当时**那个地址。"""
    base, accounts, account = web
    token = _token(accounts, account)
    payload = {
        "agent": "echo",
        "task": "你好",
        "async": True,
        "callback_url": f"https://{PUBLIC_IP}/hook",
    }
    first = _call(base, "/v1/run", token=token, payload=payload, key="k-1")[1]
    monkeypatch.setenv("AGENT_CALLBACK_ALLOW_PRIVATE", "false")

    status, second = _call(
        base,
        "/v1/run",
        token=token,
        payload={**payload, "callback_url": "http://10.0.0.5/hook"},
        key="k-1",
    )

    assert status in (200, 202)
    assert second["run_id"] == first["run_id"]
    assert second["idempotent_replay"] is True


def test_the_callback_fires_when_an_async_job_finishes(tmp_path, monkeypatch):
    """真机演练：本地起一个接收端，断言它真的收到了、而且签名验得过。"""
    received: dict = {}

    class _Receiver(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - 父类约定的方法名
            length = int(self.headers.get("Content-Length") or 0)
            received["body"] = self.rfile.read(length)
            received["headers"] = dict(self.headers.items())
            self.send_response(204)
            self.end_headers()

        def log_message(self, *args):  # noqa: D102 - 静音
            pass

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    receiver.daemon_threads = True
    threading.Thread(target=receiver.serve_forever, daemon=True).start()
    port = receiver.server_port
    monkeypatch.setenv("AGENT_CALLBACK_PORTS", str(port))

    server, base, accounts, account = _start_web(tmp_path, monkeypatch)
    settings = Settings.from_env()
    try:
        token = _token(accounts, account)
        status, submitted = _call(
            base,
            "/v1/run",
            token=token,
            payload={
                "agent": "echo",
                "task": "你好",
                "async": True,
                "callback_url": f"http://127.0.0.1:{port}/hook",
            },
        )
        assert status == 202

        deadline = time.time() + 20
        while time.time() < deadline and accounts.api_run(submitted["run_id"])[
            "callback_status"
        ] != "succeeded":
            deliver_due(accounts, settings)
            time.sleep(0.05)

        assert "body" in received, "接收端没有收到回调"
        payload = json.loads(received["body"])
        assert payload["event"] == "run.finished"
        assert payload["run_id"] == submitted["run_id"]
        assert payload["status"] == "succeeded"
        timestamp = received["headers"]["X-AgentCode-Timestamp"]
        assert received["headers"]["X-AgentCode-Signature"] == "sha256=" + callback_signature(
            "s3cret-test", timestamp, received["body"]
        )

        record = accounts.api_run(submitted["run_id"])
        assert record["callback_status"] == "succeeded"
        _status, detail = _call(
            base, f"/v1/runs/{submitted['run_id']}", token=token, method="GET"
        )
        assert detail["callback"]["status"] == "succeeded"
        assert detail["callback"]["delivered_at"]
    finally:
        server.shutdown()
        server.server_close()
        receiver.shutdown()
        receiver.server_close()
