"""结构化输出：让模型回一段 JSON，我们来校验，不合格就把问题回给模型重试。

为什么需要它：很多"让模型判断一下"的地方，最早的实现是从自然语言里抠关键词
（例如"看到『需修改』就算不通过"）——模型换个说法就翻车。要求它按约定的
JSON 结构回答，再逐项校验，稳定性会好得多。

三条设计取舍：

1. **不引第三方校验库**：只支持最常用的几件事——必填键、类型、枚举、列表元素
   类型、非空。够用，而且出错信息可以直接讲给模型听；
2. **不强制 ``response_format``**：不是所有兼容 OpenAI 的服务都认这个参数，
   默认纯靠提示词要求 JSON（兼容性最好）；
3. **失败不抛异常**：返回带 ``problems`` 的结果，调用方自己决定"要不要回退到
   老办法"（例如规划器退回按文本解析）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from agentcode.llm.base import LLMReply, Message

#: 「喂消息、拿回复」的函数；可以直接传一个 LLM 对象
Completer = Callable[[Sequence[Message]], LLMReply]

#: 一份轻量 schema：必填键 + 每个键的类型 + 可选枚举/列表元素类型
Schema = Mapping[str, Any]

#: 规划器用的 schema：步骤列表
PLAN_SCHEMA: dict[str, Any] = {
    "required": ["steps"],
    "types": {"steps": "list"},
    "item_types": {"steps": "str"},
    "not_empty": ["steps"],
}


@dataclass
class JsonAnswer:
    """一次"要 JSON"的结果。``payload`` 为空表示最终没拿到合格的答案。"""

    payload: dict[str, Any] | None = None
    text: str = ""
    problems: list[str] = field(default_factory=list)
    attempts: int = 0


def parse_json_object(text: str) -> dict[str, Any] | None:
    """从模型回复里抠出第一个 JSON 对象。

    容忍三种常见写法：纯 JSON、```json 围栏里的、以及前后夹着说明文字的。
    抠不到或不是对象就返回 ``None``。
    """
    content = str(text or "").strip()
    if not content:
        return None
    fenced = _fenced_json(content)
    for candidate in (fenced, content):
        if not candidate:
            continue
        parsed = _load_object(candidate)
        if parsed is not None:
            return parsed
    embedded = _first_object(content)
    return _load_object(embedded) if embedded else None


def _load_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return dict(value) if isinstance(value, Mapping) else None


def _fenced_json(text: str) -> str:
    """取出 ```json ... ``` 里的内容。"""
    marker = "```"
    start = text.find(marker)
    if start < 0:
        return ""
    end = text.find(marker, start + len(marker))
    if end < 0:
        return ""
    body = text[start + len(marker) : end].strip()
    if body.lower().startswith("json"):
        body = body[4:].strip()
    return body


def _first_object(text: str) -> str:
    """按括号配对找出第一段完整的 ``{...}``。"""
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


_TYPE_NAMES = {
    "str": "字符串",
    "int": "整数",
    "float": "数字",
    "bool": "布尔值",
    "list": "列表",
    "dict": "对象",
}


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "str":
        return isinstance(value, str)
    if expected == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "bool":
        return isinstance(value, bool)
    if expected == "list":
        return isinstance(value, list)
    if expected == "dict":
        return isinstance(value, Mapping)
    return True


def validate_json(payload: Mapping[str, Any], schema: Schema) -> list[str]:
    """按 schema 校验，返回中文问题清单（空列表 = 通过）。"""
    problems: list[str] = []
    for key in schema.get("required", ()):
        if key not in payload:
            problems.append(f"缺少字段 {key}")
    for key, expected in (schema.get("types") or {}).items():
        if key in payload and not _type_ok(payload[key], str(expected)):
            problems.append(f"{key} 应该是{_TYPE_NAMES.get(str(expected), expected)}")
    for key, allowed in (schema.get("enums") or {}).items():
        if key in payload and payload[key] not in allowed:
            problems.append(f"{key} 只能是：{'、'.join(str(item) for item in allowed)}")
    for key, item_type in (schema.get("item_types") or {}).items():
        value = payload.get(key)
        if isinstance(value, list):
            for index, item in enumerate(value):
                if not _type_ok(item, str(item_type)):
                    problems.append(
                        f"{key} 的第 {index + 1} 项应该是{_TYPE_NAMES.get(str(item_type), item_type)}"
                    )
    for key in schema.get("not_empty", ()):
        value = payload.get(key)
        if value in ("", None) or (isinstance(value, (list, dict)) and not value):
            problems.append(f"{key} 不能是空的")
    return problems


_RETRY_SUFFIX = """

# 上一次的回答不合格
{problems}

请只输出一个 JSON 对象，不要解释、不要代码围栏之外的文字。"""


def ask_json(
    source: Any,
    messages: Sequence[Message],
    schema: Schema,
    *,
    retries: int = 1,
) -> JsonAnswer:
    """让模型按 schema 回 JSON；不合格时把问题回给模型重试。

    ``source`` 可以是任意实现了 ``complete(messages)`` 的对象（例如 LLM 后端），
    也可以直接是一个「消息 → 回复」的函数——智能体传的是带中间件的调用，
    这样用量统计、重试、超时都还在链上。
    """
    complete = _as_completer(source)
    conversation = list(messages)
    last_text = ""
    problems: list[str] = []
    for attempt in range(1, max(1, int(retries) + 1) + 1):
        reply = complete(conversation)
        last_text = reply.text or ""
        payload = parse_json_object(last_text)
        if payload is None:
            problems = ["回答不是合法的 JSON 对象"]
        else:
            problems = validate_json(payload, schema)
            if not problems:
                return JsonAnswer(payload=payload, text=last_text, attempts=attempt)
        if attempt > max(1, int(retries) + 1) - 1:
            break
        conversation = list(conversation) + [
            {"role": "assistant", "content": last_text},
            {
                "role": "user",
                "content": _RETRY_SUFFIX.format(
                    problems="；".join(f"- {item}" for item in problems)
                ),
            },
        ]
    return JsonAnswer(payload=None, text=last_text, problems=problems, attempts=attempt)


def _as_completer(source: Any) -> Completer:
    """把 LLM 对象或普通函数统一成「消息 → 回复」。"""
    complete = getattr(source, "complete", None)
    if callable(complete):
        return lambda messages: complete(messages)
    if callable(source):
        return source
    raise TypeError("ask_json 需要 LLM 对象或「消息 → LLMReply」的函数。")
