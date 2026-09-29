"""账号库迁移测试（离线）。"""

from __future__ import annotations

import sqlite3

import pytest

from agentcode.core.errors import MigrationError
from agentcode.storage.migrations import Migration, current_version, run_migrations


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
