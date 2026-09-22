"""智能体注册表：CLI 与外部代码通过它发现并创建智能体。"""

from __future__ import annotations

from typing import Any, Callable

from agentcode.core.agent import BaseAgent
from agentcode.core.errors import AgentCodeError, AgentNotFoundError

AgentFactory = Callable[..., BaseAgent]


class AgentRegistry:
    """名称到工厂函数的注册表。

    工厂通常就是智能体类本身，因此 ``create`` 的额外参数会原样透传给构造函数。
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[AgentFactory, str]] = {}

    def register(
        self,
        name: str,
        factory: AgentFactory,
        description: str = "",
        overwrite: bool = False,
    ) -> None:
        """登记一个智能体工厂。"""
        if not name or not name.strip():
            raise AgentCodeError("智能体名称不能为空。")
        if name in self._entries and not overwrite:
            raise AgentCodeError(f"智能体 '{name}' 已注册，如需替换请显式指定 overwrite=True。")
        self._entries[name] = (factory, description)

    def get(self, name: str) -> AgentFactory:
        """取回工厂函数，不存在时抛出 :class:`AgentNotFoundError`。"""
        entry = self._entries.get(name)
        if entry is None:
            raise AgentNotFoundError(
                f"未找到名为 '{name}' 的智能体。可用智能体：{'、'.join(self.names()) or '无'}。"
            )
        return entry[0]

    def create(self, name: str, **kwargs: Any) -> BaseAgent:
        """创建智能体实例。"""
        return self.get(name)(**kwargs)

    def names(self) -> list[str]:
        """全部已注册的名称。"""
        return list(self._entries)

    def description_of(self, name: str) -> str:
        """某个智能体的说明文本。"""
        entry = self._entries.get(name)
        return entry[1] if entry else ""

    def describe(self) -> str:
        """生成面向用户的清单文本。"""
        if not self._entries:
            return "（尚未注册任何智能体）"
        return "\n".join(
            f"- {name}: {description or '（无说明）'}"
            for name, (_, description) in self._entries.items()
        )

    def to_dict(self) -> dict[str, str]:
        """转为可序列化字典。"""
        return {name: description for name, (_, description) in self._entries.items()}

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    def __len__(self) -> int:
        return len(self._entries)


#: 全局默认注册表；内置智能体在 ``agentcode.agents`` 中登记
default_registry = AgentRegistry()


def register_agent(name: str, description: str = "") -> Callable[[type], type]:
    """类装饰器：把智能体类登记进默认注册表。"""

    def decorator(cls: type) -> type:
        default_registry.register(
            name, cls, description or (cls.__doc__ or "").strip().splitlines()[0]
        )
        return cls

    return decorator
