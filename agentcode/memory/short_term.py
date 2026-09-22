"""短期记忆：只保留最近若干轮对话，控制提示词长度。"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from agentcode.llm.base import Message


class ShortTermMemory:
    """按"轮"保存消息，超出上限时丢弃最早的轮次。

    一轮 = 用户提问 + 助手回答，由智能体在一轮运行结束后整体写入，
    因此记忆里不会有"只问没答"的半截记录。
    """

    def __init__(self, max_turns: int = 10) -> None:
        if max_turns <= 0:
            raise ValueError("max_turns 必须大于 0。")
        self.max_turns = max_turns
        self._turns: list[list[Message]] = []

    def add_turn(self, messages: Iterable[Message]) -> None:
        """追加一轮对话。"""
        turn = [dict(message) for message in messages]
        if not turn:
            return
        self._turns.append(turn)
        if len(self._turns) > self.max_turns:
            self._turns = self._turns[-self.max_turns :]

    def get_messages(self) -> list[Message]:
        """按时间顺序展开全部保留的消息。"""
        return [message for turn in self._turns for message in turn]

    def as_dialogue(self) -> str:
        """渲染成"用户：…／助手：…"的对话文本，供提示词使用。"""
        lines: list[str] = []
        for turn in self._turns:
            for message in turn:
                content = str(message.get("content", "")).strip()
                if not content:
                    continue
                role = "用户" if message.get("role") == "user" else "助手"
                lines.append(f"{role}：{content}")
        return "\n".join(lines)

    def clear(self) -> None:
        """清空记忆。"""
        self._turns = []

    @property
    def turns(self) -> Sequence[Sequence[Message]]:
        """当前的轮次（只读视图）。"""
        return tuple(tuple(turn) for turn in self._turns)

    def __len__(self) -> int:
        return len(self._turns)
