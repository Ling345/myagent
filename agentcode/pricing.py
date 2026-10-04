"""把 token 换算成钱。

"基础版 ¥29/月，每天 20 万 token"——这单生意是赚是亏，得能一眼算出来。
这是**运营决策**的依据，不是技术指标，所以单独一个模块。

三个容易算错的地方：

1. **输入和输出单价差好几倍**（输出通常贵 2–4 倍）。只记 token 总数，
   换算出来的是假账。所以账本从 v3 起分开记（见 storage/migrations.py）。
2. **老数据没有拆分**，不能假装它不存在，也不能瞎猜一个比例——
   这里会把它单独标出来。
3. **"用户天天跑满"才是要算的那个数**。拿平均用量算毛利，会得出一个
   让你安心的错误答案。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from agentcode.plans import Plan, sellable_plans

#: 一个月按多少天算
DAYS_PER_MONTH = 30


@dataclass(frozen=True)
class Price:
    """模型的单价。单位是**分 / 百万 token**，跟各家服务商的报价口径一致。"""

    input_cents_per_million: int = 0
    output_cents_per_million: int = 0

    @property
    def configured(self) -> bool:
        """有没有配过单价。没配就只能看 token，看不了钱。"""
        return self.input_cents_per_million > 0 or self.output_cents_per_million > 0

    def cost_cents(self, prompt_tokens: Any, completion_tokens: Any) -> int:
        """算钱，四舍五入到「分」。整数运算，不碰浮点。"""
        if not self.configured:
            return 0
        numerator = (
            int(prompt_tokens or 0) * int(self.input_cents_per_million)
            + int(completion_tokens or 0) * int(self.output_cents_per_million)
        )
        # 加半个百万分之一再整除 = 四舍五入
        return (numerator + 500_000) // 1_000_000


def price_from_settings(settings: Any) -> Price:
    """从配置里读单价。不配就是 0——宁可显示"未配置"，也不给一个假数字。"""
    return Price(
        input_cents_per_million=int(getattr(settings, "price_input_per_million", 0) or 0),
        output_cents_per_million=int(getattr(settings, "price_output_per_million", 0) or 0),
    )


def observed_output_ratio(prompt_tokens: Any = 0, completion_tokens: Any = 0) -> float | None:
    """从实际用量里算出"输出占多少"。

    也接受 ``AccountStore.usage_breakdown()`` 返回的字典。
    没有数据时返回 ``None``——不要编一个比例出来。
    """
    if isinstance(prompt_tokens, Mapping):
        breakdown = prompt_tokens
        prompt_tokens = breakdown.get("prompt_tokens", 0)
        completion_tokens = breakdown.get("completion_tokens", 0)
    prompt = int(prompt_tokens or 0)
    completion = int(completion_tokens or 0)
    total = prompt + completion
    if total <= 0:
        return None
    return completion / total


@dataclass(frozen=True)
class PlanMargin:
    """一个套餐的成本与毛利估算。金额单位都是「分」。"""

    plan: str
    title: str
    price_cents: int
    daily_tokens: int
    monthly_tokens: int
    #: 按观察到的输入/输出比例估算的成本；没有观测数据时是 None
    estimated_cost_cents: int | None
    estimated_margin_cents: int | None
    #: 最坏情况：所有 token 都按输出计价
    worst_case_cost_cents: int
    worst_case_margin_cents: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan,
            "title": self.title,
            "price_cents": self.price_cents,
            "daily_tokens": self.daily_tokens,
            "monthly_tokens": self.monthly_tokens,
            "estimated_cost_cents": self.estimated_cost_cents,
            "estimated_margin_cents": self.estimated_margin_cents,
            "worst_case_cost_cents": self.worst_case_cost_cents,
            "worst_case_margin_cents": self.worst_case_margin_cents,
        }


def _split_for(total: int, output_ratio: float) -> tuple[int, int]:
    """按比例把一个总数拆成输入/输出。"""
    completion = int(round(total * output_ratio))
    return total - completion, completion


def plan_margins(
    price: Price,
    *,
    output_ratio: float | None = None,
    days: int = DAYS_PER_MONTH,
) -> list[PlanMargin]:
    """每个可售套餐"用户天天跑满"时的成本与毛利。

    ``output_ratio`` 是从真实用量里观察到的输出占比；给 ``None`` 就只报
    最坏情况（全部按输出计价），不编一个比例出来。

    不限量套餐（内部的 owner）不在表里——给它算"毛利"没有意义。
    """
    rows: list[PlanMargin] = []
    for plan in sellable_plans():
        if plan.daily_tokens <= 0:
            continue
        monthly = plan.daily_tokens * max(1, days)
        worst_prompt, worst_completion = _split_for(monthly, 1.0)
        worst_cost = price.cost_cents(worst_prompt, worst_completion)

        estimated_cost: int | None = None
        estimated_margin: int | None = None
        if output_ratio is not None:
            prompt, completion = _split_for(monthly, min(1.0, max(0.0, output_ratio)))
            estimated_cost = price.cost_cents(prompt, completion)
            estimated_margin = plan.price_cents - estimated_cost

        rows.append(
            PlanMargin(
                plan=plan.name,
                title=plan.title,
                price_cents=plan.price_cents,
                daily_tokens=plan.daily_tokens,
                monthly_tokens=monthly,
                estimated_cost_cents=estimated_cost,
                estimated_margin_cents=estimated_margin,
                worst_case_cost_cents=worst_cost,
                worst_case_margin_cents=plan.price_cents - worst_cost,
            )
        )
    return rows
