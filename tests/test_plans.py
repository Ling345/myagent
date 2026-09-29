"""套餐目录测试（离线）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentcode.plans import (
    FREE,
    OWNER,
    PLANS,
    effective_plan,
    get_plan,
    is_expired,
    sellable_plans,
)


def test_unknown_plan_falls_back_to_free():
    """坏字符串绝不能换来无限额度。"""
    assert get_plan("不存在的套餐").name == FREE
    assert get_plan(None).name == FREE
    assert get_plan("").name == FREE


def test_plan_lookup_is_forgiving_about_case_and_space():
    assert get_plan("  PRO ").name == "pro"


def test_owner_is_not_sellable():
    assert OWNER not in {plan.name for plan in sellable_plans()}
    assert PLANS[OWNER].daily_tokens == 0  # 0 = 不限


def test_sellable_plans_are_sorted_by_price():
    prices = [plan.price_cents for plan in sellable_plans()]
    assert prices == sorted(prices)
    assert prices[0] == 0  # 免费套餐排最前


def test_no_expiry_means_never_expires():
    assert is_expired(None) is False
    assert is_expired("") is False


def test_unparseable_expiry_is_treated_as_expired():
    """宁可降级也不放行：坏数据不能让付费墙失效。"""
    assert is_expired("不是时间") is True


def test_expiry_compares_against_now():
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    assert is_expired((now - timedelta(days=1)).isoformat(), now=now) is True
    assert is_expired((now + timedelta(days=1)).isoformat(), now=now) is False


def test_effective_plan_downgrades_after_expiry():
    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    past = (now - timedelta(days=1)).isoformat()
    future = (now + timedelta(days=1)).isoformat()
    assert effective_plan("pro", past, now=now).name == FREE
    assert effective_plan("pro", future, now=now).name == "pro"
    assert effective_plan(OWNER, None, now=now).name == OWNER


def test_catalog_matches_the_design_doc():
    """额度数字是设计文档里确认过的，改它要先改文档。"""
    assert PLANS["free"].daily_tokens == 20_000
    assert PLANS["basic"].daily_tokens == 200_000
    assert PLANS["pro"].daily_tokens == 1_000_000
    assert PLANS["team"].daily_tokens == 5_000_000
    assert PLANS["free"].allow_code_tools is False
    assert PLANS["basic"].allow_code_tools is True
    assert PLANS["basic"].price_cents == 2_900
