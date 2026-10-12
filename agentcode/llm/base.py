"""大语言模型后端的抽象接口。

后端只需实现 :meth:`BaseLLM.think`（输入消息、返回文本）。想支持**原生工具调用**
（把工具以 JSON Schema 交给模型，模型回 ``tool_calls``）的后端额外做两件事：

- 把 :attr:`BaseLLM.supports_tools` 置为 ``True``；
- 覆盖 :meth:`BaseLLM.complete`，在拿到 ``tools`` 时按工具调用协议发请求。

没有覆盖 ``complete`` 的后端也能照常工作：基类会把 ``think`` 的文本结果包成
:class:`LLMReply`，于是一行都不用改（离线脚本模型就属于这一类）。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

#: 一条消息。带工具调用时会有 ``tool_calls`` / ``tool_call_id`` 这类字段，
#: 所以值不限于字符串。
Message = Mapping[str, Any]


@dataclass
class ToolCallRequest:
    """模型要求调用一次工具。

    ``arguments`` 是模型给的**原始 JSON 字符串**（各家格式略有出入，例如空参数
    可能是空串），所以这里不做强制解析，解析失败时按"没有参数"处理。
    """

    id: str
    name: str
    arguments: str = ""

    def arguments_dict(self) -> dict[str, Any]:
        """把参数解析成字典；解析不出来就当作无参数。"""
        text = (self.arguments or "").strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return {}
        return dict(value) if isinstance(value, Mapping) else {}

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass
class LLMReply:
    """一次模型调用的完整回复：文本 + （可能的）工具调用。"""

    text: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    #: 结束原因（例如 ``tool_calls`` / ``stop``），仅用于展示与排错
    finish_reason: str = ""

    @property
    def has_tool_calls(self) -> bool:
        """这次回复是不是工具调用。"""
        return bool(self.tool_calls)

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        return {
            "text": self.text,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "finish_reason": self.finish_reason,
        }


class BaseLLM(ABC):
    """所有后端都只需要实现 ``think``。

    ``last_usage`` 用于把 token 消耗透出给中间件与结果统计，
    后端不支持用量统计时可以返回 ``None`` 或估算值。
    """

    #: 后端名称，用于日志与展示
    name: str = "base"
    #: 这个后端支不支持"把工具 schema 交给模型"的原生工具调用
    supports_tools: bool = False

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

    def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> LLMReply:
        """输入消息列表，返回完整回复（文本 + 工具调用）。

        基类实现就是"调 ``think`` 再把文本包起来"：不支持工具调用的后端
        （以及所有老后端）什么都不用改。传进来的 ``tools`` 会被忽略。
        """
        return LLMReply(text=self.think(messages, temperature=temperature))
