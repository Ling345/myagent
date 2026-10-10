"""账号库迁移测试（离线）。"""

from __future__ import annotations

import sqlite3

import pytest

from agentcode.core.errors import MigrationError
from agentcode.storage.migrations import (
    MIGRATIONS,
    Migration,
    current_version,
    run_migrations,
)


def _connect(tmp_path) -> sqlite3.Connection:
    return sqlite3.connect(tmp_path / "db.sqlite")


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _make_legacy_database(conn: sqlite3.Connection) -> None:
    """造一个 v1 结构的老库：只有 accounts 与 usage，没有 schema_meta。"""
    conn.execute(
        "CREATE TABLE accounts (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL,"
        " password_hash TEXT NOT NULL, plan TEXT NOT NULL DEFAULT 'free',"
        " daily_token_limit INTEGER NOT NULL, is_active INTEGER NOT NULL DEFAULT 1,"
        " created_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE usage (account_id TEXT NOT NULL, day TEXT NOT NULL,"
        " tokens INTEGER NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (account_id, day))"
    )
    conn.commit()


def test_fresh_database_gets_initial_schema(tmp_path):
    conn = _connect(tmp_path)
    version = run_migrations(conn)

    assert version >= 1
    assert {"accounts", "usage"} <= _tables(conn)
    assert current_version(conn) == version


def test_migrations_are_idempotent(tmp_path):
    conn = _connect(tmp_path)
    first = run_migrations(conn)
    second = run_migrations(conn)

    assert first == second


def test_legacy_database_keeps_its_data(tmp_path):
    """老库升级后数据不能丢。"""
    conn = _connect(tmp_path)
    _make_legacy_database(conn)
    conn.execute(
        "INSERT INTO accounts (id, name, password_hash, plan, daily_token_limit, is_active,"
        " created_at) VALUES ('a1', 'alice', 'hash', 'free', 50000, 1, '2026-09-01T00:00:00+00:00')"
    )
    conn.commit()

    run_migrations(conn)

    row = conn.execute("SELECT name FROM accounts WHERE id = 'a1'").fetchone()
    assert row is not None
    assert row[0] == "alice"


def test_failed_migration_rolls_back_schema_changes(tmp_path):
    """迁移中途失败必须整体回滚——包括已经执行的 DDL。

    sqlite3 默认不把 DDL 放进隐式事务，所以这条测试是在守一个真坑：
    光用 ``with conn:`` 是回滚不掉建表语句的。
    """
    conn = _connect(tmp_path)

    def boom(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE should_not_survive (id INTEGER)")
        raise RuntimeError("故意炸")

    with pytest.raises(MigrationError, match="迁移 v1"):
        run_migrations(conn, [Migration(1, "坏的迁移", boom)])

    assert current_version(conn) == 0
    assert "should_not_survive" not in _tables(conn)


def test_migration_failure_message_tells_you_to_back_up(tmp_path):
    conn = _connect(tmp_path)

    def boom(connection: sqlite3.Connection) -> None:
        raise RuntimeError("炸")

    with pytest.raises(MigrationError, match="备份"):
        run_migrations(conn, [Migration(1, "坏的迁移", boom)])


def test_later_migration_runs_after_an_earlier_one(tmp_path):
    """版本号决定顺序，而不是列表顺序。"""
    conn = _connect(tmp_path)
    order: list[int] = []

    def first(connection: sqlite3.Connection) -> None:
        order.append(1)

    def second(connection: sqlite3.Connection) -> None:
        order.append(2)

    run_migrations(
        conn,
        [Migration(2, "第二", second), Migration(1, "第一", first)],
    )
    assert order == [1, 2]


# ---------------------------------------------------------------- v2：计费结构


def test_v2_adds_billing_columns_and_tables(tmp_path):
    conn = _connect(tmp_path)
    run_migrations(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    assert {"plan_expires_at", "plan_started_at", "limit_override"} <= columns
    assert {"usage_ledger", "orders"} <= _tables(conn)


def test_v2_migrates_legacy_usage_into_the_ledger(tmp_path):
    conn = _connect(tmp_path)
    _make_legacy_database(conn)
    conn.execute(
        "INSERT INTO usage (account_id, day, tokens, calls) VALUES ('a1', '2026-09-20', 1234, 7)"
    )
    conn.commit()

    run_migrations(conn)

    row = conn.execute(
        "SELECT account_id, day, tokens, calls, note FROM usage_ledger"
    ).fetchone()
    assert row == ("a1", "2026-09-20", 1234, 7, "历史迁移")


def test_legacy_usage_is_migrated_only_once(tmp_path):
    conn = _connect(tmp_path)
    _make_legacy_database(conn)
    conn.execute(
        "INSERT INTO usage (account_id, day, tokens, calls) VALUES ('a1', '2026-09-20', 10, 1)"
    )
    conn.commit()

    run_migrations(conn)
    run_migrations(conn)  # 再跑一次不能重复搬

    count = conn.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0]
    assert count == 1


def test_v2_ledger_has_the_expected_columns(tmp_path):
    conn = _connect(tmp_path)
    run_migrations(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(usage_ledger)")}
    assert {"account_id", "day", "created_at", "tokens", "calls", "run_id", "note"} <= columns


# ---------------------------------------------------------------- v3：输入输出分开记


def test_v3_adds_the_token_split_columns(tmp_path):
    """计价靠它：输入和输出的单价差好几倍，只记总数换算不出钱。"""
    conn = _connect(tmp_path)
    run_migrations(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(usage_ledger)")}
    assert {"prompt_tokens", "completion_tokens"} <= columns


def test_v3_leaves_old_rows_without_a_split(tmp_path):
    """老数据只有总数，拆不出来——留空，别瞎猜。"""
    conn = _connect(tmp_path)
    run_migrations(conn, [m for m in MIGRATIONS if m.version <= 2])
    conn.execute(
        "INSERT INTO usage_ledger (account_id, day, created_at, tokens, calls)"
        " VALUES ('a1', '2026-09-20', '2026-09-20T00:00:00+00:00', 100, 1)"
    )
    conn.commit()

    run_migrations(conn)

    row = conn.execute("SELECT tokens, prompt_tokens, completion_tokens FROM usage_ledger").fetchone()
    assert row[0] == 100
    assert row[1] is None
    assert row[2] is None


# ---------------------------------------------------------------- v4：审计日志


def test_v4_creates_the_audit_log(tmp_path):
    conn = _connect(tmp_path)
    run_migrations(conn)

    assert "audit_log" in _tables(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_log)")}
    assert {
        "at",
        "actor_id",
        "actor_name",
        "action",
        "target",
        "result",
        "detail",
        "ip",
    } <= columns


def test_v4_reaches_an_old_database_without_losing_it(tmp_path):
    """现役库（v1 结构 + 真实账号）升上来必须既能用、又能记审计。"""
    conn = _connect(tmp_path)
    _make_legacy_database(conn)
    conn.execute(
        "INSERT INTO accounts (id, name, password_hash, plan, daily_token_limit, is_active,"
        " created_at) VALUES ('a1', 'alice', 'hash', 'free', 50000, 1, '2026-09-01T00:00:00+00:00')"
    )
    conn.commit()

    version = run_migrations(conn)

    assert version >= 4
    assert "audit_log" in _tables(conn)
    assert conn.execute("SELECT name FROM accounts WHERE id = 'a1'").fetchone()[0] == "alice"


def test_v4_is_idempotent(tmp_path):
    conn = _connect(tmp_path)
    run_migrations(conn)
    first = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    run_migrations(conn)
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == first


# ---------------------------------------------------------------- v5：邮箱与通知


def test_v5_adds_the_email_column_and_the_notifications_table(tmp_path):
    conn = _connect(tmp_path)
    run_migrations(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    assert "email" in columns
    assert "notifications" in _tables(conn)
    notification_columns = {row[1] for row in conn.execute("PRAGMA table_info(notifications)")}
    assert {
        "created_at",
        "account_id",
        "email",
        "event",
        "dedupe_key",
        "status",
        "error",
    } <= notification_columns


def test_v5_upgrades_an_old_database_without_losing_the_rest(tmp_path):
    """v1 老库 + 真实账号升到 v5：账号还在、能填邮箱、能记通知。"""
    conn = _connect(tmp_path)
    _make_legacy_database(conn)
    conn.execute(
        "INSERT INTO accounts (id, name, password_hash, plan, daily_token_limit, is_active,"
        " created_at) VALUES ('a1', 'alice', 'hash', 'free', 50000, 1, '2026-09-01T00:00:00+00:00')"
    )
    conn.commit()

    version = run_migrations(conn)

    assert version >= 5
    conn.execute("UPDATE accounts SET email = 'alice@example.com' WHERE name = 'alice'")
    assert (
        conn.execute("SELECT email FROM accounts WHERE name = 'alice'").fetchone()[0]
        == "alice@example.com"
    )
    assert conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0


# ---------------------------------------------------------------- v8：任务完成回调


def test_v8_adds_the_callback_columns(tmp_path):
    """回调状态要落库：不落库的话，服务重启就把待发的回调弄丢了。"""
    conn = _connect(tmp_path)
    run_migrations(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(api_runs)")}
    assert {
        "callback_url",
        "callback_status",
        "callback_attempts",
        "callback_error",
        "callback_next_at",
        "callback_delivered_at",
    } <= columns


def test_v7_database_upgrades_to_v8_without_losing_runs(tmp_path):
    conn = _connect(tmp_path)
    run_migrations(conn, [m for m in MIGRATIONS if m.version <= 7])
    conn.execute(
        "INSERT INTO api_runs (run_id, account_id, agent, task, status, created_at)"
        " VALUES ('old-1', 'a1', 'echo', '老任务', 'succeeded', '2026-10-01T00:00:00+00:00')"
    )
    conn.commit()

    version = run_migrations(conn)

    assert version >= 8
    row = conn.execute(
        "SELECT task, callback_status FROM api_runs WHERE run_id = 'old-1'"
    ).fetchone()
    assert row[0] == "老任务"
    assert row[1] is None  # 老任务没有回调，留空表示"没有这回事"
