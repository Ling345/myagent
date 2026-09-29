"""套餐驱动的配额测试（离线）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentcode.accounts import AccountStore


def _store(tmp_path) -> AccountStore:
    return AccountStore(tmp_path / "accounts.db")


def test_free_plan_limit_comes_from_the_catalog(tmp_path):
    store = _store(tmp_path)
    account = store.create("alice", "password123")
    assert store.effective_plan(account).name == "free"
    assert store.daily_limit(account) == 20_000


def test_limit_override_beats_the_plan(tmp_path):
    store = _store(tmp_path)
    account = store.create("alice", "password123", daily_token_limit=100)
    assert store.daily_limit(account) == 100


def test_owner_plan_is_unlimited(tmp_path):
    store = _store(tmp_path)
    account = store.create("alice", "password123", plan="owner")
    assert store.daily_limit(account) == 0
    store.record_usage(account.id, 10_000_000)
    assert store.check_quota(store.get("alice"))[0] is True
    assert store.remaining_tokens(store.get("alice")) == -1


def test_expired_plan_downgrades_to_free(tmp_path):
    store = _store(tmp_path)
    account = store.create("alice", "password123", plan="pro")
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    store.apply_plan("alice", "pro", started_at=past, expires_at=past)

    refreshed = store.get("alice")
    assert store.effective_plan(refreshed).name == "free"
    assert store.daily_limit(refreshed) == 20_000


def test_active_paid_plan_raises_the_limit(tmp_path):
    store = _store(tmp_path)
    store.create("alice", "password123")
    future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds")
    store.apply_plan("alice", "pro", started_at="2026-09-29T00:00:00+00:00", expires_at=future)

    assert store.daily_limit(store.get("alice")) == 1_000_000


def test_quota_rejection_mentions_upgrade(tmp_path):
    store = _store(tmp_path)
    account = store.create("alice", "password123", daily_token_limit=100)
    store.record_usage(account.id, 100)

    allowed, reason = store.check_quota(store.get("alice"))
    assert allowed is False
    assert "今日额度已用完" in reason
    assert "升级套餐" in reason


def test_remaining_tokens_counts_down(tmp_path):
    store = _store(tmp_path)
    account = store.create("alice", "password123", daily_token_limit=1000)
    store.record_usage(account.id, 200)
    assert store.remaining_tokens(store.get("alice")) == 800


def test_disabled_account_is_rejected(tmp_path):
    store = _store(tmp_path)
    store.create("alice", "password123")
    store.set_active("alice", False)
    allowed, reason = store.check_quota(store.get("alice"))
    assert allowed is False
    assert "停用" in reason


def test_unknown_plan_name_still_resolves_to_free(tmp_path):
    """数据库里的套餐名被改坏时，不能变成无限额度。"""
    store = _store(tmp_path)
    account = store.create("alice", "password123")
    with store._lock, store._conn:  # noqa: SLF001 - 故意制造坏数据
        store._conn.execute("UPDATE accounts SET plan = '???' WHERE id = ?", (account.id,))

    refreshed = store.get("alice")
    assert store.effective_plan(refreshed).name == "free"
    assert store.daily_limit(refreshed) == 20_000
