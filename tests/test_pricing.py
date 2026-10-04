"""把 token 换算成钱（离线）。

"基础版 ¥29/月，每天 20 万 token"——这单生意是赚是亏，得能一眼算出来。
这是**运营决策**的依据，不是技术指标。
"""

from __future__ import annotations

import pytest

from agentcode.plans import PLANS
from agentcode.pricing import (
    Price,
    observed_output_ratio,
    plan_margins,
    price_from_settings,
)

#: 一组好算的单价（分/百万 token）：输入 200 分（¥2），输出 800 分（¥8）
SAMPLE = Price(input_cents_per_million=200, output_cents_per_million=800)


# ---------------------------------------------------------------- 单价


def test_unconfigured_price_costs_nothing():
    assert Price().configured is False
    assert Price().cost_cents(1_000_000, 1_000_000) == 0


def test_configured_price_is_detected():
    assert SAMPLE.configured is True


def test_cost_of_pure_input():
    # 1 百万输入 = 200 分
    assert SAMPLE.cost_cents(1_000_000, 0) == 200


def test_cost_of_pure_output():
    # 1 百万输出 = 800 分
    assert SAMPLE.cost_cents(0, 1_000_000) == 800


def test_cost_of_a_mixed_run():
    # 80 万输入 + 20 万输出 = 160 + 160 = 320 分
    assert SAMPLE.cost_cents(800_000, 200_000) == 320


def test_cost_of_a_typical_single_run():
    """一次测试生成任务大约 10k token，要能算出非零的钱。"""
    assert SAMPLE.cost_cents(8_000, 2_000) == 3


def test_tiny_amounts_round_to_zero_instead_of_going_negative():
    assert SAMPLE.cost_cents(1, 0) == 0


def test_rounding_is_half_up():
    # 2500 输入 = 0.5 分 → 进位成 1 分
    assert SAMPLE.cost_cents(2_500, 0) == 1
    # 2499 输入 = 0.4998 分 → 舍去
    assert SAMPLE.cost_cents(2_499, 0) == 0


# ---------------------------------------------------------------- 观察到的比例


def test_observed_output_ratio():
    assert observed_output_ratio(800, 200) == pytest.approx(0.2)


def test_observed_output_ratio_without_data_is_none():
    assert observed_output_ratio(0, 0) is None


def test_observed_output_ratio_from_breakdown_dict():
    assert observed_output_ratio(
        {"prompt_tokens": 900, "completion_tokens": 100}
    ) == pytest.approx(0.1)


# ---------------------------------------------------------------- 套餐毛利


def test_plan_margins_covers_every_sellable_plan():
    margins = plan_margins(SAMPLE, output_ratio=0.2)
    names = {item.plan for item in margins}
    assert names == {"free", "basic", "pro", "team"}


def test_plan_margin_math():
    """基础版：¥29/月，日额度 20 万 → 月 600 万 token。

    按输出占 20% 算：480 万输入 + 120 万输出
      = 480万×200 + 120万×800 = 9.6亿 + 9.6亿 = 19.2亿「分×百万分之一」
      = 1920 分 = ¥19.20
    售价 2900 分（¥29）→ 毛利 980 分（¥9.80），约 34%。

    也就是说：用户天天跑满，基础版还剩三成毛利——这个数字才是定价要看的。
    """
    margins = {item.plan: item for item in plan_margins(SAMPLE, output_ratio=0.2)}
    basic = margins["basic"]
    assert basic.monthly_tokens == 6_000_000
    assert basic.estimated_cost_cents == 1920
    assert basic.price_cents == 2900
    assert basic.estimated_margin_cents == 980


def test_worst_case_assumes_everything_is_output():
    """全是输出是上界——不现实，但那是"最坏能亏到哪"的答案。"""
    margins = {item.plan: item for item in plan_margins(SAMPLE, output_ratio=0.2)}
    basic = margins["basic"]
    # 6 百万全按输出：6 × 800 = 4800 分 = ¥48，比售价 2900 还高 —— 亏
    assert basic.worst_case_cost_cents == 4800
    assert basic.worst_case_margin_cents == 2900 - 4800


def test_free_plan_margin_is_negative_by_definition():
    """免费套餐的"毛利"永远是负的，这是它的定义。"""
    margins = {item.plan: item for item in plan_margins(SAMPLE, output_ratio=0.2)}
    assert margins["free"].price_cents == 0
    assert margins["free"].estimated_margin_cents < 0


def test_team_plan_is_the_riskiest():
    """额度给得越大，单用户成本越接近卖价——这是定价最容易翻车的地方。"""
    margins = {item.plan: item for item in plan_margins(SAMPLE, output_ratio=0.2)}
    team = margins["team"]
    basic = margins["basic"]
    assert team.worst_case_margin_cents < team.price_cents
    assert team.worst_case_margin_cents < basic.worst_case_margin_cents


def test_without_an_observed_ratio_only_the_worst_case_is_reported():
    margins = {item.plan: item for item in plan_margins(SAMPLE, output_ratio=None)}
    basic = margins["basic"]
    assert basic.estimated_cost_cents is None
    assert basic.estimated_margin_cents is None
    assert basic.worst_case_cost_cents == 4800


def test_internal_plan_is_not_in_the_margin_table():
    """owner 是不限量套餐，算"毛利"没有意义。"""
    assert PLANS["owner"].sellable is False
    assert "owner" not in {item.plan for item in plan_margins(SAMPLE, output_ratio=0.2)}


def test_price_from_settings(monkeypatch):
    from agentcode.config import Settings

    monkeypatch.setenv("AGENT_PRICE_INPUT_PER_MILLION", "200")
    monkeypatch.setenv("AGENT_PRICE_OUTPUT_PER_MILLION", "800")
    settings = Settings.from_env(search_parents=False)
    price = price_from_settings(settings)
    assert price.input_cents_per_million == 200
    assert price.output_cents_per_million == 800


def test_price_defaults_to_zero_when_unset(monkeypatch):
    from agentcode.config import Settings

    monkeypatch.delenv("AGENT_PRICE_INPUT_PER_MILLION", raising=False)
    monkeypatch.delenv("AGENT_PRICE_OUTPUT_PER_MILLION", raising=False)
    price = price_from_settings(Settings.from_env(search_parents=False))
    assert price.configured is False
