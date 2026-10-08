"""API 令牌（离线）。

这块的重点全在安全上：**只存哈希**（库被看到也不能拿去用）、明文只出现一次、
吊销/过期/停用立刻失效、一个账号的令牌只能碰自己的东西。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agentcode.accounts import AccountStore
from agentcode.tokens import (
    TOKEN_PREFIX,
    ApiTokenService,
    generate_token,
    hash_token,
)


def _store(tmp_path: Path) -> AccountStore:
    return AccountStore(tmp_path / "agentcode.db")


def _service(tmp_path: Path) -> tuple[AccountStore, ApiTokenService]:
    store = _store(tmp_path)
    return store, ApiTokenService(store)


# ---------------------------------------------------------------- 生成


def test_generated_tokens_are_long_random_and_prefixed():
    first, second = generate_token(), generate_token()
    assert first.startswith(TOKEN_PREFIX)
    assert len(first) > 30
    assert first != second  # 每次都不一样


def test_hash_is_stable_and_not_the_token():
    token = generate_token()
    assert hash_token(token) == hash_token(token)
    assert hash_token(token) != token
    assert len(hash_token(token)) == 64  # sha256 十六进制


def test_plaintext_is_returned_once_and_never_stored(tmp_path):
    """库被翻出来也不能拿去用——这是令牌这种凭据的底线。"""
    store, service = _service(tmp_path)
    account = store.create("alice", "password123")

    record, plaintext = service.create(account, name="我的脚本")

    assert plaintext.startswith(TOKEN_PREFIX)
    # 表里没有明文、也没有可逆的东西
    with sqlite3.connect(store.path) as conn:
        rows = conn.execute("SELECT * FROM api_tokens").fetchall()
    flattened = " ".join(str(value) for row in rows for value in row)
    assert plaintext not in flattened
    assert hash_token(plaintext) in flattened
    # 列表接口也不吐哈希
    listed = service.list(account.id)
    assert listed[0]["id"] == record["id"]
    assert "token_hash" not in listed[0]
    assert record["prefix"] == plaintext[: len(record["prefix"])]


def test_tokens_can_be_named_and_an_empty_name_gets_a_default(tmp_path):
    store, service = _service(tmp_path)
    account = store.create("alice", "password123")
    service.create(account, name="")
    assert service.list(account.id)[0]["name"] == "未命名令牌"


# ---------------------------------------------------------------- 认令牌


def test_a_token_resolves_to_its_account(tmp_path):
    store, service = _service(tmp_path)
    alice = store.create("alice", "password123")
    _, plaintext = service.create(alice, name="脚本")

    resolved = service.resolve(plaintext)

    assert resolved is not None
    assert resolved["account"].id == alice.id
    assert resolved["token"]["name"] == "脚本"
    # 用过之后要留下痕迹（界面上显示"最后使用时间"）
    assert service.list(alice.id)[0]["last_used_at"]


def test_one_account_cannot_use_anothers_token_to_reach_data(tmp_path):
    store, service = _service(tmp_path)
    alice = store.create("alice", "password123")
    bob = store.create("bob", "password123")
    _, alice_token = service.create(alice, name="alice 的")
    _, bob_token = service.create(bob, name="bob 的")

    assert service.resolve(alice_token)["account"].name == "alice"
    assert service.resolve(bob_token)["account"].name == "bob"


@pytest.mark.parametrize(
    "junk",
    ["", "   ", "agk_不存在的令牌", "随便一串", "Bearer agk_xxx", None, 123],
)
def test_junk_tokens_are_rejected(tmp_path, junk):
    _, service = _service(tmp_path)
    assert service.resolve(junk) is None


def test_revoked_tokens_stop_working_immediately(tmp_path):
    store, service = _service(tmp_path)
    account = store.create("alice", "password123")
    record, plaintext = service.create(account, name="要被吊销的")

    assert service.revoke(account.id, record["id"]) is True

    assert service.resolve(plaintext) is None
    assert service.list(account.id)[0]["is_active"] is False
    # 再吊销一次没有意义，但也不能报错
    assert service.revoke(account.id, record["id"]) is False


def test_one_account_cannot_revoke_anothers_token(tmp_path):
    store, service = _service(tmp_path)
    alice = store.create("alice", "password123")
    bob = store.create("bob", "password123")
    record, plaintext = service.create(alice, name="alice 的")

    assert service.revoke(bob.id, record["id"]) is False
    assert service.resolve(plaintext) is not None


def test_expired_tokens_stop_working(tmp_path):
    store, service = _service(tmp_path)
    account = store.create("alice", "password123")
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    record, plaintext = service.create(account, name="过期的", expires_at=past)

    assert service.resolve(plaintext) is None
    assert service.list(account.id)[0]["expired"] is True


def test_a_future_expiry_still_works_and_is_reported(tmp_path):
    store, service = _service(tmp_path)
    account = store.create("alice", "password123")
    _, plaintext = service.create(account, name="还有效", expires_days=30)

    assert service.resolve(plaintext) is not None
    listed = service.list(account.id)[0]
    assert listed["expired"] is False
    assert listed["expires_at"]


def test_disabling_the_account_kills_its_tokens(tmp_path):
    store, service = _service(tmp_path)
    store.create("alice", "password123")
    _, plaintext = service.create(store.get("alice"), name="脚本")
    store.set_active("alice", False)

    assert service.resolve(plaintext) is None


def test_deleting_the_account_removes_its_tokens(tmp_path):
    """注销之后令牌不能还留着——那是能继续花钱的凭据。"""
    store, service = _service(tmp_path)
    account = store.create("alice", "password123")
    _, plaintext = service.create(account, name="脚本")

    store.delete_account(account.id)

    assert service.resolve(plaintext) is None
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0] == 0


def test_several_tokens_per_account_and_each_can_be_revoked_alone(tmp_path):
    store, service = _service(tmp_path)
    account = store.create("alice", "password123")
    first, first_token = service.create(account, name="脚本")
    second, second_token = service.create(account, name="CI")

    service.revoke(account.id, first["id"])

    assert service.resolve(first_token) is None
    assert service.resolve(second_token) is not None
    assert len(service.list(account.id)) == 2
