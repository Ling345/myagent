"""一个用来演示「测试生成智能体」的小模块。

故意留了几个容易踩的边界：除零、负数开方、非法输入类型。
"""

from __future__ import annotations

import math


def add(a: float, b: float) -> float:
    """返回两数之和。"""
    return a + b


def subtract(a: float, b: float) -> float:
    """返回 a 减 b。"""
    return a - b


def divide(a: float, b: float) -> float:
    """返回 a 除以 b；除数为 0 时抛 ValueError。"""
    if b == 0:
        raise ValueError("除数不能为 0")
    return a / b


def average(numbers: list[float]) -> float:
    """返回平均值；空列表抛 ValueError。"""
    if not numbers:
        raise ValueError("列表不能为空")
    return sum(numbers) / len(numbers)


def safe_sqrt(value: float) -> float | None:
    """返回平方根；负数返回 None（不抛异常）。"""
    if value < 0:
        return None
    return math.sqrt(value)
