"""用量账本测试（离线）。"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from agentcode.accounts import AccountStore


def _store(tmp_path) -> AccountStore:
    return AccountStore(tmp_path / "accounts.db")


def test_record_usage_writes_a_ledger_row(tmp_path):
    store = _store(tmp_path)
    account = store.create("alice", "password123")
    store.record_usage(account.id, 120, calls=1, run_id="run-abc")

    rows = store.ledger_rows(account.id)
    assert len(rows) == 1
    assert rows[0]["tokens"] == 120
    assert rows[0]["run_id"] == "run-abc"


def test_usage_today_sums_the_ledger(tmp_path):
    store = _store(tmp_path)
    account = store.create("alice", "password123")
    store.record_usage(account.id, 100, calls=1)
    store.record_usage(account.id, 50, calls=2)

    assert store.usage_today(account.id) == (150, 3)


def test_usage_between_aggregates_a_range(tmp_path):
    store = _store(tmp_path)
    account = store.create("alice", "password123")
    store.record_usage(account.id, 10, calls=1)
    store.record_usage(account.id, 20, calls=1, note="补一笔")
    today_total = store.usage_today(account.id)[0]

    assert store.usage_between(account.id, "2000-01-01", "2999-12-31")[0] == today_total
    assert store.usage_between(account.id, "2999-01-01", "2999-12-31") == (0, 0)


def test_usage_history_groups_by_day(tmp_path):
    store = _store(tmp_path)
    account = store.create("alice", "password123")
    store.record_usage(account.id, 30, calls=1)
    store.record_usage(account.id, 70, calls=3)

    history = store.usage_history(account.id)
    assert len(history) == 1
    assert history[0]["tokens"] == 100
    assert history[0]["calls"] == 4


def test_export_usage_csv_has_header_and_rows(tmp_path):
    store = _store(tmp_path)
    account = store.create("alice", "password123")
    store.record_usage(account.id, 42, calls=1)

    csv = store.export_usage_csv(account.id)
    lines = csv.strip().splitlines()
    assert lines[0] == "日期,Token,调用次数"
    assert lines[1].endswith(",42,1")


def test_reset_usage_clears_the_day(tmp_path):
    store = _store(tmp_path)
    account = store.create("alice", "password123")
    store.record_usage(account.id, 500)
    store.reset_usage(account.id)
    assert store.usage_today(account.id) == (0, 0)


# ---------------------------------------------------------------- 输入输出分开


def test_record_usage_keeps_the_token_split(tmp_path):
    """计价要用：输入和输出单价差好几倍。"""
    store = _store(tmp_path)
    account = store.create("alice", "password123")
    store.record_usage(
        account.id, 150, calls=1, prompt_tokens=100, completion_tokens=50
    )

    row = store.ledger_rows(account.id)[0]
    assert row["prompt_tokens"] == 100
    assert row["completion_tokens"] == 50
    assert row["tokens"] == 150


def test_record_usage_without_a_split_still_works(tmp_path):
    """调用方拿不到拆分时（比如自己估算的用量）也得能记。"""
    store = _store(tmp_path)
    account = store.create("alice", "password123")
    store.record_usage(account.id, 100)
    row = store.ledger_rows(account.id)[0]
    assert row["tokens"] == 100
    assert row["prompt_tokens"] is None


def test_usage_breakdown_aggregates_the_split(tmp_path):
    store = _store(tmp_path)
    account = store.create("alice", "password123")
    store.record_usage(account.id, 150, prompt_tokens=100, completion_tokens=50)
    store.record_usage(account.id, 90, prompt_tokens=80, completion_tokens=10)

    today = store.usage_today(account.id)
    breakdown = store.usage_breakdown(
        account.id, "2000-01-01", "2999-12-31"
    )
    assert breakdown["prompt_tokens"] == 180
    assert breakdown["completion_tokens"] == 60
    assert breakdown["total_tokens"] == today[0] == 240
    assert breakdown["calls"] == 2
    assert breakdown["rows_without_split"] == 0


def test_usage_breakdown_flags_rows_that_have_no_split(tmp_path):
    """老数据只有总数，拆不出来——要让调用方知道有多少是估的。"""
    store = _store(tmp_path)
    account = store.create("alice", "password123")
    store.record_usage(account.id, 1000)  # 老式的记法
    store.record_usage(account.id, 150, prompt_tokens=100, completion_tokens=50)

    breakdown = store.usage_breakdown(account.id, "2000-01-01", "2999-12-31")
    assert breakdown["prompt_tokens"] == 100
    assert breakdown["completion_tokens"] == 50
    assert breakdown["total_tokens"] == 1150
    assert breakdown["rows_without_split"] == 1
    assert breakdown["unsplit_tokens"] == 1000


def test_legacy_usage_table_is_migrated_into_the_ledger(tmp_path):
    """老库里的日计数器不能凭空消失。

    先造一个 v1 老库（只有 accounts / usage，没有 schema_meta），
    再让 AccountStore 打开它——这时迁移会自动跑起来。
    """
    path = tmp_path / "accounts.db"
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,"
        " password_hash TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'free',"
        " daily_token_limit INTEGER NOT NULL, is_active INTEGER NOT NULL DEFAULT 1,"
        " created_at TEXT NOT NULL)"
    )
    raw.execute(
        "CREATE TABLE usage (account_id TEXT NOT NULL, day TEXT NOT NULL,"
        " tokens INTEGER NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (account_id, day))"
    )
    today = datetime.now(timezone.utc).date().isoformat()
    raw.execute(
        "INSERT INTO accounts (id, name, password_hash, plan, daily_token_limit, is_active,"
        " created_at) VALUES ('a1', 'alice', 'x', 'free', 50000, 1, '2026-09-01T00:00:00+00:00')"
    )
    raw.execute(
        "INSERT INTO usage (account_id, day, tokens, calls) VALUES ('a1', ?, 777, 3)", (today,)
    )
    raw.commit()
    raw.close()

    store = AccountStore(path)  # 打开即迁移
    assert store.usage_today("a1") == (777, 3)
    assert store.get("alice") is not None
