"""账号、用量账本与订单：收费产品的地基。

用 SQLite（标准库 sqlite3，零依赖、事务可靠）保存三样东西：

1. ``accounts``：账号、密码哈希、套餐、到期时间、额度覆盖、启用状态；
2. ``usage_ledger``：用量账本，**一行 = 一次运行**（含运行编号）；
   今日用量、区间用量、账单、导出，全部从它算；
3. ``orders``：订单（套餐、金额、状态、支付渠道）。

表结构由 :mod:`agentcode.storage.migrations` 拥有并负责升级，这里不再自己建表——
原来那套 ``CREATE TABLE IF NOT EXISTS`` 加不了新列，库里有真实账号时会原地爆掉。

密码用 PBKDF2-SHA256 加盐哈希，不保存明文。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from agentcode.core.errors import AgentCodeError
from agentcode.plans import UNLIMITED, Plan, effective_plan, get_plan, is_expired
from agentcode.storage.migrations import run_migrations

DEFAULT_DB_PATH = "traces/agentcode.db"
DEFAULT_DAILY_TOKEN_LIMIT = 50_000
PBKDF2_ITERATIONS = 200_000
MIN_PASSWORD_LENGTH = 6


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """生成 ``pbkdf2_sha256$迭代次数$盐$哈希`` 形式的密码哈希。"""
    if len(str(password or "")) < MIN_PASSWORD_LENGTH:
        raise AgentCodeError(f"密码至少 {MIN_PASSWORD_LENGTH} 位。")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码是否匹配存储的哈希（用固定时间比较，避免时序侧信道）。"""
    try:
        algorithm, iterations, salt, expected = str(stored).split("$")
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", str(password or "").encode("utf-8"), salt.encode("utf-8"), int(iterations)
    )
    return hmac.compare_digest(digest.hex(), expected)


def _today() -> str:
    """用量按 UTC 日期归档。"""
    return datetime.now(timezone.utc).date().isoformat()


@dataclass
class Account:
    """一个用户账号。"""

    id: str
    name: str
    password_hash: str
    plan: str = "free"
    #: **派生值**：有 limit_override 用它，否则用套餐的日额度（0 = 不限）
    daily_token_limit: int = DEFAULT_DAILY_TOKEN_LIMIT
    is_active: bool = True
    created_at: str = ""
    plan_started_at: str | None = None
    plan_expires_at: str | None = None
    #: 账号级额度覆盖；为空则跟随套餐
    limit_override: int | None = None


class AccountStore:
    """账号与用量的存储（线程安全）。"""

    def __init__(self, path: str | Path = DEFAULT_DB_PATH) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        """建表与升级结构。

        真正的 schema 定义在 :mod:`agentcode.storage.migrations` 里；
        打开老库时这里会自动把它升到最新版本。
        """
        with self._lock:
            run_migrations(self._conn)

    # ------------------------------------------------------------------ 账号

    def create(
        self,
        name: str,
        password: str,
        *,
        plan: str = "free",
        daily_token_limit: int | None = None,
    ) -> Account:
        """新建账号；名称重复时报错。"""
        cleaned = " ".join(str(name or "").split())
        if not cleaned:
            raise AgentCodeError("账号名不能为空。")
        if daily_token_limit is not None and daily_token_limit <= 0:
            raise AgentCodeError("每日 token 上限必须大于 0。")

        resolved = get_plan(plan)
        override = int(daily_token_limit) if daily_token_limit is not None else None
        account = Account(
            id=uuid.uuid4().hex[:12],
            name=cleaned,
            password_hash=hash_password(password),
            plan=resolved.name,
            # 显式给了额度就当作覆盖，否则跟随套餐
            daily_token_limit=override if override is not None else resolved.daily_tokens,
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
        except sqlite3.IntegrityError as exc:
            raise AgentCodeError(f"账号「{cleaned}」已存在。") from exc
        return account

    @staticmethod
    def _row_to_account(row: sqlite3.Row) -> Account:
        override = row["limit_override"]
        plan = effective_plan(row["plan"], row["plan_expires_at"])
        return Account(
            id=row["id"],
            name=row["name"],
            password_hash=row["password_hash"],
            plan=row["plan"],
            daily_token_limit=int(override) if override is not None else plan.daily_tokens,
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            plan_started_at=row["plan_started_at"],
            plan_expires_at=row["plan_expires_at"],
            limit_override=None if override is None else int(override),
        )

    def get(self, name: str) -> Account | None:
        """按账号名取账号。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM accounts WHERE name = ?", (str(name or "").strip(),)
            ).fetchone()
        return self._row_to_account(row) if row else None

    def get_by_id(self, account_id: str) -> Account | None:
        """按 id 取账号。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM accounts WHERE id = ?", (str(account_id or ""),)
            ).fetchone()
        return self._row_to_account(row) if row else None

    def list(self) -> list[Account]:
        """列出全部账号（按创建时间）。"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM accounts ORDER BY created_at").fetchall()
        return [self._row_to_account(row) for row in rows]

    def verify(self, name: str, password: str) -> Account | None:
        """校验账号密码；账号不存在、密码错、被停用都返回 None。"""
        account = self.get(name)
        if account is None or not account.is_active:
            return None
        if not verify_password(password, account.password_hash):
            return None
        return account

    def set_active(self, name: str, active: bool) -> bool:
        """启用或停用账号。"""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE accounts SET is_active = ? WHERE name = ?",
                (1 if active else 0, str(name or "").strip()),
            )
        return cursor.rowcount > 0

    def set_password(self, name: str, password: str) -> bool:
        """改密码。"""
        hashed = hash_password(password)
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE accounts SET password_hash = ? WHERE name = ?",
                (hashed, str(name or "").strip()),
            )
        return cursor.rowcount > 0

    def delete_account(self, account_id: str) -> bool:
        """从账号库里彻底删掉一个账号，连同它的用量账本与订单。

        **磁盘上的会话与代码目录不在这里删**——那部分归
        :func:`agentcode.lifecycle.delete_account_data`。两件事要一起做才叫注销，
        所以调用方应该用 ``lifecycle`` 里的编排，而不是单独调这一个。
        """
        cleaned = str(account_id or "").strip()
        if not cleaned:
            return False
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM usage_ledger WHERE account_id = ?", (cleaned,))
            self._conn.execute("DELETE FROM usage WHERE account_id = ?", (cleaned,))
            self._conn.execute("DELETE FROM orders WHERE account_id = ?", (cleaned,))
            cursor = self._conn.execute("DELETE FROM accounts WHERE id = ?", (cleaned,))
        return cursor.rowcount > 0

    def set_limit(self, name: str, daily_token_limit: int) -> bool:
        """调整某个账号的每日额度。

        这会写成一个**账号级覆盖**，优先级高于套餐额度（客服补偿、临时提额用）。
        """
        if daily_token_limit <= 0:
            raise AgentCodeError("每日 token 上限必须大于 0。")
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE accounts SET limit_override = ?, daily_token_limit = ? WHERE name = ?",
                (daily_token_limit, daily_token_limit, str(name or "").strip()),
            )
        return cursor.rowcount > 0

    def apply_plan(
        self,
        name: str,
        plan: str,
        *,
        started_at: str,
        expires_at: str | None,
    ) -> bool:
        """把账号的套餐改成指定状态。

        只给 :mod:`agentcode.billing` 用——**别拿它当开通入口**，
        开通要走 billing 的订单/续期逻辑，否则会算错到期时间。
        """
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

    # ------------------------------------------------------------------ 用量

    def record_usage(
        self,
        account_id: str,
        tokens: int,
        calls: int = 1,
        *,
        run_id: str | None = None,
        note: str | None = None,
    ) -> None:
        """往账本里记一笔用量。

        账本是**唯一的用量事实来源**：老那张 ``usage`` 日计数器只读不写了
        （保留是为了回滚，不是为了查询）。
        """
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
        """闭区间的用量汇总（按 UTC 日期）。"""
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

    def reset_usage(self, account_id: str, day: str | None = None) -> None:
        """抹掉某天的账本记录（客服补偿或测试用）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM usage_ledger WHERE account_id = ? AND day = ?",
                (account_id, day or _today()),
            )

    # ------------------------------------------------------------------ 订单

    #: 订单表的列，顺序与 INSERT 一致
    ORDER_FIELDS = (
        "id",
        "account_id",
        "plan",
        "months",
        "amount_cents",
        "currency",
        "status",
        "provider",
        "provider_ref",
        "created_at",
        "paid_at",
        "note",
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
        record: dict[str, object] = {
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
        """按订单号取订单。"""
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
                    "SELECT * FROM orders ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM orders WHERE account_id = ?"
                    " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (account_id, max(1, int(limit))),
                ).fetchall()
        return [self._row_to_order(row) for row in rows]

    def mark_order_paid(
        self, order_id: str, *, provider_ref: str | None = None
    ) -> dict[str, object] | None:
        """把待支付订单标成已支付。

        只在 ``status='pending'`` 时才改；返回 ``None`` 表示**没有发生状态迁移**
        （已经支付过，或者被取消/退款了）——billing 靠这个保证订单只开通一次。
        """
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE orders SET status = 'paid', paid_at = ?,"
                " provider_ref = COALESCE(?, provider_ref)"
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

    def close(self) -> None:
        """关闭数据库连接。"""
        with self._lock:
            self._conn.close()


def default_account_store() -> AccountStore:
    """按环境变量或默认路径打开账号库。"""
    return AccountStore(os.environ.get("AGENT_DB_PATH") or DEFAULT_DB_PATH)
