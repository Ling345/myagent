"""服务端按套餐收口（离线）。

配额只对**登录用户**生效——免登录模式是本地自用/自动化测试的口子，
`_account` 为空，本来就不做额度检查。所以这里的用例都真的登录。
"""

from __future__ import annotations

import http.cookiejar
import json
import threading
import urllib.error
import urllib.request

import pytest

from agentcode.accounts import AccountStore
from agentcode.config import Settings
from agentcode.web.server import create_server, scope_settings_for_plan


class _Client:
    """带 Cookie 的测试客户端。"""

    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def _call(self, path: str, payload: dict | None) -> tuple[int, dict]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method="POST" if data is not None else "GET",
        )
        try:
            with self.opener.open(request, timeout=60) as response:
                body = response.read().decode("utf-8")
                return response.status, (json.loads(body) if body.startswith("{") else {})
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8")
            return error.code, (json.loads(body) if body.startswith("{") else {})

    def get(self, path: str) -> tuple[int, dict]:
        return self._call(path, None)

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        return self._call(path, payload)

    def login(self, name: str, password: str = "password123") -> tuple[int, dict]:
        return self.post("/api/login", {"name": name, "password": password})


def _server(tmp_path, **extra):
    accounts = AccountStore(tmp_path / "accounts.db")
    server = create_server(
        host="127.0.0.1",
        port=0,
        llm_mode="mock",
        quiet=True,
        session_dir=str(tmp_path / "sessions"),
        accounts=accounts,
        **extra,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, accounts, f"http://127.0.0.1:{server.server_port}"


@pytest.fixture
def web(tmp_path):
    """要求登录的服务 + 一个额度很小的账号。"""
    server, accounts, base = _server(tmp_path)
    account = accounts.create("alice", "password123", daily_token_limit=40)
    try:
        yield base, accounts, account
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- 账本带运行编号


def test_run_records_the_run_id_in_the_ledger(web):
    base, accounts, account = web
    client = _Client(base)
    assert client.login("alice")[0] == 200

    status, _ = client.post(
        "/api/run", {"agent": "react", "task": "北京天气如何", "llm": "mock"}
    )
    assert status == 200

    rows = accounts.ledger_rows(account.id)
    assert rows, "跑完后账本里应该有记录"
    assert rows[0]["run_id"], "账本要能追到具体是哪次运行"
    # 输入/输出要分开记——计价靠它
    assert rows[0]["prompt_tokens"] is not None
    assert rows[0]["completion_tokens"] is not None
    assert rows[0]["prompt_tokens"] + rows[0]["completion_tokens"] == rows[0]["tokens"]


# ---------------------------------------------------------------- 配额


def test_quota_exhaustion_returns_402_with_upgrade_hint(web):
    base, accounts, account = web
    accounts.record_usage(account.id, 40)  # 额度就是 40，直接打满
    client = _Client(base)
    client.login("alice")

    status, payload = client.post(
        "/api/run", {"agent": "react", "task": "北京天气如何", "llm": "mock"}
    )
    assert status == 402
    assert "升级套餐" in payload["error"]


def test_account_payload_reports_the_plan(web):
    base, accounts, account = web
    accounts.record_usage(account.id, 10)
    client = _Client(base)
    client.login("alice")

    status, payload = client.get("/api/me")
    assert status == 200
    info = payload["account"]
    assert info["plan"] == "free"
    assert info["plan_title"] == "免费"
    assert info["daily_token_limit"] == 40
    assert info["used_today"] == 10
    assert info["remaining"] == 30
    assert info["unlimited"] is False


def test_unlimited_plan_reports_minus_one_remaining(tmp_path):
    server, accounts, base = _server(tmp_path)
    accounts.create("boss", "password123", plan="owner")
    try:
        client = _Client(base)
        client.login("boss")
        status, payload = client.get("/api/me")
        assert status == 200
        assert payload["account"]["unlimited"] is True
        assert payload["account"]["remaining"] == -1
    finally:
        server.shutdown()
        server.server_close()


def test_upgrading_the_plan_lifts_the_quota(tmp_path):
    """端到端：打满被拒 → 开通套餐 → 同一账号立刻能继续跑。"""
    from agentcode.billing import BillingService

    server, accounts, base = _server(tmp_path)
    account = accounts.create("alice", "password123")  # 免费套餐：日额度 2 万
    try:
        client = _Client(base)
        client.login("alice")
        accounts.record_usage(account.id, 20_000)  # 把免费额度打满
        assert client.post("/api/run", {"agent": "react", "task": "北京天气如何", "llm": "mock"})[0] == 402

        BillingService(accounts).grant(accounts.get("alice"), "basic", months=1)

        status, _ = client.post(
            "/api/run", {"agent": "react", "task": "北京天气如何", "llm": "mock"}
        )
        assert status == 200
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------------- 免费套餐不下发代码工具


def test_free_plan_scope_turns_code_tools_off():
    settings = Settings(model="m", api_key="sk-x", base_url="https://x.invalid")
    scoped = scope_settings_for_plan(settings, "free")
    assert scoped.allow_code_tools is False


def test_paid_plan_scope_keeps_code_tools_on():
    settings = Settings(
        model="m", api_key="sk-x", base_url="https://x.invalid", allow_code_tools=True
    )
    assert scope_settings_for_plan(settings, "basic").allow_code_tools is True


def test_scope_never_grants_more_than_global_setting():
    """全局关着代码执行时，套餐再高也不能把它打开。"""
    settings = Settings(
        model="m", api_key="sk-x", base_url="https://x.invalid", allow_code_tools=False
    )
    assert scope_settings_for_plan(settings, "team").allow_code_tools is False


def test_scope_without_plan_keeps_settings():
    settings = Settings(
        model="m", api_key="sk-x", base_url="https://x.invalid", allow_code_tools=True
    )
    assert scope_settings_for_plan(settings, None).allow_code_tools is True


# ---------------------------------------------------------------- 接口


def test_plans_endpoint_lists_sellable_plans(web):
    base, _, _ = web
    client = _Client(base)
    client.login("alice")

    status, payload = client.get("/api/plans")
    assert status == 200
    names = [plan["name"] for plan in payload["plans"]]
    assert names == ["free", "basic", "pro", "team"]
    assert "owner" not in names


def test_billing_endpoint_shape(web):
    base, accounts, account = web
    accounts.record_usage(account.id, 10)
    client = _Client(base)
    client.login("alice")

    status, payload = client.get("/api/billing")
    assert status == 200
    assert payload["plan"]["name"] == "free"
    assert payload["used_today"] == 10
    assert payload["daily_limit"] == 40  # 这个账号被设了 40 的覆盖
    assert payload["remaining_today"] == 30
    assert payload["orders"] == []


def test_checkout_rejects_internal_plan(web):
    base, _, _ = web
    client = _Client(base)
    client.login("alice")

    status, payload = client.post("/api/billing/checkout", {"plan": "owner", "months": 1})
    assert status == 400
    assert "不可购买" in payload["error"]


def test_checkout_rejects_bad_months(web):
    base, _, _ = web
    client = _Client(base)
    client.login("alice")

    status, payload = client.post("/api/billing/checkout", {"plan": "basic", "months": 99})
    assert status == 400
    assert "月数" in payload["error"]


def test_checkout_returns_a_pending_order_for_a_sellable_plan(web):
    base, _, _ = web
    client = _Client(base)
    client.login("alice")

    status, payload = client.post("/api/billing/checkout", {"plan": "pro", "months": 2})
    assert status == 200
    assert payload["order"]["plan"] == "pro"
    assert payload["order"]["amount_cents"] == 9_900 * 2
    assert payload["order"]["status"] == "pending"
    assert "订单号" in payload["message"] or payload["order"]["id"]


def test_checkout_order_shows_up_in_billing(web):
    base, _, _ = web
    client = _Client(base)
    client.login("alice")

    client.post("/api/billing/checkout", {"plan": "basic", "months": 1})
    _, payload = client.get("/api/billing")
    assert len(payload["orders"]) == 1
    assert payload["orders"][0]["plan"] == "basic"


def test_billing_endpoints_require_login(tmp_path):
    server, accounts, base = _server(tmp_path)
    accounts.create("alice", "password123")
    try:
        client = _Client(base)
        for path in ("/api/plans", "/api/billing"):
            assert client.get(path)[0] == 401
        assert client.post("/api/billing/checkout", {"plan": "basic"})[0] == 401
    finally:
        server.shutdown()
        server.server_close()
