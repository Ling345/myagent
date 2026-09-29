"""套餐目录：把「谁能用多少」定义成数据。

三条硬规则：

1. 未知套餐名一律回落免费套餐——坏字符串绝不能换来无限额度；
2. 到期时间解析不了就当已过期（宁可降级，也不放行）；
3. ``owner`` 是内部套餐，对外不可售。

这个模块不依赖项目里任何其它模块，方便别处随便引用而不担心循环导入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

FREE = "free"
OWNER = "owner"
#: 额度 / 频率 / 并发等于这个值表示「不限」
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
        """日额度是否不限。"""
        return self.daily_tokens <= UNLIMITED

    def to_dict(self) -> dict[str, object]:
        """给接口用的可序列化形式。"""
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


#: 套餐目录。改数字之前先改设计文档：
#: docs/superpowers/specs/2026-09-29-billing-and-usage-design.md
PLANS: dict[str, Plan] = {
    FREE: Plan(FREE, "免费", 20_000, 10, 1, False, 5, 0),
    "basic": Plan("basic", "基础", 200_000, 30, 2, True, 20, 2_900),
    "pro": Plan("pro", "专业", 1_000_000, 60, 4, True, 50, 9_900),
    "team": Plan("team", "团队", 5_000_000, 120, 8, True, 200, 39_900),
    OWNER: Plan(
        OWNER, "内部", UNLIMITED, UNLIMITED, UNLIMITED, True, UNLIMITED, 0, sellable=False
    ),
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
