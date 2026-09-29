"""套餐开通与续期测试（离线）。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentcode.accounts import AccountStore
from agentcode.billing import (
    ORDER_PAID,
    ORDER_PENDING,
    BillingService,
    add_months,
)
from agentcode.core.errors import AgentCodeError


def _service(tmp_path):
    store = AccountStore(tmp_path / "accounts.db")
    return store, BillingService(store)


def test_add_months_handles_month_ends():
    base = datetime(2026, 1, 31, tzinfo=timezone.utc)
    assert add_months(base, 1) == datetime(2026, 2, 28, tzinfo=timezone.utc)
    assert add_months(base, 2) == datetime(2026, 3, 31, tzinfo=timezone.utc)


def test_add_months_crosses_the_year():
    base = datetime(2026, 11, 15, tzinfo=timezone.utc)
    assert add_months(base, 3) == datetime(2027, 2, 15, tzinfo=timezone.utc)


def test_plans_hides_internal_plan(tmp_path):
    _, billing = _service(tmp_path)
    names = [plan.name for plan in billing.plans()]
    assert "owner" not in names
    assert names[0] == "free"


def test_checkout_rejects_unbuyable_plan(tmp_path):
    store, billing = _service(tmp_path)
    account = store.create("alice", "password123", plan="owner")
    with pytest.raises(AgentCodeError, match="不可购买"):
        billing.checkout(account, "owner")


def test_checkout_rejects_out_of_range_months(tmp_path):
    store, billing = _service(tmp_path)
    account = store.create("alice", "password123")
    with pytest.raises(AgentCodeError, match="月数"):
        billing.checkout(account, "basic", months=13)


def test_checkout_creates_a_pending_order_with_the_right_amount(tmp_path):
    store, billing = _service(tmp_path)
    account = store.create("alice", "password123")
    order = billing.checkout(account, "basic", months=3)
    assert order.status == ORDER_PENDING
    assert order.amount_cents == 2_900 * 3


def test_confirm_activates_the_plan(tmp_path):
    store, billing = _service(tmp_path)
    account = store.create("alice", "password123")
    order = billing.checkout(account, "basic", months=1)

    confirmed = billing.confirm(order.id, reference="微信转账 20260929")
    assert confirmed.status == ORDER_PAID
    assert confirmed.provider_ref == "微信转账 20260929"

    refreshed = store.get("alice")
    assert refreshed.plan == "basic"
    assert store.daily_limit(refreshed) == 200_000
    assert refreshed.plan_expires_at is not None


def test_confirm_is_idempotent(tmp_path):
    """重复确认不能再延长一次——否则等于白送。"""
    store, billing = _service(tmp_path)
    account = store.create("alice", "password123")
    order = billing.checkout(account, "basic", months=1)

    billing.confirm(order.id)
    first_expiry = store.get("alice").plan_expires_at
    billing.confirm(order.id)
    assert store.get("alice").plan_expires_at == first_expiry


def test_extending_the_same_plan_stacks_on_the_old_expiry(tmp_path):
    store, billing = _service(tmp_path)
    account = store.create("alice", "password123")
    billing.confirm(billing.checkout(account, "basic", months=1).id)
    first = store.get("alice").plan_expires_at

    billing.confirm(billing.checkout(store.get("alice"), "basic", months=1).id)
    second = store.get("alice").plan_expires_at
    assert second > first


def test_switching_plan_starts_from_today(tmp_path):
    store, billing = _service(tmp_path)
    account = store.create("alice", "password123")
    billing.confirm(billing.checkout(account, "basic", months=1).id)
    billing.confirm(billing.checkout(store.get("alice"), "pro", months=1).id)
    assert store.get("alice").plan == "pro"
    assert store.daily_limit(store.get("alice")) == 1_000_000


def test_cancelled_order_cannot_be_confirmed(tmp_path):
    store, billing = _service(tmp_path)
    account = store.create("alice", "password123")
    order = billing.checkout(account, "basic", months=1)
    store.set_order_status(order.id, "cancelled")

    with pytest.raises(AgentCodeError, match="不能确认"):
        billing.confirm(order.id)
    assert store.get("alice").plan == "free"  # 套餐没被动过


def test_unknown_order_is_rejected(tmp_path):
    _, billing = _service(tmp_path)
    with pytest.raises(AgentCodeError, match="找不到"):
        billing.confirm("不存在的订单号")


def test_grant_activates_without_an_order(tmp_path):
    store, billing = _service(tmp_path)
    account = store.create("alice", "password123")
    billing.grant(account, "pro", months=1)

    refreshed = store.get("alice")
    assert refreshed.plan == "pro"
    assert store.list_orders(account.id) == []


def test_grant_owner_never_expires(tmp_path):
    store, billing = _service(tmp_path)
    account = store.create("alice", "password123")
    billing.grant(account, "owner", months=1)
    assert store.get("alice").plan_expires_at is None


def test_grant_rejects_unknown_plan(tmp_path):
    store, billing = _service(tmp_path)
    account = store.create("alice", "password123")
    with pytest.raises(AgentCodeError, match="没有这个套餐"):
        billing.grant(account, "不存在的套餐")


def test_my_billing_reports_usage_and_orders(tmp_path):
    store, billing = _service(tmp_path)
    account = store.create("alice", "password123")
    store.record_usage(account.id, 1_234, calls=2)
    billing.checkout(account, "basic", months=1)

    payload = billing.my_billing(store.get("alice"))
    assert payload["plan"]["name"] == "free"
    assert payload["used_today"] == 1_234
    assert payload["daily_limit"] == 20_000
    assert payload["remaining_today"] == 20_000 - 1_234
    assert len(payload["orders"]) == 1
    assert payload["orders"][0]["plan"] == "basic"


def test_my_billing_marks_unlimited(tmp_path):
    store, billing = _service(tmp_path)
    account = store.create("alice", "password123", plan="owner")
    payload = billing.my_billing(store.get("alice"))
    assert payload["daily_limit"] == 0
    assert payload["remaining_today"] == -1
