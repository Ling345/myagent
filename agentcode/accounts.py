"""账号与用量：收费产品的地基。

用 SQLite（标准库 sqlite3，零依赖、事务可靠）保存两样东西：

1. ``accounts``：账号、密码哈希、套餐、每日 token 上限、启用状态；
2. ``usage``：按（账号, 日期）累计的 token 与调用次数，用来做配额与账单。

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
    daily_token_limit: int = DEFAULT_DAILY_TOKEN_LIMIT
    is_active: bool = True
    created_at: str = ""


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
        with self._lock, self._conn:
            self._conn.execute(
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
            self._conn.execute(
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

        account = Account(
            id=uuid.uuid4().hex[:12],
            name=cleaned,
            password_hash=hash_password(password),
            plan=plan,
            daily_token_limit=daily_token_limit or DEFAULT_DAILY_TOKEN_LIMIT,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        try:
            with self._lock, self._conn:
                self._conn.execute(
                    "INSERT INTO accounts (id, name, password_hash, plan, daily_token_limit,"
                    " is_active, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
                    (
                        account.id,
                        account.name,
                        account.password_hash,
                        account.plan,
                        account.daily_token_limit,
                        account.created_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AgentCodeError(f"账号「{cleaned}」已存在。") from exc
        return account

    @staticmethod
    def _row_to_account(row: sqlite3.Row) -> Account:
        return Account(
            id=row["id"],
            name=row["name"],
            password_hash=row["password_hash"],
            plan=row["plan"],
            daily_token_limit=int(row["daily_token_limit"]),
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
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

    def set_limit(self, name: str, daily_token_limit: int) -> bool:
        """调整某个账号的每日额度（卖套餐时用）。"""
        if daily_token_limit <= 0:
            raise AgentCodeError("每日 token 上限必须大于 0。")
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE accounts SET daily_token_limit = ? WHERE name = ?",
                (daily_token_limit, str(name or "").strip()),
            )
        return cursor.rowcount > 0

    # ------------------------------------------------------------------ 用量

    def record_usage(self, account_id: str, tokens: int, calls: int = 1) -> None:
        """累加用量。"""
        day = _today()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO usage (account_id, day, tokens, calls) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(account_id, day) DO UPDATE SET"
                " tokens = tokens + excluded.tokens, calls = calls + excluded.calls",
                (account_id, day, max(0, int(tokens)), max(0, int(calls))),
            )

    def usage_today(self, account_id: str) -> tuple[int, int]:
        """返回 (今天已用 token, 今天调用次数)。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT tokens, calls FROM usage WHERE account_id = ? AND day = ?",
                (account_id, _today()),
            ).fetchone()
        return (int(row["tokens"]), int(row["calls"])) if row else (0, 0)

    def usage_history(self, account_id: str, limit: int = 30) -> list[dict[str, object]]:
        """最近的每日用量，用于账单与报表。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT day, tokens, calls FROM usage WHERE account_id = ?"
                " ORDER BY day DESC LIMIT ?",
                (account_id, max(1, int(limit))),
            ).fetchall()
        return [{"day": row["day"], "tokens": row["tokens"], "calls": row["calls"]} for row in rows]

    def remaining_tokens(self, account: Account) -> int:
        """今天还剩多少 token 额度。"""
        used, _ = self.usage_today(account.id)
        return max(0, account.daily_token_limit - used)

    def check_quota(self, account: Account) -> tuple[bool, str]:
        """判断是否还能继续调用；不允许时给出中文原因。"""
        if not account.is_active:
            return False, "账号已被停用，请联系管理员。"
        used, calls = self.usage_today(account.id)
        if used >= account.daily_token_limit:
            return (
                False,
                f"今日额度已用完（{used}/{account.daily_token_limit} token，{calls} 次调用），"
                "明天会自动重置；需要更多额度请升级套餐。",
            )
        return True, ""

    def reset_usage(self, account_id: str, day: str | None = None) -> None:
        """清空某天的用量（客服补偿或测试用）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM usage WHERE account_id = ? AND day = ?",
                (account_id, day or _today()),
            )

    def close(self) -> None:
        """关闭数据库连接。"""
        with self._lock:
            self._conn.close()


def default_account_store() -> AccountStore:
    """按环境变量或默认路径打开账号库。"""
    return AccountStore(os.environ.get("AGENT_DB_PATH") or DEFAULT_DB_PATH)
