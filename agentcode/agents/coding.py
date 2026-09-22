"""Coding 智能体：写测试 → 写实现 → 跑测试 → 改到全绿。

与 Reflection 的区别：它的反馈来自真正运行出来的结果，而不是模型自评。
"""

from __future__ import annotations

from agentcode.agents.prompts import CODING_PROMPT_TEMPLATE
from agentcode.agents.react import ReActAgent
from agentcode.config import DEFAULT_CODING_STEPS
from agentcode.core.registry import register_agent


@register_agent("coding", "编码智能体：写测试 → 写实现 → 跑测试 → 改到全绿。")
class CodingAgent(ReActAgent):
    """以跑通测试为终止条件的编码智能体。"""

    name = "coding"
    description = "编码智能体：写测试 → 写实现 → 跑测试 → 改到全绿。"
    #: 写代码比闲聊长，默认给更多步数
    default_max_steps = DEFAULT_CODING_STEPS

    def _build_prompt(self, task: str, steps: list[str], history: str) -> str:
        """渲染编码任务提示词。"""
        return CODING_PROMPT_TEMPLATE.format(
            tools=self.tools.describe(),
            question=task,
            history=history,
            steps="\n".join(steps) if steps else "（暂无）",
        )
