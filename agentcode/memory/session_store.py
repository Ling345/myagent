"""把会话记忆落盘，供命令行跨次运行复用。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agentcode.core.errors import AgentCodeError
from agentcode.memory.short_term import ShortTermMemory

#: 默认的会话目录与每个会话保留的轮数
DEFAULT_SESSION_DIR = "traces/sessions"
DEFAULT_MAX_TURNS = 5


class FileSessionStore:
    """一个会话一个 JSON 文件，内容是最多若干轮对话。"""

    def __init__(
        self,
        directory: str | Path = DEFAULT_SESSION_DIR,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        self.directory = Path(directory)
        self.max_turns = max_turns

    @staticmethod
    def _safe_name(name: str) -> str:
        """把会话名限制成安全的文件名（中文、字母、数字、``-_``）。"""
        cleaned = "".join(ch for ch in str(name).strip() if ch.isalnum() or ch in "-_")
        if not cleaned:
            raise AgentCodeError("会话名称不能为空，且只能包含中英文、数字、- 和 _。")
        return cleaned[:64]

    def path_for(self, name: str) -> Path:
        """会话文件路径。"""
        return self.directory / f"{self._safe_name(name)}.json"

    def load(self, name: str) -> ShortTermMemory:
        """读取会话记忆；文件不存在或损坏时返回空记忆。"""
        memory = ShortTermMemory(max_turns=self.max_turns)
        path = self.path_for(name)
        if not path.is_file():
            return memory
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            turns = payload.get("turns", [])
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            print(f"警告：会话文件无法读取，将按新会话处理（{exc}）。")
            return memory
        for turn in turns:
            if isinstance(turn, list):
                memory.add_turn([item for item in turn if isinstance(item, dict)])
        return memory

    def save(self, name: str, memory: ShortTermMemory) -> Path:
        """把会话记忆写入磁盘，返回文件路径。"""
        path = self.path_for(name)
        payload = {
            "session": name,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "turns": [[dict(message) for message in turn] for turn in memory.turns],
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            raise AgentCodeError(f"写入会话文件失败：{path}（{exc}）") from exc
        return path
