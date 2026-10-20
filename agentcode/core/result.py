"""运行结果数据结构：可以直接序列化为轨迹 JSON。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass
class TokenUsage:
    """token 用量统计。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0
    estimated: bool = False
    #: 供应商侧提示词缓存的命中/未命中 token（没报就不显示）
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """输入与输出 token 之和。"""
        return self.prompt_tokens + self.completion_tokens

    def add(self, usage: Mapping[str, Any] | None) -> None:
        """累加一次调用的用量。"""
        self.calls += 1
        if not usage:
            return
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        if usage.get("estimated"):
            self.estimated = True
        self.cache_hit_tokens += int(usage.get("cache_hit_tokens") or 0)
        self.cache_miss_tokens += int(usage.get("cache_miss_tokens") or 0)

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        data: dict[str, Any] = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "estimated": self.estimated,
        }
        if self.cache_hit_tokens:
            data["cache_hit_tokens"] = self.cache_hit_tokens
        if self.cache_miss_tokens:
            data["cache_miss_tokens"] = self.cache_miss_tokens
        return data


@dataclass
class Step:
    """一次"思考—行动—观察"的轨迹记录。"""

    index: int
    thought: str = ""
    action: str = ""
    tool: str | None = None
    tool_input: str | None = None
    observation: str | None = None
    answer: str | None = None
    duration_ms: float = 0.0
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        data: dict[str, Any] = {
            "index": self.index,
            "thought": self.thought,
            "action": self.action,
            "tool": self.tool,
            "tool_input": self.tool_input,
            "observation": self.observation,
            "duration_ms": round(self.duration_ms, 2),
        }
        if self.answer is not None:
            data["answer"] = self.answer
        if self.error is not None:
            data["error"] = self.error
        if self.extra:
            data["extra"] = self.extra
        return data


@dataclass
class AgentResult:
    """一次智能体运行的完整结果。"""

    agent: str
    task: str
    answer: str = ""
    success: bool = True
    steps: list[Step] = field(default_factory=list)
    error: str | None = None
    duration_ms: float = 0.0
    usage: TokenUsage = field(default_factory=TokenUsage)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典（轨迹 JSON 的顶层结构）。"""
        data: dict[str, Any] = {
            "agent": self.agent,
            "task": self.task,
            "answer": self.answer,
            "success": self.success,
            "duration_ms": round(self.duration_ms, 2),
            "usage": self.usage.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
        }
        if self.error is not None:
            data["error"] = self.error
        if self.extra:
            data["extra"] = self.extra
        return data
