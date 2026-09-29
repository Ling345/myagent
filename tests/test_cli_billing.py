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
