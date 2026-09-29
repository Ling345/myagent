"""套餐的开通与续期。

支付渠道藏在 :class:`PaymentProvider` 后面：现在只有 ``ManualProvider``
（管理员确认到账），以后接微信 / 支付宝 / Stripe 只要实现同一个协议，
上层业务代码一行都不用改。

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
        """从存储层的行字典构造。"""
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
        """给接口用的可序列化形式。"""
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
        """账号当前有效的套餐。"""
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
        """改套餐或续期。

        续期规则：同一个套餐且还没过期，就从原来的到期日往后加；
        换套餐、或者已经过期了，就从今天重新起算。
        """
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
        if plan.sellable:
            expires_at: str | None = add_months(base, months).isoformat(timespec="seconds")
        else:
            expires_at = None
        self.store.apply_plan(account.name, plan.name, started_at=started, expires_at=expires_at)
