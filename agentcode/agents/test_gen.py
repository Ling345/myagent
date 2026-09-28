"""测试生成智能体：读源码 → 生成 pytest 用例 → 真的跑一遍 → 失败就修。

与 coding 智能体的区别：它的产物是**测试**，而被测源码只读不改——
如果源码本身有问题，它只负责在结论里指出，不擅自修改。
"""

from __future__ import annotations

from agentcode.agents.prompts import TEST_GEN_PROMPT_TEMPLATE
from agentcode.agents.react import ReActAgent
from agentcode.core.context import RunContext
from agentcode.core.registry import register_agent
from agentcode.core.result import AgentResult

#: 读源码 + 写测试 + 跑测试 + 至少两轮修正，给足步数
DEFAULT_TEST_GEN_STEPS = 16


@register_agent("test_gen", "测试生成智能体：为指定源码生成 pytest 用例，并真的跑通。")
class TestGenerationAgent(ReActAgent):
    """测试生成智能体。"""

    name = "test_gen"
    description = "测试生成智能体：为指定源码生成 pytest 用例，并真的跑通。"
    default_max_steps = DEFAULT_TEST_GEN_STEPS

    def _build_prompt(self, task: str, steps: list[str], history: str) -> str:
        """渲染测试生成提示词。"""
        return TEST_GEN_PROMPT_TEMPLATE.format(
            tools=self.tools.describe(),
            question=task,
            history=history,
            steps="\n".join(steps) if steps else "（暂无）",
        )

    def run(self, task: str, context: RunContext | None = None) -> AgentResult:
        """前置检查：没有代码工具就明确报错，而不是空转十几步。"""
        missing = [
            name for name in ("read_file", "write_file", "run_python") if self.tools.get(name) is None
        ]
        if missing:
            ctx = context or self._new_context(task)
            return self._build_result(
                task,
                "",
                ctx,
                success=False,
                error=(
                    "测试生成需要代码工具（缺少：" + "、".join(missing) + "）。"
                    "网页端请在 .env 里设 AGENT_ALLOW_CODE_TOOLS=true 后重启服务；"
                    "命令行默认已开启。"
                ),
            )
        return super().run(task, context=context)
