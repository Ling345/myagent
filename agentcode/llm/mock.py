"""离线脚本模型：按预设顺序返回文本，用于测试与无网络演示。"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from agentcode.core.errors import LLMError
from agentcode.llm.base import BaseLLM, Message


def _estimate_usage(messages: Sequence[Message], reply: str) -> dict[str, Any]:
    """按字符数粗略估算 token，仅用于展示。"""
    prompt_chars = sum(len(str(item.get("content", ""))) for item in messages)
    return {
        "prompt_tokens": max(1, prompt_chars // 2),
        "completion_tokens": max(1, len(reply) // 2),
        "estimated": True,
    }


class ScriptedLLM(BaseLLM):
    """按脚本顺序返回回复的假模型。

    脚本用尽后：优先返回 ``fallback``，否则重复最后一次回复；
    脚本为空且从未产出过回复时抛出 :class:`LLMError`。
    这样"模型在循环里反复给出同一种行动"这类场景也能被复现。
    """

    name = "scripted"

    def __init__(
        self,
        responses: Sequence[str],
        fallback: str | None = None,
        temperature: float = 0.0,
    ) -> None:
        super().__init__(temperature=temperature)
        self.responses = list(responses)
        self.fallback = fallback
        self.calls: list[list[Message]] = []
        self._last_reply: str | None = None

    @property
    def remaining(self) -> int:
        """剩余未消费的脚本条数。"""
        return len(self.responses)

    def think(
        self,
        messages: Sequence[Message],
        temperature: float | None = None,
    ) -> str:
        """返回下一条脚本回复。"""
        self.calls.append(list(messages))
        if self.responses:
            reply = self.responses.pop(0)
        elif self.fallback is not None:
            reply = self.fallback
        elif self._last_reply is not None:
            reply = self._last_reply
        else:
            raise LLMError("脚本模型没有更多预设回复了。")
        self._last_reply = reply
        self._last_usage = _estimate_usage(messages, reply)
        return reply


def demo_responses_llm(agent: str = "react") -> ScriptedLLM:
    """构造用于离线演示各智能体的脚本模型。"""
    scripts: dict[str, list[str]] = {
        "react": [
            "Thought: 先了解北京的天气，再推荐景点。\nAction: get_weather[北京]",
            "Thought: 天气信息已拿到，可以给出最终建议。\n"
            "Action: Finish[北京当前晴、气温 24 摄氏度（演示数据），适合去故宫和颐和园。]",
        ],
        "plan_and_solve": [
            "```python\n"
            "[\"统计周一卖出的苹果数\", \"计算周二与周三的数量\", \"汇总三天总数\"]\n"
            "```",
            "周一卖出 15 个苹果（演示数据）。",
            "周二 30 个，周三 25 个（演示数据）。",
            "三天共卖出 70 个苹果（演示数据）。",
        ],
        "reflection": [
            "def find_primes(n):\n"
            "    return [x for x in range(2, n + 1) if all(x % d for d in range(2, x))]",
            "当前实现是 O(n·√n) 级别，可用埃拉托斯特尼筛法把复杂度降到 O(n log log n)，因此需要改进。",
            "def find_primes(n):\n"
            "    flags = [True] * (n + 1)\n"
            "    flags[:2] = [False, False]\n"
            "    for i in range(2, int(n ** 0.5) + 1):\n"
            "        if flags[i]:\n"
            "            for j in range(i * i, n + 1, i):\n"
            "                flags[j] = False\n"
            "    return [i for i, ok in enumerate(flags) if ok]",
            "筛法实现已是该问题的常规最优解，无需改进。",
        ],
        "echo": [
            "Thought: 回显任务内容。\nAction: Finish[已收到任务]",
        ],
    }
    return ScriptedLLM(scripts.get(agent, scripts["react"]))
