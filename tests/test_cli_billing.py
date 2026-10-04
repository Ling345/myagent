"""账单相关命令行（离线）。"""

from __future__ import annotations

import json

from agentcode.accounts import AccountStore
from agentcode.billing import BillingService
from agentcode.cli import main


def _prepare(tmp_path, monkeypatch) -> AccountStore:
    """把账号库指到临时目录，避免碰真实的 traces/agentcode.db。"""
    path = tmp_path / "accounts.db"
    monkeypatch.setenv("AGENT_DB_PATH", str(path))
    return AccountStore(path)


def test_user_plan_command_switches_plan(tmp_path, monkeypatch, capsys):
    store = _prepare(tmp_path, monkeypatch)
    store.create("alice", "password123")

    code = main(["user", "plan", "alice", "pro", "--months", "1"])
    assert code == 0
    refreshed = AccountStore(tmp_path / "accounts.db").get("alice")
    assert refreshed.plan == "pro"
    assert refreshed.plan_expires_at is not None
    assert "pro" in capsys.readouterr().out


def test_user_plan_rejects_unknown_account(tmp_path, monkeypatch, capsys):
    _prepare(tmp_path, monkeypatch)
    code = main(["user", "plan", "nobody", "pro"])
    assert code == 2
    assert "没有这个账号" in capsys.readouterr().out


def test_billing_grant_command_works(tmp_path, monkeypatch, capsys):
    store = _prepare(tmp_path, monkeypatch)
    store.create("alice", "password123")

    code = main(["billing", "grant", "alice", "basic", "--months", "2"])
    assert code == 0
    assert AccountStore(tmp_path / "accounts.db").get("alice").plan == "basic"
    assert "basic" in capsys.readouterr().out


def test_billing_grant_rejects_unknown_plan(tmp_path, monkeypatch, capsys):
    store = _prepare(tmp_path, monkeypatch)
    store.create("alice", "password123")

    code = main(["billing", "grant", "alice", "不存在的套餐"])
    assert code == 2
    assert "没有这个套餐" in capsys.readouterr().out


def test_billing_confirm_command_activates(tmp_path, monkeypatch, capsys):
    store = _prepare(tmp_path, monkeypatch)
    account = store.create("alice", "password123")
    order = BillingService(store).checkout(account, "basic", 1)

    code = main(["billing", "confirm", order.id, "--reference", "微信转账"])
    assert code == 0
    refreshed = AccountStore(tmp_path / "accounts.db").get("alice")
    assert refreshed.plan == "basic"
    assert "已确认到账" in capsys.readouterr().out


def test_billing_confirm_rejects_unknown_order(tmp_path, monkeypatch, capsys):
    _prepare(tmp_path, monkeypatch)
    code = main(["billing", "confirm", "nope"])
    assert code == 2
    assert "找不到这个订单" in capsys.readouterr().out


def test_billing_orders_lists_with_json(tmp_path, monkeypatch, capsys):
    store = _prepare(tmp_path, monkeypatch)
    account = store.create("alice", "password123")
    BillingService(store).checkout(account, "basic", 1)

    code = main(["billing", "orders", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["orders"][0]["plan"] == "basic"
    assert payload["orders"][0]["amount_cents"] == 2_900


def test_billing_orders_is_readable_without_json(tmp_path, monkeypatch, capsys):
    store = _prepare(tmp_path, monkeypatch)
    account = store.create("alice", "password123")
    BillingService(store).checkout(account, "pro", 2)

    code = main(["billing", "orders"])
    assert code == 0
    out = capsys.readouterr().out
    assert "pro" in out
    assert "198.00" in out  # 9900 分 × 2 个月
    assert "pending" in out


def test_billing_orders_empty_message(tmp_path, monkeypatch, capsys):
    _prepare(tmp_path, monkeypatch)
    code = main(["billing", "orders"])
    assert code == 0
    assert "还没有订单" in capsys.readouterr().out


# ---------------------------------------------------------------- 成本换算


def _with_usage(tmp_path, monkeypatch):
    """准备一份有真实用量的库，并配上单价。"""
    store = _prepare(tmp_path, monkeypatch)
    account = store.create("alice", "password123")
    store.record_usage(account.id, 10_000, calls=1, prompt_tokens=8_000, completion_tokens=2_000)
    monkeypatch.setenv("AGENT_PRICE_INPUT_PER_MILLION", "200")
    monkeypatch.setenv("AGENT_PRICE_OUTPUT_PER_MILLION", "800")
    return store, account


def test_costs_reports_money_when_price_is_configured(tmp_path, monkeypatch, capsys):
    _with_usage(tmp_path, monkeypatch)
    code = main(["billing", "costs", "--days", "30"])
    out = capsys.readouterr().out

    assert code == 0
    assert "输出占比 20.0%（实测）" in out
    # 8000×200 + 2000×800 = 160万 + 160万 = 320万「分×百万分之一」= 3 分
    assert "￥0.03" in out
    assert "套餐毛利" in out


def test_costs_uses_the_observed_ratio(tmp_path, monkeypatch, capsys):
    """有真实数据就不要再拿假设值糊弄人。"""
    _with_usage(tmp_path, monkeypatch)
    main(["billing", "costs"])
    out = capsys.readouterr().out
    assert "假设值" not in out


def test_costs_says_so_when_price_is_missing(tmp_path, monkeypatch, capsys):
    """没配单价就说清楚怎么配，别让人猜为什么全是 0。"""
    store = _prepare(tmp_path, monkeypatch)
    store.create("alice", "password123")
    monkeypatch.delenv("AGENT_PRICE_INPUT_PER_MILLION", raising=False)
    monkeypatch.delenv("AGENT_PRICE_OUTPUT_PER_MILLION", raising=False)

    code = main(["billing", "costs"])
    out = capsys.readouterr().out
    assert code == 0
    assert "没配单价" in out
    assert "AGENT_PRICE_INPUT_PER_MILLION" in out


def test_costs_marks_the_assumption_when_there_is_no_data(tmp_path, monkeypatch, capsys):
    """还没有拆分数据时必须标明"这是假设"，不能假装是实测。"""
    store = _prepare(tmp_path, monkeypatch)
    account = store.create("alice", "password123")
    store.record_usage(account.id, 1000)  # 老式记法：只有总数
    monkeypatch.setenv("AGENT_PRICE_INPUT_PER_MILLION", "200")
    monkeypatch.setenv("AGENT_PRICE_OUTPUT_PER_MILLION", "800")

    main(["billing", "costs"])
    out = capsys.readouterr().out
    assert "假设值" in out
    assert "早期数据" in out


def test_costs_output_ratio_can_be_overridden(tmp_path, monkeypatch, capsys):
    store = _prepare(tmp_path, monkeypatch)
    store.create("alice", "password123")
    monkeypatch.setenv("AGENT_PRICE_INPUT_PER_MILLION", "200")
    monkeypatch.setenv("AGENT_PRICE_OUTPUT_PER_MILLION", "800")

    main(["billing", "costs", "--output-ratio", "0.5"])
    assert "50.0%" in capsys.readouterr().out


def test_costs_json_output(tmp_path, monkeypatch, capsys):
    import json

    _with_usage(tmp_path, monkeypatch)
    code = main(["billing", "costs", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["price"]["configured"] is True
    assert payload["totals"]["total_tokens"] == 10_000
    assert payload["totals"]["estimated_cost_cents"] == 3
    assert payload["output_ratio_is_observed"] is True
    plans = {item["plan"] for item in payload["plans"]}
    assert plans == {"free", "basic", "pro", "team"}


def test_costs_can_filter_by_account(tmp_path, monkeypatch, capsys):
    import json

    store = _prepare(tmp_path, monkeypatch)
    alice = store.create("alice", "password123")
    bob = store.create("bob", "password123")
    store.record_usage(alice.id, 1_000, prompt_tokens=800, completion_tokens=200)
    store.record_usage(bob.id, 5_000, prompt_tokens=4_000, completion_tokens=1_000)

    main(["billing", "costs", "--account", "bob", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert [row["name"] for row in payload["accounts"]] == ["bob"]
    assert payload["totals"]["total_tokens"] == 5_000


def test_costs_rejects_an_unknown_account(tmp_path, monkeypatch, capsys):
    _prepare(tmp_path, monkeypatch)
    assert main(["billing", "costs", "--account", "nobody"]) == 2
    assert "没有这个账号" in capsys.readouterr().out
