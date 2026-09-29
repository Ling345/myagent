"""账号库的版本化迁移。

为什么需要：原来只有 ``CREATE TABLE IF NOT EXISTS``——加一列、加一张表都没有路径，
而库里躺着真实账号，直接改结构就是原地爆掉。

三条规则，别破例：

1. **只增不改**：加列用 ``ALTER TABLE ADD COLUMN``，不删列、不改类型；
2. 每条迁移**单独一个显式事务**，失败就整体回滚，版本停在上一条，库仍然可用；
3. **幂等**：版本号说了算，跑过的不再跑。

注意（踩过的坑）：``sqlite3`` 默认不会把 DDL 放进隐式事务，
所以这里必须自己 ``BEGIN``，否则迁移中途失败会留下半拉子结构。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable, Sequence

from agentcode.core.errors import MigrationError

SCHEMA_META_TABLE = "schema_meta"
VERSION_KEY = "version"


@dataclass(frozen=True)
class Migration:
    """一条迁移：版本号、说明，以及在连接上执行的函数。"""

    version: int
    description: str
    apply: Callable[[sqlite3.Connection], None]


def _ensure_meta(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {SCHEMA_META_TABLE} ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )


def current_version(conn: sqlite3.Connection) -> int:
    """当前库的 schema 版本；没有记录时是 0。"""
    _ensure_meta(conn)
    row = conn.execute(
        f"SELECT value FROM {SCHEMA_META_TABLE} WHERE key = ?", (VERSION_KEY,)
    ).fetchone()
    if row is None:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _set_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        f"INSERT INTO {SCHEMA_META_TABLE} (key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (VERSION_KEY, str(version)),
    )


def add_column(conn: sqlite3.Connection, table: str, column: str, type_: str) -> None:
    """加一列；已存在就跳过（SQLite 没有 ``ADD COLUMN IF NOT EXISTS``）。"""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_}")


def _v1_initial(conn: sqlite3.Connection) -> None:
    """初始结构：账号与按天累计的用量。

    老库跑这一段是 no-op，只是把版本号从 0 抬到 1。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            daily_token_limit INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage (
            account_id TEXT NOT NULL,
            day TEXT NOT NULL,
            tokens INTEGER NOT NULL DEFAULT 0,
            calls INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (account_id, day)
        )
        """
    )


def _v2_billing(conn: sqlite3.Connection) -> None:
    """套餐字段 + 用量账本 + 订单表，并把老的日用量搬进账本。"""
    add_column(conn, "accounts", "plan_expires_at", "TEXT")
    add_column(conn, "accounts", "plan_started_at", "TEXT")
    add_column(conn, "accounts", "limit_override", "INTEGER")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_id TEXT NOT NULL,
            day TEXT NOT NULL,
            created_at TEXT NOT NULL,
            tokens INTEGER NOT NULL,
            calls INTEGER NOT NULL,
            run_id TEXT,
            note TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ledger_account_day ON usage_ledger(account_id, day)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            plan TEXT NOT NULL,
            months INTEGER NOT NULL,
            amount_cents INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'CNY',
            status TEXT NOT NULL,
            provider TEXT NOT NULL,
            provider_ref TEXT,
            created_at TEXT NOT NULL,
            paid_at TEXT,
            note TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_account ON orders(account_id, created_at DESC)"
    )

    # 老用量搬进账本：只搬一次，靠 note 标记去重
    conn.execute(
        """
        INSERT INTO usage_ledger (account_id, day, created_at, tokens, calls, run_id, note)
        SELECT u.account_id, u.day, u.day || 'T00:00:00+00:00', u.tokens, u.calls, NULL, '历史迁移'
        FROM usage AS u
        WHERE NOT EXISTS (
            SELECT 1 FROM usage_ledger AS l
            WHERE l.account_id = u.account_id AND l.day = u.day AND l.note = '历史迁移'
        )
        """
    )


#: 迁移按版本号顺序执行；新迁移一律往后追加，绝不改老的
MIGRATIONS: list[Migration] = [
    Migration(1, "初始结构（accounts / usage）", _v1_initial),
    Migration(2, "套餐字段、用量账本与订单", _v2_billing),
]


def _apply_atomically(conn: sqlite3.Connection, migration: Migration) -> None:
    """在显式事务里跑一条迁移并推进版本号。"""
    if conn.in_transaction:  # 别把外面没提交的事务搅进来
        conn.commit()
    conn.execute("BEGIN")
    try:
        migration.apply(conn)
        _set_version(conn, migration.version)
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def run_migrations(
    conn: sqlite3.Connection, migrations: Sequence[Migration] | None = None
) -> int:
    """把库升到最新版本，返回最终版本号。"""
    chain = sorted(migrations if migrations is not None else MIGRATIONS, key=lambda m: m.version)
    _ensure_meta(conn)
    version = current_version(conn)
    for migration in chain:
        if migration.version <= version:
            continue
        try:
            _apply_atomically(conn, migration)
        except Exception as exc:  # noqa: BLE001 - 统一转成中文可读错误
            raise MigrationError(
                f"数据库迁移 v{migration.version}（{migration.description}）失败：{exc}。"
                "库已回滚到迁移前的状态，请先备份再排查。"
            ) from exc
        version = migration.version
    return version
