"""网页会话仓库：会话名、完整对话记录、上下文记忆与磁盘持久化。

每个会话保存三样东西：

1. 元信息：id、名字、创建/更新时间；
2. 完整对话记录：用于切回旧会话时还原界面（不只是最近几轮）；
3. 智能体实例：内存里的上下文记忆，进程内复用；进程重启后由对话记录重建。

会话默认落盘到 ``traces/web-sessions/<id>.json``，重启服务后列表与记录都还在。
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agentcode.core.agent import BaseAgent
from agentcode.memory import ShortTermMemory

DEFAULT_MAX_SESSIONS = 20
DEFAULT_MEMORY_TURNS = 5
DEFAULT_SESSION_NAME = "新会话"
DEFAULT_SESSION_DIR = "traces/web-sessions"
AUTO_NAME_LENGTH = 16
MAX_NAME_LENGTH = 60
MAX_SESSION_ID_LENGTH = 64

_SAFE_ID_PATTERN = re.compile(r"[^A-Za-z0-9_-]")


def default_session_dir() -> str:
    """网页会话的默认落盘目录（单独抽成函数，便于测试替换）。"""
    return DEFAULT_SESSION_DIR


def _now() -> str:
    """当前时间的 ISO 字符串（UTC，精确到秒）。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def clean_name(name: Any, fallback: str = DEFAULT_SESSION_NAME) -> str:
    """把用户输入的名字压成一行并限制长度。"""
    text = " ".join(str(name or "").split())
    return text[:MAX_NAME_LENGTH] or fallback


@dataclass
class SessionRecord:
    """一个会话的全部状态。"""

    id: str
    name: str = DEFAULT_SESSION_NAME
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    messages: list[dict[str, Any]] = field(default_factory=list)
    agent: BaseAgent | None = field(default=None, repr=False, compare=False)

    def summary(self) -> dict[str, Any]:
        """给列表用的轻量信息。"""
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": len(self.messages),
            "turns": len(self.agent.memory) if self.agent is not None else 0,
        }

    def to_dict(self) -> dict[str, Any]:
        """落盘用的结构（不含智能体实例）。"""
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [dict(message) for message in self.messages],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SessionRecord":
        """从落盘内容还原。"""
        messages = [
            dict(message) for message in payload.get("messages", []) if isinstance(message, dict)
        ]
        return cls(
            id=str(payload.get("id") or uuid.uuid4().hex[:12]),
            name=clean_name(payload.get("name")),
            created_at=str(payload.get("created_at") or _now()),
            updated_at=str(payload.get("updated_at") or _now()),
            messages=messages,
        )


class SessionStore:
    """会话仓库（线程安全）。"""

    def __init__(
        self,
        directory: str | Path | None = None,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_turns: int = DEFAULT_MEMORY_TURNS,
    ) -> None:
        if max_sessions <= 0:
            raise ValueError("max_sessions 必须大于 0。")
        if max_turns <= 0:
            raise ValueError("max_turns 必须大于 0。")
        # 传 "" 表示纯内存（测试用）；传 None 表示用默认目录
        if directory is None:
            directory = default_session_dir()
        self.directory = Path(directory) if directory else None
        self.max_sessions = max_sessions
        self.max_turns = max_turns
        self._sessions: dict[str, SessionRecord] = {}
        self._lock = threading.RLock()
        self._load_from_disk()

    # ------------------------------------------------------------------ 磁盘

    @staticmethod
    def _safe_id(session_id: str) -> str:
        """把会话 id 收成安全的文件名片段。"""
        cleaned = _SAFE_ID_PATTERN.sub("", str(session_id or ""))[:MAX_SESSION_ID_LENGTH]
        return cleaned or uuid.uuid4().hex[:12]

    def _path(self, session_id: str) -> Path | None:
        if self.directory is None:
            return None
        return self.directory / f"{self._safe_id(session_id)}.json"

    def _load_from_disk(self) -> None:
        """启动时把已有会话读进内存；坏文件直接跳过。"""
        if self.directory is None or not self.directory.is_dir():
            return
        for path in sorted(self.directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            record = SessionRecord.from_dict(payload)
            self._sessions[record.id] = record

    def _persist(self, record: SessionRecord) -> None:
        """把会话写回磁盘（失败只打印警告，不影响对话）。"""
        path = self._path(record.id)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            print(f"警告：会话文件写入失败（{exc}）。")

    def _remove_file(self, session_id: str) -> None:
        path = self._path(session_id)
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _prune(self) -> None:
        """超出上限时丢掉最久没用的会话（连同文件）。"""
        while len(self._sessions) > self.max_sessions:
            oldest = min(self._sessions.values(), key=lambda item: item.updated_at)
            self._sessions.pop(oldest.id, None)
            self._remove_file(oldest.id)

    # ------------------------------------------------------------- 增删改查

    def list(self) -> list[dict[str, Any]]:
        """按最近使用倒序列出会话。"""
        with self._lock:
            records = sorted(self._sessions.values(), key=lambda item: item.updated_at, reverse=True)
            return [record.summary() for record in records]

    def record(self, session_id: str) -> SessionRecord | None:
        """取会话记录，不存在返回 None。"""
        with self._lock:
            return self._sessions.get(str(session_id or ""))

    def ensure(self, session_id: str | None = None, name: Any = None) -> SessionRecord:
        """取会话；不存在就用给定 id（或新 id）建一个。"""
        with self._lock:
            if session_id:
                record = self._sessions.get(str(session_id))
                if record is not None:
                    return record
                record = SessionRecord(id=self._safe_id(session_id), name=clean_name(name))
                self._sessions[record.id] = record
                self._persist(record)
                self._prune()
                return record
            return self.create(name=name)

    def create(self, name: Any = None) -> SessionRecord:
        """新建一个会话。"""
        with self._lock:
            record = SessionRecord(id=uuid.uuid4().hex[:12], name=clean_name(name))
            self._sessions[record.id] = record
            self._persist(record)
            self._prune()
            return record

    def rename(self, session_id: str, name: Any) -> bool:
        """给会话改名；会话不存在返回 False。"""
        with self._lock:
            record = self._sessions.get(str(session_id or ""))
            if record is None:
                return False
            record.name = clean_name(name)
            record.updated_at = _now()
            self._persist(record)
            return True

    def delete(self, session_id: str) -> bool:
        """删除会话及其文件。"""
        with self._lock:
            record = self._sessions.pop(str(session_id or ""), None)
            if record is None:
                return False
            self._remove_file(record.id)
            return True

    def reset(self, session_id: str) -> bool:
        """清空某个会话的记录与记忆（会话本身保留）。"""
        with self._lock:
            record = self._sessions.get(str(session_id or ""))
            if record is None:
                return False
            record.messages.clear()
            record.agent = None
            record.updated_at = _now()
            self._persist(record)
            return True

    # --------------------------------------------------------------- 对话

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        success: bool = True,
    ) -> None:
        """往会话里追加一条消息；第一条用户消息会自动作为会话名。"""
        text = str(content or "").strip()
        if not text:
            return
        with self._lock:
            record = self.ensure(session_id)
            record.messages.append(
                {
                    "role": "user" if role == "user" else "assistant",
                    "content": text,
                    "at": _now(),
                    "success": bool(success),
                }
            )
            if role == "user" and record.name == DEFAULT_SESSION_NAME:
                first_line = text.splitlines()[0]
                record.name = clean_name(first_line[:AUTO_NAME_LENGTH])
            record.updated_at = _now()
            self._persist(record)

    def messages(self, session_id: str) -> list[dict[str, Any]]:
        """取会话的完整对话记录。"""
        record = self.record(session_id)
        return [dict(message) for message in record.messages] if record else []

    # ------------------------------------------------------- 智能体与记忆

    def get(
        self,
        session_id: str,
        factory: Callable[[], BaseAgent],
        max_turns: int | None = None,
    ) -> BaseAgent:
        """取会话的智能体；首次使用时用 ``factory`` 创建，并从记录重建记忆。

        ``max_turns`` 用于重建记忆时覆盖仓库默认值（网页会传当前配置值）。
        """
        with self._lock:
            record = self.ensure(session_id)
            if record.agent is None:
                agent = factory()
                agent.memory = self._rebuild_memory(record, max_turns or self.max_turns)
                record.agent = agent
            return record.agent

    def _rebuild_memory(self, record: SessionRecord, max_turns: int | None = None) -> ShortTermMemory:
        """从对话记录重建短期记忆（只取一问一答都成功完成的轮次）。"""
        memory = ShortTermMemory(max_turns=max_turns or self.max_turns)
        pending: dict[str, Any] | None = None
        for message in record.messages:
            if message.get("role") == "user":
                pending = message
            elif message.get("role") == "assistant" and pending is not None:
                if message.get("success", True):
                    memory.add_turn(
                        [
                            {"role": "user", "content": pending["content"]},
                            {"role": "assistant", "content": message["content"]},
                        ]
                    )
                pending = None
        return memory

    # --------------------------------------------------------------- 兼容

    def turns(self, session_id: str) -> int:
        """该会话记忆里当前保留的轮数。"""
        record = self.record(session_id)
        return len(record.agent.memory) if record and record.agent else 0

    def session_ids(self) -> list[str]:
        """当前保留的会话 id，按最近使用顺序。"""
        return [item["id"] for item in self.list()]

    def __contains__(self, session_id: object) -> bool:
        return str(session_id) in self._sessions

    def __len__(self) -> int:
        return len(self._sessions)


#: 兼容旧名字（早期版本只有"智能体会话表"这一个职责）
AgentSessionStore = SessionStore
