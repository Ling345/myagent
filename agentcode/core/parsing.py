"""模型输出的解析工具。

三种智能体共用同一套解析函数，避免提示词格式与解析逻辑不一致。
"""

from __future__ import annotations

import ast
import re
from typing import Any

# 工具名允许中文或英文，只要不含空白与括号
_NAME = r"([^\s\[\]()]+)"
_ACTION_PATTERN = re.compile(r"Action[:：]\s*(.*)", re.DOTALL)
_THOUGHT_PATTERN = re.compile(r"Thought[:：]\s*(.*?)(?=\n\s*Action[:：]|\Z)", re.DOTALL)
_BRACKET_PATTERN = re.compile(rf"^{_NAME}\s*\[(.*)\]\s*$", re.DOTALL)
_CALL_PATTERN = re.compile(rf"^{_NAME}\s*\((.*)\)\s*$", re.DOTALL)
_BARE_NAME_PATTERN = re.compile(rf"^{_NAME}$")
_FENCE_PATTERN = re.compile(r"```(?:python|py|json)?\s*(.*?)```", re.DOTALL)
_LIST_PREFIX_PATTERN = re.compile(r"^\s*(?:[-*•]|\d+\s*[.、)]|\(\d+\))\s*")
#: 模型有时会在 Action 后面顺手编一段 Observation，解析时要切掉
_OBSERVATION_SUFFIX_PATTERN = re.compile(r"\n\s*(?:Observation|观察)[:：].*$", re.DOTALL)

#: 按行兜底解析时，至少需要这么多行才认为它是一份计划
_MIN_FALLBACK_STEPS = 2


def _clean_action(action: str) -> str:
    """清掉模型顺手输出的 Observation 后缀与代码围栏残留。"""
    text = action.strip()
    text = _OBSERVATION_SUFFIX_PATTERN.sub("", text)
    if text.endswith("```"):
        text = text[:-3].strip()
    return text.strip()


def parse_react_output(text: str) -> tuple[str | None, str | None]:
    """从 ReAct 输出中提取 ``(Thought, Action)``。"""
    if not text:
        return None, None
    thought_match = _THOUGHT_PATTERN.search(text)
    action_match = _ACTION_PATTERN.search(text)
    thought = thought_match.group(1).strip() if thought_match else None
    action = _clean_action(action_match.group(1)) if action_match else None
    return thought or None, action or None


def parse_action(action: str) -> tuple[str | None, str | None]:
    """解析行动文本，支持 ``名字[输入]``、``名字(输入)`` 与裸名字三种形式。"""
    text = _clean_action(action or "")
    if not text:
        return None, None
    bracket = _BRACKET_PATTERN.match(text)
    if bracket:
        return bracket.group(1), bracket.group(2).strip()
    call = _CALL_PATTERN.match(text)
    if call:
        return call.group(1), call.group(2).strip()
    bare = _BARE_NAME_PATTERN.match(text)
    if bare:
        return bare.group(1), ""
    return None, None


def parse_finish(action: str) -> str | None:
    """提取最终答案，兼容 ``Finish[答案]`` 与 ``finish(answer="答案")``。"""
    text = _clean_action(action or "")
    name, raw = parse_action(text)
    if name is None or name.lower() != "finish":
        return None
    content = (raw or "").strip()
    keyword = re.match(r"^answer\s*=\s*(.*)$", content, re.DOTALL)
    if keyword:
        content = keyword.group(1).strip()
    if len(content) >= 2 and content[0] == content[-1] and content[0] in {'"', "'"}:
        content = content[1:-1]
    return content


def _literal_to_list(text: str) -> list[str] | None:
    """尝试把文本解析为字符串列表。"""
    try:
        value: Any = ast.literal_eval(text.strip())
    except (ValueError, SyntaxError):
        return None
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value]
        return [item for item in items if item]
    return None


def parse_plan(text: str) -> list[str]:
    """把规划器输出解析为步骤列表，解析失败返回空列表。

    优先读取代码围栏里的 Python 列表；没有围栏时按行兜底，
    但兜底结果至少要有两行，避免把一整句话误当成计划。
    """
    if not text:
        return []
    fence = _FENCE_PATTERN.search(text)
    if fence:
        parsed = _literal_to_list(fence.group(1))
        if parsed:
            return parsed
    parsed = _literal_to_list(text)
    if parsed:
        return parsed

    steps: list[str] = []
    for line in text.splitlines():
        stripped = _LIST_PREFIX_PATTERN.sub("", line.strip())
        if not stripped or stripped.startswith("```"):
            continue
        steps.append(stripped.strip().strip("\"'"))
    steps = [step for step in steps if step]
    return steps if len(steps) >= _MIN_FALLBACK_STEPS else []
