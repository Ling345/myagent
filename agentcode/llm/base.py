"""大语言模型后端的抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping, Sequence

Message = Mapping[str, str]


class BaseLLM(ABC):
    """所有后端都只需要实现 ``think``。

    ``last_usage`` 用于把 token 消耗透出给中间件与结果统计，
    后端不支持用量统计时可以返回 ``None`` 或估算值。
    """

    #: 后端名称，用于日志与展示
    name: str = "base"

    def __init__(self, temperature: float = 0.0) -> None:
        self.temperature = temperature
        self._last_usage: dict[str, Any] | None = None

    @property
    def last_usage(self) -> dict[str, Any] | None:
        """最近一次调用的用量信息。"""
        return self._last_usage

    @abstractmethod
    def think(
        self,
        messages: Sequence[Message],
        temperature: float | None = None,
    ) -> str:
        """输入消息列表，返回模型回复文本。"""
        raise NotImplementedError
