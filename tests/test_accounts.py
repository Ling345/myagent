"""账号、密码哈希与每日配额。"""

from __future__ import annotations

import pytest

from agentcode.accounts import AccountStore, hash_password, verify_password
from agentcode.core.errors import AgentCodeError


@pytest.fixture
def store(tmp_path) -> AccountStore:
    return AccountStore(tmp_path / "accounts.db")


def test_password_hash_roundtrip():
    stored = hash_password("s3cret-pw")
    assert stored.startswith("pbkdf2_sha256$")
    assert "s3cret-pw" not in stored
    assert verify_password("s3cret-pw", stored) is True
    assert verify_password("wrong", stored) is False


def test_short_password_is_rejected():
    with pytest.raises(AgentCodeError):
        hash_password("123")


def test_verify_rejects_broken_hash():
    assert verify_password("x", "不是合法哈希") is False


def test_create_and_get_account(store):
    account = store.create("alice", "password123", plan="pro", daily_token_limit=1234)
    assert account.plan == "pro"
    assert account.daily_token_limit == 1234
    assert store.get("alice").id == account.id
    assert store.get_by_id(account.id).name == "alice"


def test_duplicate_name_is_rejected(store):
    store.create("alice", "password123")
    with pytest.raises(AgentCodeError):
        store.create("alice", "password456")


def test_verify_returns_none_for_wrong_password(store):
    store.create("alice", "password123")
    assert store.verify("alice", "nope") is None
    assert store.verify("bob", "password123") is None
    assert store.verify("alice", "password123") is not None


def test_disabled_account_cannot_login(store):
    store.create("alice", "password123")
    assert store.set_active("alice", False) is True
    assert store.verify("alice", "password123") is None
    assert store.set_active("alice", True) is True
    assert store.verify("alice", "password123") is not None


def test_usage_accumulates_and_resets(store):
    account = store.create("alice", "password123", daily_token_limit=1000)
    store.record_usage(account.id, 120, calls=1)
    store.record_usage(account.id, 80, calls=1)
    assert store.usage_today(account.id) == (200, 2)
    assert store.remaining_tokens(account) == 800

    store.reset_usage(account.id)
    assert store.usage_today(account.id) == (0, 0)


def test_quota_blocks_after_limit(store):
    account = store.create("alice", "password123", daily_token_limit=100)
    allowed, _ = store.check_quota(account)
    assert allowed is True

    store.record_usage(account.id, 150)
    allowed, reason = store.check_quota(account)
    assert allowed is False
    assert "额度已用完" in reason


def test_quota_rejects_disabled_account(store):
    account = store.create("alice", "password123")
    store.set_active("alice", False)
    allowed, reason = store.check_quota(store.get("alice"))
    assert allowed is False
    assert "停用" in reason
    assert account.is_active is True  # 内存里的旧对象不受影响


def test_set_limit_and_history(store):
    account = store.create("alice", "password123")
    assert store.set_limit("alice", 999) is True
    assert store.get("alice").daily_token_limit == 999
    store.record_usage(account.id, 42, calls=3)
    history = store.usage_history(account.id)
    assert history[0]["tokens"] == 42
    assert history[0]["calls"] == 3


def test_usage_survives_reopen(tmp_path):
    path = tmp_path / "accounts.db"
    first = AccountStore(path)
    account = first.create("alice", "password123")
    first.record_usage(account.id, 77)
    first.close()

    second = AccountStore(path)
    assert second.get("alice").name == "alice"
    assert second.usage_today(account.id) == (77, 1)
