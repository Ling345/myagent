"""工具注册表：统一管理工具的名称、描述、参数与调用。

设计要点：

- 工具执行失败不抛异常，而是返回中文错误字符串，
  这样智能体循环可以把错误当作 Observation 继续推理。
- ``invoke`` 同时支持 ``工具名[字符串参数]`` 与 ``工具名(key=value)`` 两种调用形态。
- 引号包裹的参数会还原常用转义（``\\n``、``\\t``、``\\"`` 等），
  这样模型写 ``content="第一行\\n第二行"`` 时能真的换行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from agentcode.core.errors import ToolError

#: 引号内允许还原的转义序列
_ESCAPE_MAP = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "'": "'", "\\": "\\"}
_ESCAPE_PATTERN = re.compile(r"\\(.)", re.DOTALL)


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的完整描述。"""

    name: str
    description: str
    func: Callable[..., Any]
    parameters: dict[str, str] = field(default_factory=dict)

    def signature_hint(self) -> str:
        """生成 ``参数名: 说明`` 形式的参数提示。"""
        if not self.parameters:
            return "无参数"
        return "，".join(f"{name}: {desc}" for name, desc in self.parameters.items())

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
        }


def _split_top_level(text: str) -> list[str]:
    """按顶层逗号切分字符串，忽略引号内的逗号（含被转义的引号）。"""
    parts: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    escaped = False
    for char in text:
        if quote:
            buffer.append(char)
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            buffer.append(char)
            continue
        if char == ",":
            parts.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)
    parts.append("".join(buffer))
    return [part.strip() for part in parts if part.strip()]


def _unescape(value: str) -> str:
    """还原常用转义序列；不认识的转义原样保留。"""
    return _ESCAPE_PATTERN.sub(
        lambda match: _ESCAPE_MAP.get(match.group(1), match.group(0)), value
    )


def _unquote(value: str) -> str:
    """去掉最外层成对的引号，并还原其中的转义序列。

    只有被引号包裹的值才还原转义，避免把 ``traces\\new`` 这类未加引号的
    Windows 路径误伤成换行。
    """
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return _unescape(value[1:-1])
    return value


def parse_kwargs(raw_input: str) -> dict[str, str] | None:
    """把 ``a=1, b="文本"`` 解析为字典；不是关键字形式时返回 None。"""
    text = raw_input.strip()
    if not text or "=" not in text:
        return None
    kwargs: dict[str, str] = {}
    for part in _split_top_level(text):
        match = re.match(r"^([A-Za-z_]\w*)\s*=\s*(.*)$", part, re.DOTALL)
        if not match:
            return None
        kwargs[match.group(1)] = _unquote(match.group(2))
    return kwargs or None


def parse_known_kwargs(raw_input: str, keys: Iterable[str]) -> dict[str, str] | None:
    """按**已知参数名**切分参数，容忍值里出现未转义的引号或逗号。

    模型写 ``content="print("hi")"`` 这种内层引号没转义的内容时，
    逐字符扫描引号会误判边界；这里改成"从参数名切刀"，把两把刀之间的整段
    都当作值，代码里的引号和逗号就不会破坏解析。
    """
    key_list = [key for key in keys if key]
    if not key_list:
        return None
    pattern = re.compile(r"(?<!\w)(" + "|".join(re.escape(key) for key in key_list) + r")\s*=\s*")
    matches = list(pattern.finditer(raw_input))
    if not matches:
        return None

    kwargs: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(raw_input)
        value = raw_input[start:end].strip()
        value = value.rstrip().rstrip(",").strip()
        kwargs[match.group(1)] = _unquote(value)
    return kwargs or None


class ToolRegistry:
    """工具注册表。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    # ------------------------------------------------------------------ 注册

    def register_tool(
        self,
        name: str,
        description: str,
        func: Callable[..., Any],
        parameters: dict[str, str] | None = None,
        overwrite: bool = False,
    ) -> ToolSpec:
        """注册一个工具；名称重复且未指定覆盖时抛出 :class:`ToolError`。"""
        if not name or not name.strip():
            raise ToolError("工具名称不能为空。")
        if name in self._tools and not overwrite:
            raise ToolError(f"工具 '{name}' 已注册，如需替换请显式指定 overwrite=True。")
        spec = ToolSpec(
            name=name,
            description=description,
            func=func,
            parameters=dict(parameters or {}),
        )
        self._tools[name] = spec
        return spec

    def tool(
        self,
        name: str | None = None,
        description: str = "",
        parameters: dict[str, str] | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """装饰器形式的注册入口。"""

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.register_tool(
                name=name or func.__name__,
                description=description or (func.__doc__ or "").strip().splitlines()[0],
                func=func,
                parameters=parameters,
            )
            return func

        return decorator

    def extend(self, specs: Iterable[ToolSpec]) -> None:
        """批量登记已有的工具规格。"""
        for spec in specs:
            self.register_tool(spec.name, spec.description, spec.func, spec.parameters, overwrite=True)

    # ------------------------------------------------------------------ 查询

    def get(self, name: str) -> ToolSpec | None:
        """按名称取回工具规格，不存在返回 None。"""
        return self._tools.get(name)

    def names(self) -> list[str]:
        """返回全部工具名称。"""
        return list(self._tools)

    def describe(self) -> str:
        """生成供提示词使用的工具清单。"""
        if not self._tools:
            return "（当前没有可用工具）"
        return "\n".join(
            f"- {spec.name}: {spec.description}（参数：{spec.signature_hint()}）"
            for spec in self._tools.values()
        )

    # ------------------------------------------------------------------ 调用

    def _build_call(self, spec: ToolSpec, raw_input: str) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """根据输入文本决定位置参数还是关键字参数。"""
        text = (raw_input or "").strip()
        if not text:
            return (), {}
        kwargs = parse_kwargs(text)
        if kwargs is None:
            # 退一步：按该工具声明的参数名切分，容忍值里的引号/逗号
            kwargs = parse_known_kwargs(text, spec.parameters)
        if kwargs is not None:
            return (), kwargs
        return (text,), {}

    def invoke(self, name: str, raw_input: str = "") -> str:
        """调用工具并把结果或错误统一转成字符串。"""
        spec = self.get(name)
        if spec is None:
            available = "、".join(self._tools) or "无"
            return f"错误：未注册的工具 '{name}'。可用工具：{available}。"
        args, kwargs = self._build_call(spec, raw_input)
        try:
            result = spec.func(*args, **kwargs)
        except TypeError as exc:
            return f"错误：工具 '{name}' 参数不匹配（{exc}）。"
        except Exception as exc:  # noqa: BLE001 - 工具异常一律转为可读信息
            return f"错误：工具 '{name}' 执行失败：{exc}"
        return result if isinstance(result, str) else str(result)
