"""单次运行的上下文：贯穿智能体循环，收集轨迹与统计信息。"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from agentcode.core.result import Step, TokenUsage


@dataclass
class RunContext:
    """一次 ``run`` 调用期间共享的状态。"""

    task: str = ""
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    steps: list[Step] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    extras: dict[str, Any] = field(default_factory=dict)
    _monotonic: float = field(default_factory=time.monotonic, repr=False)

    def next_index(self) -> int:
        """下一个步骤序号（从 1 开始）。"""
        return len(self.steps) + 1

    def add_step(self, step: Step) -> Step:
        """追加一条步骤记录。"""
        self.steps.append(step)
        return step

    def elapsed_ms(self) -> float:
        """已经过的时间（毫秒）。"""
        return (time.monotonic() - self._monotonic) * 1000

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        return {
            "run_id": self.run_id,
            "task": self.task,
            "created_at": self.created_at.isoformat(),
            "duration_ms": round(self.elapsed_ms(), 2),
            "usage": self.usage.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
        }
