"""按会话复用智能体实例，让网页里的连续提问共享上下文记忆。"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Callable

from agentcode.core.agent import BaseAgent

#: 内存里最多保留多少个会话，超出按最近最少使用淘汰
DEFAULT_MAX_SESSIONS = 20


class AgentSessionStore:
    """``session_id`` → 智能体实例（含记忆）的线程安全 LRU 表。"""

    def __init__(self, max_sessions: int = DEFAULT_MAX_SESSIONS) -> None:
        if max_sessions <= 0:
            raise ValueError("max_sessions 必须大于 0。")
        self.max_sessions = max_sessions
        self._sessions: OrderedDict[str, BaseAgent] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _normalize(session_id: str) -> str:
        """校验并裁剪会话标识。"""
        key = str(session_id or "").strip()
        if not key:
            raise ValueError("session_id 不能为空。")
        return key[:128]

    def get(self, session_id: str, factory: Callable[[], BaseAgent]) -> BaseAgent:
        """取回会话的智能体；不存在就用 ``factory`` 创建并记住。"""
        key = self._normalize(session_id)
        with self._lock:
            agent = self._sessions.get(key)
            if agent is None:
                agent = factory()
                self._sessions[key] = agent
            self._sessions.move_to_end(key)
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)
            return agent

    def reset(self, session_id: str) -> bool:
        """丢弃一个会话；确实删掉了才返回 True。"""
        key = self._normalize(session_id)
        with self._lock:
            return self._sessions.pop(key, None) is not None

    def turns(self, session_id: str) -> int:
        """该会话已经记住的轮数（不存在则为 0）。"""
        try:
            key = self._normalize(session_id)
        except ValueError:
            return 0
        with self._lock:
            agent = self._sessions.get(key)
            return len(agent.memory) if agent is not None else 0

    def session_ids(self) -> list[str]:
        """当前保留的会话标识，按最近使用顺序。"""
        with self._lock:
            return list(self._sessions)

    def __contains__(self, session_id: object) -> bool:
        with self._lock:
            return str(session_id) in self._sessions

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
