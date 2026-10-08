"""API 令牌：让程序也能用这个服务。

网页靠登录 cookie 认人，那套东西不适合脚本：密码能登网页、能改密码、能注销，
写进脚本等于把家门钥匙交出去。令牌是另一把钥匙——**只能调 API**，
能随时吊销、能设过期、能一次给好几把（哪个泄露废哪个）。

三条硬规矩：

1. **只存哈希**。库里存 :func:`hash_token` 的结果；明文只在创建那一刻返回一次。
   这样备份泄露、误提交、拖库，都拿不到能用的凭据。
2. **认令牌时顺带查账号状态**。账号被停用或删掉，令牌立刻失效——
   不然"停用"就是个假动作。
3. **令牌只属于一个账号**。所有查询都带 ``account_id``，
   拿 A 的令牌碰不到 B 的任何东西。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from agentcode.accounts import Account, AccountStore

#: 令牌前缀：一眼看出"这是 AgentCode 的令牌"，也方便泄露时做扫描告警
TOKEN_PREFIX = "agk_"
#: 随机部分的字节数（43 个 URL 安全字符，够抗暴力猜）
TOKEN_BYTES = 32
#: 界面上显示多少位（前缀是明文，只用来让用户认"哪一把"）
DISPLAY_PREFIX_LENGTH = len(TOKEN_PREFIX) + 6


def generate_token() -> str:
    """生成一把新令牌的明文（只在创建时出现一次）。"""
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(plaintext: str | None) -> str:
    """令牌的存储形式：sha256 十六进制。

    令牌本身是 32 字节随机数，不存在"字典爆破"的问题，所以不加盐也能安全地
    按哈希做等值查询（认证必须能 O(1) 找到那一行）。
    """
    return hashlib.sha256(str(plaintext or "").encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    try:
        moment = datetime.fromisoformat(str(expires_at))
    except (TypeError, ValueError):
        return True  # 读不懂的过期时间当"已过期"，不能当"永不过期"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment <= _now()


class ApiTokenService:
    """令牌的生成、认证与吊销。"""

    def __init__(self, store: AccountStore) -> None:
        self.store = store

    def create(
        self,
        account: Account,
        *,
        name: str = "",
        expires_days: int | None = None,
        expires_at: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        """给某个账号建一把令牌，返回 ``(记录, 明文)``——明文只出现这一次。"""
        plaintext = generate_token()
        if expires_at is None and expires_days:
            expires_at = (_now() + timedelta(days=int(expires_days))).isoformat(timespec="seconds")
        record = self.store.create_api_token(
            account.id,
            name=name,
            prefix=plaintext[:DISPLAY_PREFIX_LENGTH],
            token_hash=hash_token(plaintext),
            expires_at=expires_at,
        )
        return record, plaintext

    def resolve(self, plaintext: Any) -> dict[str, Any] | None:
        """把请求里的令牌换成 ``{"account": ..., "token": ...}``；不认就返回 None。"""
        cleaned = str(plaintext or "").strip()
        if not cleaned.startswith(TOKEN_PREFIX) or len(cleaned) < 20:
            # 形状不对就别查库了；顺便挡掉有人拿会话 cookie 当令牌用
            return None
        record = self.store.find_api_token(hash_token(cleaned))
        if record is None or not record["is_active"]:
            return None
        if _is_expired(record["expires_at"]):  # type: ignore[arg-type]
            return None
        account = self.store.get_by_id(str(record["account_id"]))
        if account is None or not account.is_active:
            return None
        self.store.touch_api_token(str(record["id"]))
        return {"account": account, "token": record}

    def list(self, account_id: str) -> list[dict[str, Any]]:
        """某个账号的令牌（不带哈希），并标出是否已过期。"""
        items = self.store.api_tokens(account_id)
        for item in items:
            item["expired"] = _is_expired(item["expires_at"])  # type: ignore[arg-type]
        return items

    def revoke(self, account_id: str, token_id: str) -> bool:
        """吊销一把令牌（只能吊销自己账号的）。"""
        return self.store.revoke_api_token(account_id, token_id)
