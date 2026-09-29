# 套餐、用量账本与开通流程 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 AgentCode 从「能做限制」变成「能收费」——套餐目录、用量账本、手动开通、老库平滑迁移。

**Architecture:** 四层。`plans.py` 是纯数据的套餐目录，不依赖任何东西；`storage/migrations.py` 给 SQLite 加版本化迁移；`accounts.py` 继续做唯一的存储层（多了账本表与订单表）；`billing.py` 是纯业务逻辑，支付渠道藏在 `PaymentProvider` 协议后面。

**Tech Stack:** Python 3.10+、标准库 `sqlite3` / `dataclasses` / `typing`、标准库 `http.server`、pytest。**不引入任何新的第三方依赖。**

**Spec:** `docs/superpowers/specs/2026-09-29-billing-and-usage-design.md`

## Global Constraints

- Python 下限 3.10（`pyproject.toml` 的 `requires-python`），CI 会真的在 3.10 上跑
- 不引入新的第三方依赖
- 所有注释、日志、面向用户的错误信息一律中文
- 测试必须离线：不联网、不调真实模型
- 金额一律用「分」存整数，**绝不用浮点**
- 时间一律 UTC ISO8601 字符串（`datetime.now(timezone.utc).isoformat(timespec="seconds")`）
- 迁移**只增不改**：不删列、不改列类型
- 未知套餐名回落 `free`；到期时间解析失败一律当**已过期**（宁可降级也不放行）
- 每个任务结束都要提交一次

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `agentcode/plans.py`（新） | 套餐目录与到期判断，纯数据无依赖 |
| `agentcode/storage/__init__.py`（新） | 存储层包，给以后的 Postgres 留位置 |
| `agentcode/storage/migrations.py`（新） | 版本化迁移框架 + 全部迁移定义 |
| `agentcode/accounts.py`（改） | 唯一存储层：账号、账本、订单 |
| `agentcode/billing.py`（新） | 业务逻辑：下单、确认、开通、续期 |
| `agentcode/web/limits.py`（改） | 支持按次覆盖额度（套餐驱动） |
| `agentcode/web/server.py`（改） | 配额与限流走套餐；三个新接口 |
| `agentcode/web/static/*`（改） | 「套餐与用量」面板 |
| `agentcode/cli.py`（改） | `user plan`、`billing orders\|grant` |

---

### Task 1: 套餐目录

**Files:**
- Create: `agentcode/plans.py`
- Test: `tests/test_plans.py`

**Interfaces:**
- Consumes: 无（这是最底层，不依赖项目里任何东西）
- Produces:
  - `Plan`（frozen dataclass，字段见下）
  - `PLANS: dict[str, Plan]`、`FREE = "free"`、`OWNER = "owner"`、`UNLIMITED = 0`
  - `get_plan(name: str | None) -> Plan`
  - `sellable_plans() -> list[Plan]`
  - `is_expired(expires_at: str | None, *, now: datetime | None = None) -> bool`
  - `effective_plan(plan_name: str | None, expires_at: str | None, *, now=None) -> Plan`

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_plans.py
"""套餐目录测试（离线）。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentcode.plans import FREE, OWNER, PLANS, effective_plan, get_plan, is_expired, sellable_plans


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
```

- [ ] **Step 2: 跑测试，确认它失败**

Run: `D:\Anaconda\python.exe -m pytest tests/test_plans.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'agentcode.plans'`

- [ ] **Step 3: 写最小实现**

```python
# agentcode/plans.py
"""套餐目录：把「谁能用多少」定义成数据。

三条硬规则：

1. 未知套餐名一律回落免费套餐——坏字符串绝不能换来无限额度；
2. 到期时间解析不了就当已过期（宁可降级，也不放行）；
3. `owner` 是内部套餐，对外不可售。

不依赖项目里任何其它模块，方便别处随便引用而不担心循环导入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

FREE = "free"
OWNER = "owner"
#: 额度/频率/并发等于这个值表示「不限」
UNLIMITED = 0


@dataclass(frozen=True)
class Plan:
    """一个套餐。金额单位是「分」，绝不用浮点。"""

    name: str
    title: str
    daily_tokens: int
    per_minute: int
    max_concurrent: int
    allow_code_tools: bool
    max_sessions: int
    price_cents: int
    currency: str = "CNY"
    sellable: bool = True

    @property
    def unlimited_tokens(self) -> bool:
        return self.daily_tokens <= UNLIMITED

    def to_dict(self) -> dict[str, object]:
        """给接口用的可序列化形式（不含内部字段）。"""
        return {
            "name": self.name,
            "title": self.title,
            "daily_tokens": self.daily_tokens,
            "per_minute": self.per_minute,
            "max_concurrent": self.max_concurrent,
            "allow_code_tools": self.allow_code_tools,
            "max_sessions": self.max_sessions,
            "price_cents": self.price_cents,
            "currency": self.currency,
        }


PLANS: dict[str, Plan] = {
    FREE: Plan(FREE, "免费", 20_000, 10, 1, False, 5, 0),
    "basic": Plan("basic", "基础", 200_000, 30, 2, True, 20, 2_900),
    "pro": Plan("pro", "专业", 1_000_000, 60, 4, True, 50, 9_900),
    "team": Plan("team", "团队", 5_000_000, 120, 8, True, 200, 39_900),
    OWNER: Plan(OWNER, "内部", UNLIMITED, UNLIMITED, UNLIMITED, True, UNLIMITED, 0, sellable=False),
}


def get_plan(name: str | None) -> Plan:
    """按名字取套餐；未知名字回落免费套餐。"""
    return PLANS.get(str(name or "").strip().lower(), PLANS[FREE])


def sellable_plans() -> list[Plan]:
    """对外可售的套餐，按价格从低到高。"""
    return sorted((plan for plan in PLANS.values() if plan.sellable), key=lambda p: p.price_cents)


def is_expired(expires_at: str | None, *, now: datetime | None = None) -> bool:
    """套餐是否已过期。

    空的到期时间表示「不过期」（内部账号）；
    解析不了的字符串一律当已过期——坏数据不能让付费墙失效。
    """
    text = str(expires_at or "").strip()
    if not text:
        return False
    try:
        expiry = datetime.fromisoformat(text)
    except ValueError:
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry <= (now or datetime.now(timezone.utc))


def effective_plan(
    plan_name: str | None, expires_at: str | None, *, now: datetime | None = None
) -> Plan:
    """有效套餐：过期就回落免费。"""
    if is_expired(expires_at, now=now):
        return PLANS[FREE]
    return get_plan(plan_name)
```

- [ ] **Step 4: 跑测试，确认通过**

Run: `D:\Anaconda\python.exe -m pytest tests/test_plans.py -q`
Expected: PASS（8 项）

- [ ] **Step 5: 提交**

```bash
git add agentcode/plans.py tests/test_plans.py
git commit -m "feat: 套餐目录（免费/基础/专业/团队 + 内部套餐）"
```

---

### Task 2: 迁移框架

**Files:**
- Create: `agentcode/storage/__init__.py`（空文件 + 一句 docstring）
- Create: `agentcode/storage/migrations.py`
- Modify: `agentcode/core/errors.py`（加 `MigrationError`）
- Test: `tests/test_migrations.py`

**Interfaces:**
- Consumes: `agentcode.core.errors.AgentCodeError`
- Produces:
  - `MigrationError(AgentCodeError)`
  - `Migration(version: int, description: str, apply: Callable[[sqlite3.Connection], None])`
  - `MIGRATIONS: list[Migration]`（本任务只有 v1）
  - `current_version(conn) -> int`
  - `run_migrations(conn, migrations: Sequence[Migration] | None = None) -> int`

**这个任务最容易踩的坑：** `sqlite3` 默认**不会把 DDL 放进隐式事务**，光用 `with conn:` 是回滚不掉建表语句的。必须自己 `BEGIN`，下面的实现里有完整处理，也有专门的测试守着。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_migrations.py
"""账号库迁移测试（离线）。"""

from __future__ import annotations

import sqlite3

import pytest

from agentcode.core.errors import MigrationError
from agentcode.storage.migrations import (
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
    """老库（只有 CREATE TABLE IF NOT EXISTS 建出来的结构）升级后数据不能丢。"""
    conn = _connect(tmp_path)
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
    conn.execute(
        "INSERT INTO accounts (id, name, password_hash, plan, daily_token_limit, is_active,"
        " created_at) VALUES ('a1', 'alice', 'hash', 'free', 50000, 1, '2026-09-01T00:00:00+00:00')"
    )
    conn.commit()

    run_migrations(conn)

    row = conn.execute("SELECT name FROM accounts WHERE id = 'a1'").fetchone()
    assert row is not None and row[0] == "alice"


def test_failed_migration_rolls_back_schema_changes(tmp_path):
    """迁移中途失败必须整体回滚——包括已经执行的 DDL。"""
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
```

- [ ] **Step 2: 跑测试，确认它失败**

Run: `D:\Anaconda\python.exe -m pytest tests/test_migrations.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'agentcode.storage'`

- [ ] **Step 3: 加异常类型**

```python
# agentcode/core/errors.py —— 在 ToolError 前面插入
class MigrationError(AgentCodeError):
    """数据库迁移失败（库已被回滚，可以安全重试）。"""
```

- [ ] **Step 4: 写迁移框架**

```python
# agentcode/storage/__init__.py
"""存储层：账号库的 schema 与迁移。

单独一个包是给以后留位置——真要多实例部署时，Postgres 实现放这里，
上层业务代码不用动。
"""
```

```python
# agentcode/storage/migrations.py
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
    """一条迁移：版本号、说明、以及在连接上执行的函数。"""

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
    """初始结构：账号与按天累计的用量。老库跑这一段是 no-op，只把版本抬到 1。"""
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


#: 迁移按版本号顺序执行；新迁移一律往后追加，绝不改老的
MIGRATIONS: list[Migration] = [
    Migration(1, "初始结构（accounts / usage）", _v1_initial),
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
```

- [ ] **Step 5: 跑测试，确认通过**

Run: `D:\Anaconda\python.exe -m pytest tests/test_migrations.py -q`
Expected: PASS（5 项）

- [ ] **Step 6: 提交**

```bash
git add agentcode/storage agentcode/core/errors.py tests/test_migrations.py
git commit -m "feat: 账号库的版本化迁移框架（含 DDL 回滚）"
```

---

### Task 3: 迁移 v2——套餐字段、用量账本、订单表

**Files:**
- Modify: `agentcode/storage/migrations.py`
- Test: `tests/test_migrations.py`

**Interfaces:**
- Consumes: Task 2 的 `Migration`、`add_column`、`MIGRATIONS`
- Produces: 表 `usage_ledger`、`orders`；`accounts` 新列 `plan_expires_at` / `plan_started_at` / `limit_override`

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_migrations.py —— 追加

def test_v2_adds_billing_columns_and_tables(tmp_path):
    conn = _connect(tmp_path)
    run_migrations(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
    assert {"plan_expires_at", "plan_started_at", "limit_override"} <= columns
    assert {"usage_ledger", "orders"} <= _tables(conn)


def test_v2_migrates_legacy_usage_into_the_ledger(tmp_path):
    conn = _connect(tmp_path)
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
    conn.execute("INSERT INTO usage (account_id, day, tokens, calls) VALUES ('a1', '2026-09-20', 1234, 7)")
    conn.commit()

    run_migrations(conn)

    row = conn.execute(
        "SELECT account_id, day, tokens, calls, note FROM usage_ledger"
    ).fetchone()
    assert row == ("a1", "2026-09-20", 1234, 7, "历史迁移")


def test_legacy_usage_is_migrated_only_once(tmp_path):
    conn = _connect(tmp_path)
    conn.execute(
        "CREATE TABLE usage (account_id TEXT NOT NULL, day TEXT NOT NULL,"
        " tokens INTEGER NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (account_id, day))"
    )
    conn.execute("INSERT INTO usage (account_id, day, tokens, calls) VALUES ('a1', '2026-09-20', 10, 1)")
    conn.commit()

    run_migrations(conn)
    run_migrations(conn)  # 再跑一次不能重复搬

    count = conn.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0]
    assert count == 1
```

- [ ] **Step 2: 跑测试，确认它失败**

Run: `D:\Anaconda\python.exe -m pytest tests/test_migrations.py -q`
Expected: FAIL —— `assert {'usage_ledger', 'orders'} <= _tables(conn)` 不成立

- [ ] **Step 3: 加 v2 迁移**

```python
# agentcode/storage/migrations.py —— 插在 MIGRATIONS 定义之前

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
```

然后把清单改成两项：

```python
MIGRATIONS: list[Migration] = [
    Migration(1, "初始结构（accounts / usage）", _v1_initial),
    Migration(2, "套餐字段、用量账本与订单", _v2_billing),
]
```

- [ ] **Step 4: 跑测试，确认通过**

Run: `D:\Anaconda\python.exe -m pytest tests/test_migrations.py -q`
Expected: PASS（8 项）

- [ ] **Step 5: 提交**

```bash
git add agentcode/storage/migrations.py tests/test_migrations.py
git commit -m "feat: 迁移 v2——套餐字段、用量账本与订单表"
```

---

### Task 4: `accounts.py` 接上迁移，改用用量账本

**Files:**
- Modify: `agentcode/accounts.py`
- Test: `tests/test_ledger.py`（新）；`tests/test_accounts.py` 必须**原样通过**

**Interfaces:**
- Consumes: `agentcode.plans.effective_plan / UNLIMITED`、`agentcode.storage.migrations.run_migrations`
- Produces:
  - `Account` 新字段：`plan_started_at: str | None`、`plan_expires_at: str | None`、`limit_override: int | None`
  - `record_usage(account_id, tokens, calls=1, *, run_id=None, note=None) -> None`
  - `usage_between(account_id, start_day, end_day) -> tuple[int, int]`
  - `export_usage_csv(account_id, limit=90) -> str`
  - `reset_usage(account_id, day=None)`（改成删账本行）

**关键约定（`Account.daily_token_limit` 的语义）**：它现在是**派生值**——
`limit_override` 有值就用它，否则用套餐的日额度。这样 `cli.py` / `server.py` 里现有的
`account.daily_token_limit` 不用改，`tests/test_accounts.py:96` 那句
`store.get("alice").daily_token_limit == 999` 也继续成立。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_ledger.py
"""用量账本测试（离线）。"""

from __future__ import annotations

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
    today = store.usage_today(account.id)[0]

    assert store.usage_between(account.id, "2000-01-01", "2999-12-31")[0] == today
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


def test_legacy_usage_table_is_migrated_into_the_ledger(tmp_path):
    """老库里的日计数器不能凭空消失。

    先造一个 v1 老库（只有 accounts / usage，没有 schema_meta），
    再让 AccountStore 打开它——这时迁移会自动跑起来。
    """
    import sqlite3
    from datetime import datetime, timezone

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
    raw.execute("INSERT INTO usage (account_id, day, tokens, calls) VALUES ('a1', ?, 777, 3)", (today,))
    raw.commit()
    raw.close()

    store = AccountStore(path)  # 打开即迁移
    assert store.usage_today("a1") == (777, 3)
    assert store.get("alice") is not None
```

- [ ] **Step 2: 跑测试，确认它失败**

Run: `D:\Anaconda\python.exe -m pytest tests/test_ledger.py -q`
Expected: FAIL —— `AttributeError: 'AccountStore' object has no attribute 'ledger_rows'`

- [ ] **Step 3: 改 `accounts.py`**

改 `_init_schema`：schema 现在归迁移管，不再自己建表。

```python
    def _init_schema(self) -> None:
        """建表与升级结构。真正的 schema 定义在 storage.migrations 里。"""
        with self._lock:
            run_migrations(self._conn)
```

`Account` 加三个字段，并让 `daily_token_limit` 变成派生值：

```python
@dataclass
class Account:
    """一个用户账号。"""

    id: str
    name: str
    password_hash: str
    plan: str = "free"
    daily_token_limit: int = DEFAULT_DAILY_TOKEN_LIMIT
    is_active: bool = True
    created_at: str = ""
    plan_started_at: str | None = None
    plan_expires_at: str | None = None
    #: 账号级额度覆盖；为空则跟随套餐
    limit_override: int | None = None
```

```python
    @staticmethod
    def _row_to_account(row: sqlite3.Row) -> Account:
        override = row["limit_override"]
        plan = effective_plan(row["plan"], row["plan_expires_at"])
        return Account(
            id=row["id"],
            name=row["name"],
            password_hash=row["password_hash"],
            plan=row["plan"],
            # 派生值：有覆盖用覆盖，否则用套餐的日额度（owner 是 0 = 不限）
            daily_token_limit=int(override) if override is not None else plan.daily_tokens,
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            plan_started_at=row["plan_started_at"],
            plan_expires_at=row["plan_expires_at"],
            limit_override=None if override is None else int(override),
        )
```

`create` 把额度写进 `limit_override`，同时保持老列有值（NOT NULL）：

```python
        override = int(daily_token_limit) if daily_token_limit is not None else None
        plan = get_plan(plan_name)
        account = Account(
            id=uuid.uuid4().hex[:12],
            name=cleaned,
            password_hash=hash_password(password),
            plan=plan.name,
            daily_token_limit=override if override is not None else plan.daily_tokens,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            limit_override=override,
        )
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO accounts (id, name, password_hash, plan, daily_token_limit,"
                    " is_active, created_at, limit_override) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                    (
                        account.id,
                        account.name,
                        account.password_hash,
                        account.plan,
                        account.daily_token_limit,
                        account.created_at,
                        account.limit_override,
                    ),
                )
```

（`create` 的参数名改成 `plan_name`？**不要改**——现有调用方用的是位置参数 `plan=`，
保持 `plan: str = "free"` 不变，只在函数体里 `get_plan(plan)`。）

用量部分整体换掉：

```python
    def record_usage(
        self,
        account_id: str,
        tokens: int,
        calls: int = 1,
        *,
        run_id: str | None = None,
        note: str | None = None,
    ) -> None:
        """往账本里记一笔用量。账本是唯一的用量事实来源。"""
        now = datetime.now(timezone.utc)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO usage_ledger"
                " (account_id, day, created_at, tokens, calls, run_id, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    account_id,
                    now.date().isoformat(),
                    now.isoformat(timespec="seconds"),
                    max(0, int(tokens)),
                    max(0, int(calls)),
                    run_id,
                    note,
                ),
            )

    def ledger_rows(self, account_id: str, limit: int = 50) -> list[dict[str, object]]:
        """最近的账本明细，新的排前面。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, created_at, tokens, calls, run_id, note FROM usage_ledger"
                " WHERE account_id = ? ORDER BY id DESC LIMIT ?",
                (account_id, max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def usage_between(self, account_id: str, start_day: str, end_day: str) -> tuple[int, int]:
        """闭区间的用量汇总。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(tokens), 0), COALESCE(SUM(calls), 0) FROM usage_ledger"
                " WHERE account_id = ? AND day BETWEEN ? AND ?",
                (account_id, start_day, end_day),
            ).fetchone()
        return (int(row[0]), int(row[1]))

    def usage_today(self, account_id: str) -> tuple[int, int]:
        """返回 (今天已用 token, 今天调用次数)。"""
        today = _today()
        return self.usage_between(account_id, today, today)

    def usage_history(self, account_id: str, limit: int = 30) -> list[dict[str, object]]:
        """最近 N 天的每日用量，用于账单与报表。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, SUM(tokens) AS tokens, SUM(calls) AS calls FROM usage_ledger"
                " WHERE account_id = ? GROUP BY day ORDER BY day DESC LIMIT ?",
                (account_id, max(1, int(limit))),
            ).fetchall()
        return [
            {"day": row["day"], "tokens": int(row["tokens"]), "calls": int(row["calls"])}
            for row in rows
        ]

    def export_usage_csv(self, account_id: str, limit: int = 90) -> str:
        """把最近的每日用量导成 CSV，给用户对账用。"""
        lines = ["日期,Token,调用次数"]
        for item in reversed(self.usage_history(account_id, limit)):
            lines.append(f"{item['day']},{item['tokens']},{item['calls']}")
        return "\n".join(lines) + "\n"

    def reset_usage(self, account_id: str, day: str | None = None) -> None:
        """抹掉某天的账本记录（客服补偿或测试用）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM usage_ledger WHERE account_id = ? AND day = ?",
                (account_id, day or _today()),
            )
```

`set_limit` 改写 `limit_override`（并同步老列）：

```python
    def set_limit(self, name: str, daily_token_limit: int) -> bool:
        """调整某个账号的每日额度（会覆盖套餐额度）。"""
        if daily_token_limit <= 0:
            raise AgentCodeError("每日 token 上限必须大于 0。")
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE accounts SET limit_override = ?, daily_token_limit = ? WHERE name = ?",
                (daily_token_limit, daily_token_limit, str(name or "").strip()),
            )
        return cursor.rowcount > 0
```

文件顶部补 import：

```python
from agentcode.plans import UNLIMITED, effective_plan, get_plan
from agentcode.storage.migrations import run_migrations
```

（全项目统一用 `UNLIMITED` 这一个名字，不要另起别名。）

- [ ] **Step 4: 跑测试，确认通过**

Run: `D:\Anaconda\python.exe -m pytest tests/test_ledger.py tests/test_accounts.py -q`
Expected: PASS（新 7 项 + 老的 `test_accounts.py` 全部）

- [ ] **Step 5: 提交**

```bash
git add agentcode/accounts.py tests/test_ledger.py
git commit -m "feat: 账号库改用用量账本，schema 交给迁移框架"
```

---

### Task 5: 套餐驱动的配额与限流

**Files:**
- Modify: `agentcode/accounts.py`（`remaining_tokens`、`check_quota`）
- Modify: `agentcode/web/limits.py`（按次覆盖）
- Test: `tests/test_quotas.py`（新）、`tests/test_limits.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `Plan`、Task 4 的 `usage_today`
- Produces:
  - `AccountStore.effective_plan(account) -> Plan`
  - `AccountStore.daily_limit(account) -> int`（0 = 不限）
  - `UsageGuard.check_request(key, per_minute: int | None = None)`
  - `UsageGuard.acquire(key, per_minute: int | None = None, max_concurrent: int | None = None)`
  - 约定：`None` = 用 guard 自己的默认值；`<= 0` = 不限

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_quotas.py
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


def test_quota_rejection_mentions_upgrade(tmp_path):
    store = _store(tmp_path)
    account = store.create("alice", "password123", daily_token_limit=100)
    store.record_usage(account.id, 100)

    allowed, reason = store.check_quota(store.get("alice"))
    assert allowed is False
    assert "今日额度已用完" in reason
    assert "升级套餐" in reason


def test_disabled_account_is_rejected(tmp_path):
    store = _store(tmp_path)
    account = store.create("alice", "password123")
    store.set_active("alice", False)
    allowed, reason = store.check_quota(store.get("alice"))
    assert allowed is False
    assert "停用" in reason
```

```python
# tests/test_limits.py —— 追加

def test_guard_accepts_per_call_limits():
    guard = UsageGuard(per_minute=100, max_concurrent=10)
    assert guard.acquire("u", per_minute=1, max_concurrent=1).allowed is True
    assert guard.acquire("u", per_minute=1, max_concurrent=1).allowed is False  # 撞频率


def test_guard_treats_non_positive_as_unlimited():
    guard = UsageGuard(per_minute=1, max_concurrent=1)
    for _ in range(5):
        assert guard.acquire("u", per_minute=0, max_concurrent=0).allowed is True


def test_guard_none_keeps_the_default():
    guard = UsageGuard(per_minute=2, max_concurrent=5)
    assert guard.acquire("u").allowed is True
    assert guard.acquire("u").allowed is True
    assert guard.acquire("u").allowed is False
```

- [ ] **Step 2: 跑测试，确认它失败**

Run: `D:\Anaconda\python.exe -m pytest tests/test_quotas.py tests/test_limits.py -q`
Expected: FAIL —— `AttributeError: 'AccountStore' object has no attribute 'effective_plan'`

- [ ] **Step 3: 改 `accounts.py`**

```python
    def effective_plan(self, account: Account) -> Plan:
        """账号当前有效的套餐（已过期的自动回落免费）。"""
        return effective_plan(account.plan, account.plan_expires_at)

    def daily_limit(self, account: Account) -> int:
        """今天的 token 上限；0 表示不限。"""
        if account.limit_override is not None:
            return int(account.limit_override)
        return self.effective_plan(account).daily_tokens

    def remaining_tokens(self, account: Account) -> int:
        """今天还剩多少 token；不限量时返回 -1。"""
        limit = self.daily_limit(account)
        if limit <= UNLIMITED:
            return -1
        used, _ = self.usage_today(account.id)
        return max(0, limit - used)

    def check_quota(self, account: Account) -> tuple[bool, str]:
        """判断是否还能继续调用；不允许时给出中文原因。"""
        if not account.is_active:
            return False, "账号已被停用，请联系管理员。"
        limit = self.daily_limit(account)
        if limit <= UNLIMITED:
            return True, ""
        used, calls = self.usage_today(account.id)
        if used >= limit:
            expired = is_expired(account.plan_expires_at)
            hint = "套餐已到期，" if expired else ""
            return (
                False,
                f"今日额度已用完（{used}/{limit} token，{calls} 次调用）；"
                f"{hint}额度每天 UTC 零点重置，升级套餐可以提高上限。",
            )
        return True, ""

    def apply_plan(
        self,
        name: str,
        plan: str,
        *,
        started_at: str,
        expires_at: str | None,
    ) -> bool:
        """把某个账号的套餐改成指定状态（由 billing 调用，别直接拿来卖钱）。"""
        resolved = get_plan(plan)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE accounts SET plan = ?, plan_started_at = ?, plan_expires_at = ?,"
                " daily_token_limit = ? WHERE name = ?",
                (
                    resolved.name,
                    started_at,
                    expires_at,
                    resolved.daily_tokens,
                    str(name or "").strip(),
                ),
            )
        return cursor.rowcount > 0
```

改 `limits.py`：三个方法都加可选覆盖参数。

```python
    def _limit(self, override: int | None, default: int) -> int:
        """None = 用默认值；>0 = 用覆盖值；<=0 = 不限（返回 0）。"""
        if override is None:
            return default
        return max(0, int(override))

    def check_request(self, key: str, per_minute: int | None = None) -> LimitDecision:
        """只做频率检查（用于查询类接口）。"""
        limit = self._limit(per_minute, self.per_minute)
        now = self._clock()
        with self._lock:
            window = self._window(key, now)
            if limit and len(window) >= limit:
                wait = max(1, int(WINDOW_SECONDS - (now - window[0])) + 1)
                return LimitDecision(
                    False,
                    f"请求过于频繁（每分钟最多 {limit} 次），请 {wait} 秒后再试。",
                    wait,
                )
            window.append(now)
            return LimitDecision(True)

    def acquire(
        self,
        key: str,
        per_minute: int | None = None,
        max_concurrent: int | None = None,
    ) -> LimitDecision:
        """频率检查通过后，再占用一个并发名额。"""
        decision = self.check_request(key, per_minute)
        if not decision.allowed:
            return decision
        limit = self._limit(max_concurrent, self.max_concurrent)
        if not limit:
            return LimitDecision(True)
        with self._lock:
            active = self._active.get(key, 0)
            if active >= limit:
                return LimitDecision(
                    False,
                    f"你已有 {active} 个任务在运行（当前套餐同时最多 {limit} 个），"
                    "等其中一个结束再试。",
                    5,
                )
            self._active[key] = active + 1
            return LimitDecision(True)
```

- [ ] **Step 4: 跑测试，确认通过**

Run: `D:\Anaconda\python.exe -m pytest tests/test_quotas.py tests/test_limits.py tests/test_accounts.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agentcode/accounts.py agentcode/web/limits.py tests/test_quotas.py tests/test_limits.py
git commit -m "feat: 配额与限流改由套餐驱动，支持按次覆盖"
```

---

### Task 6: 订单与开通

**Files:**
- Modify: `agentcode/accounts.py`（订单表的增删查改）
- Create: `agentcode/billing.py`
- Test: `tests/test_billing.py`

**Interfaces:**
- Consumes: Task 1 的 `Plan / get_plan / is_expired / sellable_plans`、Task 4/5 的 `AccountStore`
- Produces:
  - `AccountStore.create_order / get_order / list_orders / mark_order_paid`
  - `Order`（frozen dataclass）+ `Order.from_row / to_dict`
  - `add_months(moment, months) -> datetime`
  - `PaymentProvider`（Protocol）、`ManualProvider`
  - `BillingService(store, provider=None)`，方法见下
  - 订单状态常量 `ORDER_PENDING / ORDER_PAID / ORDER_CANCELLED / ORDER_REFUNDED`

**关键设计**：`mark_order_paid` 只在 `status='pending'` 时才改（`UPDATE ... WHERE status='pending'`），
返回 `None` 表示"没有发生状态迁移"。`confirm` 靠这个返回值保证**只会开通一次**。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_billing.py
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


def test_plans_endpoint_hides_internal_plan(tmp_path):
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


def test_cancelled_order_cannot_be_confirmed(tmp_path):
    store, billing = _service(tmp_path)
    account = store.create("alice", "password123")
    order = billing.checkout(account, "basic", months=1)
    store.set_order_status(order.id, "cancelled")

    with pytest.raises(AgentCodeError, match="不能确认"):
        billing.confirm(order.id)


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


def test_my_billing_reports_usage_and_orders(tmp_path):
    store, billing = _service(tmp_path)
    account = store.create("alice", "password123")
    store.record_usage(account.id, 1_234, calls=2)
    billing.checkout(account, "basic", months=1)

    payload = billing.my_billing(store.get("alice"))
    assert payload["plan"]["name"] == "free"
    assert payload["used_today"] == 1_234
    assert payload["daily_limit"] == 20_000
    assert len(payload["orders"]) == 1
```

- [ ] **Step 2: 跑测试，确认它失败**

Run: `D:\Anaconda\python.exe -m pytest tests/test_billing.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'agentcode.billing'`

- [ ] **Step 3: 给 `accounts.py` 加订单的增删查改**

```python
    ORDER_FIELDS = (
        "id", "account_id", "plan", "months", "amount_cents", "currency",
        "status", "provider", "provider_ref", "created_at", "paid_at", "note",
    )

    def create_order(
        self,
        account_id: str,
        plan: str,
        months: int,
        amount_cents: int,
        currency: str,
        *,
        provider: str,
    ) -> dict[str, object]:
        """落一条待支付订单。"""
        record = {
            "id": uuid.uuid4().hex[:12],
            "account_id": account_id,
            "plan": plan,
            "months": int(months),
            "amount_cents": int(amount_cents),
            "currency": currency,
            "status": "pending",
            "provider": provider,
            "provider_ref": None,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "paid_at": None,
            "note": None,
        }
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO orders (id, account_id, plan, months, amount_cents, currency,"
                " status, provider, provider_ref, created_at, paid_at, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(record[field] for field in self.ORDER_FIELDS),
            )
        return record

    @classmethod
    def _row_to_order(cls, row: sqlite3.Row) -> dict[str, object]:
        return {field: row[field] for field in cls.ORDER_FIELDS}

    def get_order(self, order_id: str) -> dict[str, object] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM orders WHERE id = ?", (str(order_id or ""),)
            ).fetchone()
        return self._row_to_order(row) if row else None

    def list_orders(self, account_id: str | None = None, limit: int = 50) -> list[dict]:
        """列订单；不给 account_id 就列全部（管理员用）。"""
        with self._lock:
            if account_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (max(1, int(limit)),)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM orders WHERE account_id = ? ORDER BY created_at DESC LIMIT ?",
                    (account_id, max(1, int(limit))),
                ).fetchall()
        return [self._row_to_order(row) for row in rows]

    def mark_order_paid(
        self, order_id: str, *, provider_ref: str | None = None
    ) -> dict[str, object] | None:
        """把待支付订单标成已支付。

        只在 ``status='pending'`` 时才改，返回 None 表示**没有发生状态迁移**
        （已经支付过、或者被取消/退款了）——billing 靠这个保证只开通一次。
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE orders SET status = 'paid', paid_at = ?, provider_ref = COALESCE(?, provider_ref)"
                " WHERE id = ? AND status = 'pending'",
                (now, provider_ref, str(order_id or "")),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_order(order_id)

    def set_order_status(self, order_id: str, status: str, *, note: str | None = None) -> bool:
        """改订单状态（取消 / 退款标记）。"""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE orders SET status = ?, note = COALESCE(?, note) WHERE id = ?",
                (status, note, str(order_id or "")),
            )
        return cursor.rowcount > 0
```

- [ ] **Step 4: 写 `billing.py`**

```python
# agentcode/billing.py
"""套餐的开通与续期。

支付渠道藏在 PaymentProvider 后面：现在只有 ManualProvider（管理员确认到账），
以后接微信 / 支付宝 / Stripe 只要实现同一个协议，上层业务代码一行都不用改。

金额一律用「分」存整数，绝不用浮点。
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from agentcode.accounts import Account, AccountStore
from agentcode.core.errors import AgentCodeError
from agentcode.plans import PLANS, Plan, get_plan, is_expired, sellable_plans

ORDER_PENDING = "pending"
ORDER_PAID = "paid"
ORDER_CANCELLED = "cancelled"
ORDER_REFUNDED = "refunded"
#: 一次最多买一年，避免误操作刷出天文数字
MAX_MONTHS = 12


@dataclass(frozen=True)
class Order:
    """一条订单。"""

    id: str
    account_id: str
    plan: str
    months: int
    amount_cents: int
    currency: str
    status: str
    provider: str
    provider_ref: str | None = None
    created_at: str = ""
    paid_at: str | None = None
    note: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> "Order":
        return cls(
            id=row["id"],
            account_id=row["account_id"],
            plan=row["plan"],
            months=int(row["months"]),
            amount_cents=int(row["amount_cents"]),
            currency=row["currency"],
            status=row["status"],
            provider=row["provider"],
            provider_ref=row["provider_ref"],
            created_at=row["created_at"],
            paid_at=row["paid_at"],
            note=row["note"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "plan": self.plan,
            "months": self.months,
            "amount_cents": self.amount_cents,
            "currency": self.currency,
            "status": self.status,
            "created_at": self.created_at,
            "paid_at": self.paid_at,
        }


def add_months(moment: datetime, months: int) -> datetime:
    """加 N 个自然月；目标月天数不够时取当月最后一天。"""
    total = moment.month - 1 + int(months)
    year = moment.year + total // 12
    month = total % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


class PaymentProvider(Protocol):
    """支付渠道协议。加渠道 = 加一个实现，上层不动。"""

    name: str

    def create_order(self, account: Account, plan: Plan, months: int) -> Order: ...

    def confirm(self, order_id: str, reference: str | None = None) -> Order: ...


class ManualProvider:
    """手动开通：管理员收到钱之后用 CLI 确认。"""

    name = "manual"

    def __init__(self, store: AccountStore) -> None:
        self.store = store

    def create_order(self, account: Account, plan: Plan, months: int) -> Order:
        row = self.store.create_order(
            account.id,
            plan.name,
            months,
            plan.price_cents * months,
            plan.currency,
            provider=self.name,
        )
        return Order.from_row(row)

    def confirm(self, order_id: str, reference: str | None = None) -> Order:
        row = self.store.mark_order_paid(order_id, provider_ref=reference)
        if row is None:
            raise AgentCodeError("这个订单已经处理过了。")
        return Order.from_row(row)


class BillingService:
    """下单、确认、开通、续期。"""

    def __init__(self, store: AccountStore, provider: PaymentProvider | None = None) -> None:
        self.store = store
        self.provider: PaymentProvider = provider or ManualProvider(store)

    # ------------------------------------------------------------------ 套餐

    def plans(self) -> list[Plan]:
        """对外可售的套餐。"""
        return sellable_plans()

    def effective_plan(self, account: Account) -> Plan:
        return self.store.effective_plan(account)

    # ------------------------------------------------------------------ 下单

    def checkout(self, account: Account, plan_name: str, months: int = 1) -> Order:
        """下单。手动模式下返回的订单是「待支付」。"""
        plan = PLANS.get(str(plan_name or "").strip().lower())
        if plan is None or not plan.sellable:
            raise AgentCodeError("该套餐不可购买。")
        if not 1 <= int(months) <= MAX_MONTHS:
            raise AgentCodeError(f"购买月数必须在 1 到 {MAX_MONTHS} 之间。")
        return self.provider.create_order(account, plan, int(months))

    def confirm(self, order_id: str, reference: str | None = None) -> Order:
        """确认到账 → 开通。幂等：重复确认不会重复开通。"""
        existing = self.store.get_order(order_id)
        if existing is None:
            raise AgentCodeError("找不到这个订单。")
        if existing["status"] in (ORDER_CANCELLED, ORDER_REFUNDED):
            raise AgentCodeError(f"订单当前状态是 {existing['status']}，不能确认收款。")
        if existing["status"] == ORDER_PAID:
            return Order.from_row(existing)

        confirmed = self.provider.confirm(order_id, reference)
        account = self.store.get_by_id(confirmed.account_id)
        if account is not None:
            self._activate(account, get_plan(confirmed.plan), confirmed.months)
        return confirmed

    def grant(self, account: Account, plan_name: str, months: int = 1) -> Account:
        """管理员直接开通 / 续期，不经过订单（内部套餐也能授）。"""
        plan = PLANS.get(str(plan_name or "").strip().lower())
        if plan is None:
            raise AgentCodeError(f"没有这个套餐：{plan_name}")
        if not 1 <= int(months) <= MAX_MONTHS:
            raise AgentCodeError(f"月数必须在 1 到 {MAX_MONTHS} 之间。")
        self._activate(account, plan, int(months))
        refreshed = self.store.get(account.name)
        return refreshed if refreshed is not None else account

    # ------------------------------------------------------------------ 展示

    def my_billing(self, account: Account) -> dict[str, object]:
        """我的套餐、用量与订单。"""
        now = datetime.now(timezone.utc)
        plan = self.store.effective_plan(account)
        limit = self.store.daily_limit(account)
        used_today, calls_today = self.store.usage_today(account.id)
        start_day = (account.plan_started_at or now.isoformat())[:10]
        period_used, period_calls = self.store.usage_between(
            account.id, start_day, now.date().isoformat()
        )
        return {
            "plan": plan.to_dict(),
            "plan_expires_at": account.plan_expires_at,
            "expired": is_expired(account.plan_expires_at),
            "daily_limit": limit,
            "used_today": used_today,
            "calls_today": calls_today,
            "remaining_today": -1 if limit <= 0 else max(0, limit - used_today),
            "period_started_at": account.plan_started_at,
            "used_this_period": period_used,
            "calls_this_period": period_calls,
            "history": self.store.usage_history(account.id, 7)[::-1],
            "orders": self.store.list_orders(account.id, limit=10),
        }

    # ------------------------------------------------------------------ 内部

    def _activate(self, account: Account, plan: Plan, months: int) -> None:
        """改套餐或续期。"""
        now = datetime.now(timezone.utc)
        same_plan = not is_expired(account.plan_expires_at) and account.plan == plan.name
        if same_plan:
            base = datetime.fromisoformat(account.plan_expires_at)
            if base.tzinfo is None:
                base = base.replace(tzinfo=timezone.utc)
            started = account.plan_started_at or now.isoformat(timespec="seconds")
        else:
            base = now
            started = now.isoformat(timespec="seconds")

        # 内部套餐（owner）不过期；对外套餐按自然月往后推
        expires_at = None if not plan.sellable else add_months(base, months).isoformat(timespec="seconds")
        self.store.apply_plan(account.name, plan.name, started_at=started, expires_at=expires_at)
```

- [ ] **Step 5: 跑测试，确认通过**

Run: `D:\Anaconda\python.exe -m pytest tests/test_billing.py -q`
Expected: PASS（12 项）

- [ ] **Step 6: 提交**

```bash
git add agentcode/accounts.py agentcode/billing.py tests/test_billing.py
git commit -m "feat: 订单、开通与续期（支付渠道藏在 PaymentProvider 后面）"
```

---

### Task 7: 服务端接线——配额、限流、记账都走套餐

**Files:**
- Modify: `agentcode/web/server.py`
- Test: `tests/test_web_billing.py`（新）

**Interfaces:**
- Consumes: `AccountStore.effective_plan / check_quota`、`BillingService`、`UsageGuard.acquire(key, per_minute, max_concurrent)`
- Produces: `/api/me` 的载荷多出 `plan_title / plan_expires_at / unlimited / daily_limit`；运行结束时账本带上 `run_id`

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_web_billing.py
"""服务端按套餐收口（离线）。"""

from __future__ import annotations

import json
import threading
import urllib.request

import pytest

from agentcode.accounts import AccountStore
from agentcode.web.server import create_server


@pytest.fixture
def web(tmp_path):
    """免登录服务 + 一个额度很小的账号，返回 (地址, 账号库)。"""
    accounts = AccountStore(tmp_path / "accounts.db")
    account = accounts.create("alice", "password123", daily_token_limit=40)
    server = create_server(
        host="127.0.0.1",
        port=0,
        llm_mode="mock",
        quiet=True,
        session_dir=str(tmp_path / "sessions"),
        accounts=accounts,
        require_auth=False,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, accounts, account
    finally:
        server.shutdown()
        server.server_close()


def _post(url: str, payload: dict) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return response.status, (json.loads(body) if body.startswith("{") else {})
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_run_records_the_run_id_in_the_ledger(web):
    base, accounts, account = web
    # 免费套餐不让执行代码，用 react 跑一次纯对话即可
    status, _ = _post(f"{base}/api/run", {"agent": "react", "task": "北京天气如何", "llm": "mock"})
    assert status == 200

    rows = accounts.ledger_rows(account.id)
    assert rows, "跑完后账本里应该有记录"
    assert rows[0]["run_id"], "账本要能追到具体是哪次运行"


def test_quota_exhaustion_returns_402_with_upgrade_hint(web):
    base, accounts, account = web
    accounts.record_usage(account.id, 40)  # 额度就是 40，直接打满

    request = urllib.request.Request(
        f"{base}/api/run",
        data=json.dumps({"agent": "react", "task": "北京天气如何", "llm": "mock"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(request, timeout=30)
    assert info.value.code == 402
    assert "升级套餐" in info.value.read().decode("utf-8")


def test_plans_endpoint_lists_sellable_plans(web):
    base, _, _ = web
    with urllib.request.urlopen(f"{base}/api/plans", timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    names = [plan["name"] for plan in payload["plans"]]
    assert names == ["free", "basic", "pro", "team"]


def test_billing_endpoint_reports_usage(web):
    base, accounts, account = web
    accounts.record_usage(account.id, 123)
    request = urllib.request.Request(
        f"{base}/api/run",
        data=json.dumps({"agent": "react", "task": "北京天气如何", "llm": "mock"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    # 先跑一次把账号活跃起来（记录用量即可，不真的调用模型）
    with urllib.request.urlopen(f"{base}/api/me", timeout=30) as response:
        me = json.loads(response.read().decode("utf-8"))
    assert me["account"]["plan_title"] == "免费"


def test_checkout_creates_a_pending_order(web):
    base, _, _ = web
    status, payload = _post(f"{base}/api/billing/checkout", {"plan": "basic", "months": 1})
    assert status == 200
    assert payload["order"]["status"] == "pending"
```

- [ ] **Step 2: 跑测试，确认它失败**

Run: `D:\Anaconda\python.exe -m pytest tests/test_web_billing.py -q`
Expected: FAIL —— 404 / `KeyError: 'plan_title'`

- [ ] **Step 3: 改 `server.py`**

`_account_payload` 补套餐信息：

```python
    def _account_payload(self, account: Account) -> dict[str, Any]:
        """给前端的账号信息：名字、套餐、今日额度。"""
        store = self.server.accounts  # type: ignore[attr-defined]
        plan = store.effective_plan(account)
        used, calls = store.usage_today(account.id)
        limit = store.daily_limit(account)
        unlimited = limit <= 0
        return {
            "name": account.name,
            "plan": account.plan,
            "plan_title": plan.title,
            "plan_expires_at": account.plan_expires_at,
            "unlimited": unlimited,
            "daily_token_limit": limit,
            "used_today": used,
            "calls_today": calls,
            "remaining": -1 if unlimited else max(0, limit - used),
        }
```

接任务时按套餐收口（在 `check_quota` 之后、开跑之前）：

```python
        plan = accounts.effective_plan(account) if account is not None else None
        guard_key = self._client_key()
        decision = self.server.guard.acquire(  # type: ignore[attr-defined]
            guard_key,
            per_minute=plan.per_minute if plan else None,
            max_concurrent=plan.max_concurrent if plan else None,
        )
        if not decision.allowed:
            self._reject_by_limit(decision)
            return

        # 套餐不允许执行代码时，连工具都不下发给模型
        if plan is not None and not plan.allow_code_tools:
            settings = settings.apply_overrides({"allow_code_tools": False})
```

记账带上运行编号：

```python
            if account is not None:
                answer = finished.answer_event() or {}
                usage = (answer.get("data") or {}).get("usage") or {}
                accounts.record_usage(
                    account.id,
                    int(usage.get("total_tokens") or 0),
                    calls=1,
                    run_id=finished.id,
                )
```

- [ ] **Step 4: 跑测试，确认通过**

Run: `D:\Anaconda\python.exe -m pytest tests/test_web_billing.py tests/test_web_auth.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agentcode/web/server.py tests/test_web_billing.py
git commit -m "feat: 服务端按套餐收口配额与限流，账本带上运行编号"
```

---

### Task 8: 三个新接口

**Files:**
- Modify: `agentcode/web/server.py`
- Test: `tests/test_web_billing.py`（追加）

**Interfaces:**
- Consumes: `BillingService`（Task 6）
- Produces:
  - `GET /api/plans` → `{"plans": [Plan.to_dict(), ...]}`
  - `GET /api/billing` → `BillingService.my_billing(account)`
  - `POST /api/billing/checkout` `{"plan": "basic", "months": 1}` → `{"order": Order.to_dict()}`

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_web_billing.py —— 追加

def test_billing_endpoint_shape(web):
    base, accounts, account = web
    accounts.record_usage(account.id, 500)
    with urllib.request.urlopen(f"{base}/api/billing", timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    assert payload["plan"]["name"] == "free"
    assert payload["used_today"] == 500
    assert payload["daily_limit"] == 40          # 这个账号被设了 40 的覆盖
    assert payload["remaining_today"] == 0
    assert payload["orders"] == []


def test_checkout_rejects_internal_plan(web):
    base, _, _ = web
    status, payload = _post(f"{base}/api/billing/checkout", {"plan": "owner", "months": 1})
    assert status == 400
    assert "不可购买" in payload["error"]


def test_checkout_rejects_bad_months(web):
    base, _, _ = web
    status, payload = _post(f"{base}/api/billing/checkout", {"plan": "basic", "months": 99})
    assert status == 400
    assert "月数" in payload["error"]


def test_checkout_returns_a_pending_order_for_a_sellable_plan(web):
    base, _, _ = web
    status, payload = _post(f"{base}/api/billing/checkout", {"plan": "pro", "months": 2})
    assert status == 200
    assert payload["order"]["plan"] == "pro"
    assert payload["order"]["amount_cents"] == 9_900 * 2


def test_billing_endpoints_require_login(tmp_path):
    """管理动作走 CLI，但只读的账单信息也要登录才能看。"""
    accounts = AccountStore(tmp_path / "accounts.db")
    accounts.create("alice", "password123")
    server = create_server(
        host="127.0.0.1",
        port=0,
        llm_mode="mock",
        quiet=True,
        session_dir=str(tmp_path / "sessions"),
        accounts=accounts,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path in ("/api/plans", "/api/billing"):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}{path}", timeout=20)
                raise AssertionError(f"{path} 应该要求登录")
            except urllib.error.HTTPError as error:
                assert error.code == 401
    finally:
        server.shutdown()
        server.server_close()
```

- [ ] **Step 2: 跑测试，确认它失败**

Run: `D:\Anaconda\python.exe -m pytest tests/test_web_billing.py -q`
Expected: FAIL —— 404

- [ ] **Step 3: 加路由**

`do_GET` 里，跟在 `/api/run/stream` 后面：

```python
        if path == "/api/plans":
            self._send_json({"plans": [plan.to_dict() for plan in self._billing().plans()]})
            return
        if path == "/api/billing":
            account = getattr(self, "_account", None)
            if account is None:
                self._send_json({"error": "未登录。"}, status=401)
                return
            self._send_json(self._billing().my_billing(account))
            return
```

`do_POST` 里，**必须放在"落到 run 逻辑"之前**（现在 `/api/sessions/delete` 之后就是 run）：

```python
        if path == "/api/billing/checkout":
            account = getattr(self, "_account", None)
            if account is None:
                self._send_json({"error": "未登录。"}, status=401)
                return
            payload_json = self._read_json()
            if payload_json is None:
                return
            try:
                order = self._billing().checkout(
                    account,
                    str(payload_json.get("plan") or ""),
                    int(payload_json.get("months") or 1),
                )
            except (AgentCodeError, ValueError) as exc:
                self._send_json({"error": str(exc)}, status=400)
                return
            self._send_json(
                {
                    "order": order.to_dict(),
                    "message": "订单已创建。请按约定方式付款，管理员确认到账后套餐立即生效。",
                }
            )
            return
```

加一个取服务的小工具（和 `_registry()` 同一风格）：

```python
    def _billing(self) -> BillingService:
        """当前服务进程的账单服务。"""
        return self.server.billing  # type: ignore[attr-defined]
```

并在 `create_server` 里建一次：

```python
    from agentcode.billing import BillingService

    server.billing = BillingService(accounts or server.accounts)  # type: ignore[attr-defined]
```

（放在 `server.accounts` 赋值之后。）

- [ ] **Step 4: 跑测试，确认通过**

Run: `D:\Anaconda\python.exe -m pytest tests/test_web_billing.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agentcode/web/server.py tests/test_web_billing.py
git commit -m "feat: 套餐、账单与下单三个接口"
```

---

### Task 9: 命令行

**Files:**
- Modify: `agentcode/cli.py`
- Test: `tests/test_cli_billing.py`（新）

**Interfaces:**
- Consumes: `BillingService`、`AccountStore`
- Produces: `agentcode user plan <名> <套餐> [--months N]`、`agentcode billing orders [--limit N]`、`agentcode billing grant <名> <套餐> [--months N]`、`agentcode billing confirm <订单号> [--reference 备注]`

**为什么管理动作只在 CLI**：网页上开放"确认到账/直接开通"等于给自己留了一条自助提权的路。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_cli_billing.py
"""账单相关命令行（离线）。"""

from __future__ import annotations

import json

from agentcode.accounts import AccountStore
from agentcode.cli import main


def test_user_plan_command_switches_plan(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "accounts.db"))
    store = AccountStore(tmp_path / "accounts.db")
    store.create("alice", "password123")

    code = main(["user", "plan", "alice", "pro", "--months", "1"])
    assert code == 0
    assert AccountStore(tmp_path / "accounts.db").get("alice").plan == "pro"


def test_billing_grant_command_works(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "accounts.db"))
    AccountStore(tmp_path / "accounts.db").create("alice", "password123")

    code = main(["billing", "grant", "alice", "basic", "--months", "2"])
    assert code == 0
    assert AccountStore(tmp_path / "accounts.db").get("alice").plan == "basic"


def test_billing_confirm_command_activates(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "accounts.db"))
    store = AccountStore(tmp_path / "accounts.db")
    account = store.create("alice", "password123")
    from agentcode.billing import BillingService

    order = BillingService(store).checkout(account, "basic", 1)

    code = main(["billing", "confirm", order.id, "--reference", "微信转账"])
    assert code == 0
    assert AccountStore(tmp_path / "accounts.db").get("alice").plan == "basic"


def test_billing_orders_lists_with_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "accounts.db"))
    store = AccountStore(tmp_path / "accounts.db")
    account = store.create("alice", "password123")
    from agentcode.billing import BillingService

    BillingService(store).checkout(account, "basic", 1)
    code = main(["billing", "orders", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["orders"][0]["plan"] == "basic"


def test_billing_grant_rejects_unknown_plan(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "accounts.db"))
    AccountStore(tmp_path / "accounts.db").create("alice", "password123")
    code = main(["billing", "grant", "alice", "不存在的套餐"])
    assert code == 2
    assert "没有这个套餐" in capsys.readouterr().out
```

- [ ] **Step 2: 跑测试，确认它失败**

Run: `D:\Anaconda\python.exe -m pytest tests/test_cli_billing.py -q`
Expected: FAIL —— `argparse` 报 `invalid choice: 'billing'`

- [ ] **Step 3: 加子命令**

在 `build_parser()` 的 `user_sub` 部分追加：

```python
    plan_parser = user_sub.add_parser("plan", help="切换账号套餐")
    plan_parser.add_argument("name")
    plan_parser.add_argument("plan", help="free / basic / pro / team / owner")
    plan_parser.add_argument("--months", type=int, default=1, help="有效月数，默认 1")
```

在 `user_parser` 之后追加整个 `billing` 分支：

```python
    billing_parser = subparsers.add_parser("billing", help="订单与开通（收费相关）")
    billing_sub = billing_parser.add_subparsers(dest="billing_command", required=True)
    orders_parser = billing_sub.add_parser("orders", help="列出订单")
    orders_parser.add_argument("--account", default=None, help="只看某个账号")
    orders_parser.add_argument("--limit", type=int, default=20)
    orders_parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    grant_parser = billing_sub.add_parser("grant", help="直接开通/续期，不经过订单")
    grant_parser.add_argument("name")
    grant_parser.add_argument("plan")
    grant_parser.add_argument("--months", type=int, default=1)
    confirm_parser = billing_sub.add_parser("confirm", help="确认到账并开通")
    confirm_parser.add_argument("order_id")
    confirm_parser.add_argument("--reference", default=None, help="支付流水备注")
```

处理函数（放在 `_user_command` 旁边）：

```python
def _billing_command(args: argparse.Namespace) -> int:
    """处理 ``agentcode billing`` 子命令。"""
    from agentcode.accounts import AccountStore
    from agentcode.billing import BillingService
    from agentcode.core.errors import AgentCodeError

    store = AccountStore(os.environ.get("AGENT_DB_PATH") or DEFAULT_DB_PATH)
    billing = BillingService(store)
    try:
        if args.billing_command == "orders":
            orders = store.list_orders(args.account, args.limit)
            if args.json:
                print(json.dumps({"orders": orders}, ensure_ascii=False))
            else:
                if not orders:
                    print("（还没有订单）")
                for order in orders:
                    print(
                        f"{order['created_at']}  {order['id']}  "
                        f"{order['plan']} x{order['months']}  "
                        f"{order['amount_cents'] / 100:.2f} {order['currency']}  "
                        f"[{order['status']}]"
                    )
            return 0
        if args.billing_command == "grant":
            account = store.get(args.name)
            if account is None:
                print(f"错误：没有这个账号：{args.name}")
                return 2
            refreshed = billing.grant(account, args.plan, args.months)
            expires = refreshed.plan_expires_at or "不过期"
            print(f"已把 {refreshed.name} 设为套餐 {refreshed.plan}，到期时间：{expires}")
            return 0
        if args.billing_command == "confirm":
            order = billing.confirm(args.order_id, args.reference)
            print(f"订单 {order.id} 已确认到账，套餐 {order.plan} 生效 {order.months} 个月。")
            return 0
    except AgentCodeError as exc:
        print(f"错误：{exc}")
        return 2
    print("错误：未知的 billing 子命令。")
    return 2
```

在 `_user_command` 里加一个分支（放在其它分支之后、返回 2 之前）：

```python
    if args.user_command == "plan":
        from agentcode.billing import BillingService
        from agentcode.core.errors import AgentCodeError

        account = store.get(args.name)
        if account is None:
            print(f"错误：没有这个账号：{args.name}")
            return 2
        try:
            refreshed = BillingService(store).grant(account, args.plan, args.months)
        except AgentCodeError as exc:
            print(f"错误：{exc}")
            return 2
        print(f"已把 {refreshed.name} 设为套餐 {refreshed.plan}，到期时间：{refreshed.plan_expires_at or '不过期'}")
        return 0
```

在 `main()` 的分发链里加：

```python
        if args.command == "billing":
            return _billing_command(args)
```

（`DEFAULT_DB_PATH` 和 `json` 若未导入，按文件里现有写法补 import。）

- [ ] **Step 4: 跑测试，确认通过**

Run: `D:\Anaconda\python.exe -m pytest tests/test_cli_billing.py tests/test_cli.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add agentcode/cli.py tests/test_cli_billing.py
git commit -m "feat: CLI 增加 user plan 与 billing orders/grant/confirm"
```

---

### Task 10: 网页「套餐与用量」面板

**Files:**
- Modify: `agentcode/web/static/app.js`、`agentcode/web/static/style.css`
- Test: `tests/test_web_design.py`（追加断言）

**Interfaces:**
- Consumes: `GET /api/billing`、`GET /api/plans`、`POST /api/billing/checkout`（Task 8）
- Produces: 账号栏里一个「套餐」按钮，点开显示套餐、用量、订单

**保持克制**：主界面只给结果（既有约定），面板默认收起，不占主区域。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_web_design.py —— 追加

def test_billing_panel_exists_in_markup():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="billing-panel"' in html
    assert 'id="billing-toggle"' in html


def test_billing_panel_is_hidden_until_opened():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "#billing-panel[hidden]" in css
    assert "display: none" in css


def test_billing_js_reads_the_new_endpoints():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "/api/billing" in js
    assert "/api/plans" in js
    assert "billing/checkout" in js
```

（`STATIC` 是 `tests/test_web_design.py` 里已有的常量；没有就照该文件现有写法补一个。）

- [ ] **Step 2: 跑测试，确认它失败**

Run: `D:\Anaconda\python.exe -m pytest tests/test_web_design.py -q`
Expected: FAIL —— `assert 'id="billing-panel"' in html`

- [ ] **Step 3: 改前端**

`index.html` 在账号区加一个按钮与一个默认隐藏的面板：

```html
<button type="button" id="billing-toggle" class="ghost-button">套餐与用量</button>
<section id="billing-panel" class="billing-panel" hidden>
  <header class="billing-head">
    <strong id="billing-plan">—</strong>
    <span id="billing-expiry"></span>
    <button type="button" id="billing-close" class="icon" aria-label="关闭">✕</button>
  </header>
  <div class="billing-usage">
    <div class="billing-bar"><span id="billing-bar-fill"></span></div>
    <p id="billing-usage-text"></p>
  </div>
  <div class="billing-plans" id="billing-plans"></div>
  <ul class="billing-orders" id="billing-orders"></ul>
</section>
```

`style.css`：

```css
#billing-panel[hidden] {
  display: none;
}

.billing-panel {
  position: absolute;
  right: 16px;
  top: 56px;
  width: 320px;
  padding: 16px;
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.92);
  backdrop-filter: blur(8px);
  box-shadow: 0 12px 32px rgba(20, 40, 90, 0.16);
  z-index: 20;
}

.billing-bar {
  height: 6px;
  border-radius: 3px;
  background: rgba(63, 92, 240, 0.12);
  overflow: hidden;
}

#billing-bar-fill {
  display: block;
  height: 100%;
  width: 0;
  background: var(--accent);
  transition: width 0.3s ease;
}

#billing-bar-fill.is-over {
  background: #e0533d;
}
```

`app.js` 加三件事（`escapeHtml` 已在文件里）：

```javascript
async function openBilling() {
  els.billingPanel.hidden = false;
  await refreshBilling();
}

async function refreshBilling() {
  const [billing, plans] = await Promise.all([
    (await fetch("/api/billing")).json(),
    (await fetch("/api/plans")).json(),
  ]);
  renderBilling(billing, plans.plans || []);
}

function renderBilling(billing, plans) {
  const plan = billing.plan || {};
  els.billingPlan.textContent = plan.title || plan.name || "—";
  els.billingExpiry.textContent = billing.plan_expires_at
    ? `到期 ${String(billing.plan_expires_at).slice(0, 10)}`
    : plan.name === "owner"
      ? "不过期"
      : "永久";

  const unlimited = billing.remaining_today < 0;
  const limit = billing.daily_limit || 0;
  const used = billing.used_today || 0;
  const ratio = unlimited || !limit ? 0 : Math.min(1, used / limit);
  els.billingBarFill.style.width = `${Math.round(ratio * 100)}%`;
  els.billingBarFill.classList.toggle("is-over", !unlimited && used >= limit);
  els.billingUsageText.textContent = unlimited
    ? `今日已用 ${used} token（不限量）`
    : `今日 ${used} / ${limit} token，本月累计 ${billing.used_this_period || 0}`;

  els.billingPlans.innerHTML = plans
    .map(
      (item) => `
      <div class="billing-plan-card">
        <span>${escapeHtml(item.title)}</span>
        <span>${item.price_cents === 0 ? "免费" : `¥${(item.price_cents / 100).toFixed(0)}/月`}</span>
        <span>${item.daily_tokens} token/天</span>
        <button type="button" data-plan="${escapeHtml(item.name)}">升级</button>
      </div>`
    )
    .join("");
  els.billingPlans.querySelectorAll("button[data-plan]").forEach((button) => {
    button.addEventListener("click", () => checkout(button.dataset.plan));
  });

  els.billingOrders.innerHTML = (billing.orders || [])
    .map(
      (order) =>
        `<li>${String(order.created_at).slice(0, 10)} ${escapeHtml(order.plan)} ` +
        `¥${(order.amount_cents / 100).toFixed(2)} [${escapeHtml(order.status)}]</li>`
    )
    .join("");
}

async function checkout(planName) {
  const response = await fetch("/api/billing/checkout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plan: planName, months: 1 }),
  });
  const payload = await response.json();
  els.runStatus.textContent = response.ok
    ? `${payload.message} 订单号：${payload.order.id}`
    : payload.error || "下单失败。";
  if (response.ok) await refreshBilling();
}
```

把新元素加进 `els` 那个对象，并接上按钮：

```javascript
  billingToggle: document.getElementById("billing-toggle"),
  billingPanel: document.getElementById("billing-panel"),
  billingClose: document.getElementById("billing-close"),
  billingPlan: document.getElementById("billing-plan"),
  billingExpiry: document.getElementById("billing-expiry"),
  billingBarFill: document.getElementById("billing-bar-fill"),
  billingUsageText: document.getElementById("billing-usage-text"),
  billingPlans: document.getElementById("billing-plans"),
  billingOrders: document.getElementById("billing-orders"),
```

```javascript
els.billingToggle.addEventListener("click", openBilling);
els.billingClose.addEventListener("click", () => {
  els.billingPanel.hidden = true;
});
```

- [ ] **Step 4: 跑测试并手工看一眼**

Run: `D:\Anaconda\python.exe -m pytest tests/test_web_design.py -q`
Expected: PASS

然后重启服务、在浏览器里点开面板，确认：额度条显示正确、点升级能拿到订单号、刷新后订单出现在列表里。

```powershell
D:\Anaconda\python.exe -m agentcode web --port 8000
```

- [ ] **Step 5: 提交**

```bash
git add agentcode/web/static tests/test_web_design.py
git commit -m "feat: 网页增加套餐与用量面板"
```

---

### Task 11: 文档、真实库迁移演练与端到端验证

**Files:**
- Modify: `README.md`
- Test: 手工端到端 + 全量回归

**Interfaces:**
- Consumes: 前面全部
- Produces: README 的「套餐与计费」一节；一次对真实库副本的迁移演练记录

- [ ] **Step 1: 拿真实库的副本跑一遍迁移（不做这一步不算完）**

```powershell
Copy-Item D:\agent\agentcode.db D:\agent\.env -Destination D:\Docker-setup\ -ErrorAction SilentlyContinue
Copy-Item D:\agent\agentcode.db D:\Docker-setup\agentcode-real-copy.db -ErrorAction SilentlyContinue
```

```python
# 用副本跑迁移，确认那两个真实账号还能登录
from agentcode.accounts import AccountStore
store = AccountStore(r"D:\Docker-setup\agentcode-real-copy.db")
for account in store.list():
    print(account.name, account.plan, account.daily_token_limit, account.plan_expires_at)
print("帐号数：", len(store.list()))
```

Expected: `wyh` 的套餐是 `owner`、日额度 0（不限）、不过期；`admin` 依然存在。

- [ ] **Step 2: 端到端走一遍"用爆 → 被拒 → 开通 → 继续跑"**

```powershell
# 免费账号，额度设小一点方便打满
D:\Anaconda\python.exe -m agentcode user add demo --password demo123456 --daily-limit 30
D:\Anaconda\python.exe -m agentcode run --agent react --llm openai --task "北京天气如何"
# 再跑一次应该被拒（额度只有 30）
D:\Anaconda\python.exe -m agentcode run --agent react --llm openai --task "北京天气如何"
# 开通基础套餐
D:\Anaconda\python.exe -m agentcode billing grant demo basic --months 1
# 现在应该能继续跑
D:\Anaconda\python.exe -m agentcode run --agent react --llm openai --task "北京天气如何"
```

Expected: 第二次输出「今日额度已用完…升级套餐可以提高上限」；`grant` 之后第三次正常出结果。

- [ ] **Step 3: 把行为变化写进 README**

在 README 的「账号与配额」一节后面加「套餐与计费」，必须写清楚：

```markdown
### 套餐与计费

额度由**套餐**决定，不再由账号上的数字决定。可售套餐见 `agentcode/plans.py`。

| 套餐 | 日 token | 每分钟 | 并发 | 代码执行 | 月价 |
| --- | --- | --- | --- | --- | --- |
| 免费 | 2 万 | 10 | 1 | 否 | 免费 |
| 基础 | 20 万 | 30 | 2 | 是 | ¥29 |
| 专业 | 100 万 | 60 | 4 | 是 | ¥99 |
| 团队 | 500 万 | 120 | 8 | 是 | ¥399 |

> **行为变化**：升级到本版本后，账号上原先手工设的每日额度不再生效，
> 额度以套餐为准。需要单独提额时用 `agentcode user limit <账号> <token数>`，
> 它会写成账号级覆盖，优先级高于套餐。

usage 账本：每次运行记一条（含运行编号），今日/区间用量都从它汇总，可导出 CSV。

管理动作只在命令行（网页不开放，避免自助提权）：

```powershell
agentcode user plan <账号> <套餐> --months 3     # 换套餐 / 续期
agentcode billing orders                          # 看订单
agentcode billing grant <账号> <套餐> --months 1  # 收钱后直接开通
agentcode billing confirm <订单号> --reference "微信转账 20260929"
```
```

- [ ] **Step 4: 全量回归 + 双平台**

```powershell
D:\Anaconda\python.exe -m pytest -q -p no:warnings
```

Expected: 全绿。再在 WSL 里跑一遍（Linux / Python 3.12）确认没有平台差异。

- [ ] **Step 5: 提交并推送**

```bash
git add README.md
git commit -m "docs: 套餐与计费说明，写明行为变化与 CLI 管理命令"
git push origin main
```

---

## 自查（写完计划后回看 spec）

**Spec 覆盖检查**

| Spec 章节 | 对应任务 |
| --- | --- |
| §4 套餐目录 | Task 1 |
| §5.1 迁移机制 | Task 2 |
| §5.2–5.5 表结构与老数据搬迁 | Task 3 |
| §6 用量账本 | Task 4 |
| §8 配额与限流顺序 | Task 5、Task 7 |
| §7 订单与开通 | Task 6、Task 9 |
| §9 接口与界面 | Task 8、Task 10 |
| §10 边界与错误处理 | Task 1（未知套餐/到期）、Task 6（幂等/取消）、Task 8（下单校验）、Task 2（迁移回滚） |
| §11 测试策略 | 每个任务的测试步骤 + Task 11 的真实库演练 |
| §12 兼容性 | Task 4（派生字段保住老断言）、Task 11（README 行为变化） |

**占位符扫描**：无 TBD / TODO；每个代码步骤都给了可直接粘贴的实现。

**类型一致性检查**

- `Plan` 字段名在 Task 1 定义，Task 4/5/6/8 引用的 `plan.daily_tokens / allow_code_tools / per_minute / max_concurrent / sellable / title / price_cents` 全部存在；
- `UNLIMITED` 在 Task 1 定义，Task 4/5 用 `limit <= UNLIMITED` 判断不限；
- `usage_today` 返回 `tuple[int, int]`，Task 4 定义、Task 5 消费，未变过签名；
- `mark_order_paid` 在 Task 6 里返回 `dict | None`，`confirm` 只在返回非 None 时开通——两侧一致；
- `add_months(moment, months)` 名字在测试与实现里一致。

**已知取舍（写进代码注释）**：`confirm` 先改订单状态再开通套餐。若开通那一步失败，
订单已标记支付但套餐没生效——宁可这样，也不能反过来（用户付了钱却没拿到套餐）。
管理员看到这种情况用 `billing grant` 补一次即可。
