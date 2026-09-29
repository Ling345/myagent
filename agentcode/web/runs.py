"""运行注册表：把一次任务的执行与它的 HTTP 连接解耦。

为什么需要它：一次任务要调用模型十几二十次，跑几十秒。用户刷新页面、切走标签页、
网络抖一下，原来的 SSE 连接就断了。如果事件只存在于那条连接里，用户就再也看不到结果——
更糟的是额度照样在扣，而他什么都不知道。

所以运行的状态放在服务端：

- 事件产生后先进缓冲区，再通知所有订阅者；
- 连接断了运行照跑，重新连上时用事件下标把漏掉的补回来；
- 用量与结果在运行结束时结算，跟有没有人在看**无关**。

缓冲区有上限（老事件滚动丢弃），丢掉的部分用一条 ``truncated`` 事件告知订阅者，
这样下标永远对得上，不会串位。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

Event = dict[str, Any]
#: 造一个事件流出来（通常是 ``run_stream(...)`` 的结果）
StreamFactory = Callable[[], Iterator[Event]]
FinishHook = Callable[["RunRecord"], None]

#: 跑完的运行保留多久（秒），到点回收，避免内存无限涨
DEFAULT_TTL_SECONDS = 1800.0
#: 同时最多保留多少个运行
DEFAULT_MAX_RUNS = 100
#: 单个运行最多缓冲多少条事件
DEFAULT_EVENT_LIMIT = 5000


@dataclass
class RunRecord:
    """一次任务运行的完整状态。"""

    id: str
    agent: str
    task: str
    session_id: str | None = None
    account_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    events: list[Event] = field(default_factory=list)
    #: ``events[0]`` 在整个事件序列里的绝对下标（老事件被丢掉后会 > 0）
    base_index: int = 0
    dropped_events: int = 0
    finished: bool = False
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    condition: threading.Condition = field(default_factory=threading.Condition, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)

    @property
    def total_events(self) -> int:
        """到目前为止一共产生过多少条事件（含被丢掉的）。"""
        return self.base_index + len(self.events)

    def answer_event(self) -> Event | None:
        """取最终答案事件（没有就跑挂了或还没结束）。"""
        for event in reversed(self.events):
            if event.get("type") == "answer":
                return event
        return None

    def summary(self) -> dict[str, Any]:
        """给接口用的状态摘要（不含事件正文）。"""
        with self.condition:
            ended = self.finished_at or time.monotonic()
            return {
                "id": self.id,
                "agent": self.agent,
                "task": self.task,
                "session_id": self.session_id,
                "status": "done" if self.finished else "running",
                "created_at": self.created_at.isoformat(timespec="seconds"),
                "event_count": self.total_events,
                "dropped_events": self.dropped_events,
                "duration_ms": round((ended - self.started_at) * 1000, 1),
            }


class RunRegistry:
    """运行注册表。线程安全，一个进程一份。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_runs: int = DEFAULT_MAX_RUNS,
        event_limit: int = DEFAULT_EVENT_LIMIT,
    ) -> None:
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self.max_runs = max(1, int(max_runs))
        self.event_limit = max(1, int(event_limit))
        self._runs: dict[str, RunRecord] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 启动

    def start(
        self,
        make_stream: StreamFactory,
        *,
        agent: str,
        task: str,
        session_id: str | None = None,
        account_id: str | None = None,
        on_finish: FinishHook | None = None,
    ) -> RunRecord:
        """登记一次运行并在后台线程里跑起来，立刻返回记录。

        注意是**后台线程**：HTTP 连接只是订阅者，断了也不影响这次运行。
        """
        record = RunRecord(
            id=uuid.uuid4().hex[:12],
            agent=agent,
            task=task,
            session_id=session_id,
            account_id=account_id,
        )
        with self._lock:
            self._runs[record.id] = record
            # 先放进去再回收，这样容量上限是"最多这么多"，而不是"下一个人进来之前这么多"
            self._prune_locked()

        def worker() -> None:
            try:
                for event in make_stream():
                    self._publish(record, event)
            except Exception as exc:  # noqa: BLE001 - 兜底，别让线程静默死掉
                self._publish(
                    record, {"type": "error", "data": {"message": f"未预期的错误：{exc}"}}
                )
            finally:
                self._finish(record, on_finish)

        thread = threading.Thread(target=worker, name=f"agentcode-run-{record.id}", daemon=True)
        record.thread = thread
        thread.start()
        return record

    # ------------------------------------------------------------------ 查询

    def get(self, run_id: str | None) -> RunRecord | None:
        """按 id 取运行记录。"""
        with self._lock:
            return self._runs.get(str(run_id or ""))

    def list(
        self,
        *,
        session_id: str | None = None,
        account_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """列出运行摘要，最近的排前面。"""
        with self._lock:
            runs = [
                record
                for record in self._runs.values()
                if (session_id is None or record.session_id == session_id)
                and (account_id is None or record.account_id == account_id)
            ]
        runs.sort(key=lambda record: record.created_at, reverse=True)
        return [record.summary() for record in runs[: max(1, int(limit))]]

    def running_for_session(self, session_id: str | None) -> RunRecord | None:
        """这个会话当前有没有还没跑完的运行（页面刷新后用来自动接上）。"""
        if not session_id:
            return None
        with self._lock:
            candidates = [
                record
                for record in self._runs.values()
                if record.session_id == session_id and not record.finished
            ]
        if not candidates:
            return None
        return max(candidates, key=lambda record: record.created_at)

    # ------------------------------------------------------------------ 订阅

    def subscribe(self, record: RunRecord, from_index: int = 0) -> Iterator[Event]:
        """从 ``from_index`` 开始产出事件，一直追到运行结束。

        ``from_index`` 是**绝对事件下标**：订阅者只要记住自己收到了多少条，
        重连时把它传回来即可。老事件被丢掉时会先收到一条 ``truncated``。
        """
        with record.condition:
            skipped = max(0, record.base_index - max(0, int(from_index)))
            cursor = max(max(0, int(from_index)), record.base_index)
        if skipped:
            # 告诉订阅者"你漏了几条、从现在这个下标接着看"，它才能对齐计数
            yield {"type": "truncated", "data": {"skipped": skipped, "from": record.base_index}}

        while True:
            with record.condition:
                while cursor >= record.total_events and not record.finished:
                    # 带超时地等：即使 notify 漏了也不会永久挂住
                    record.condition.wait(timeout=1.0)
                offset = cursor - record.base_index
                batch = list(record.events[offset:]) if 0 <= offset < len(record.events) else []
                cursor += len(batch)
                done = record.finished and cursor >= record.total_events
            for event in batch:
                yield event
            if done:
                return

    # ------------------------------------------------------------------ 内部

    def _publish(self, record: RunRecord, event: Event) -> None:
        """把事件放进缓冲区并叫醒订阅者。"""
        with record.condition:
            record.events.append(event)
            overflow = len(record.events) - self.event_limit
            if overflow > 0:
                del record.events[:overflow]
                record.base_index += overflow
                record.dropped_events += overflow
            record.condition.notify_all()

    def _finish(self, record: RunRecord, on_finish: FinishHook | None) -> None:
        """标记结束，并跑一次收尾钩子（结算用量就挂在这里）。"""
        with record.condition:
            record.finished = True
            record.finished_at = time.monotonic()
            record.condition.notify_all()
        if on_finish is not None:
            try:
                on_finish(record)
            except Exception:  # noqa: BLE001 - 收尾失败不能影响已经跑完的任务
                pass

    def _prune_locked(self) -> None:
        """回收过期与超量的运行记录（调用方需持有 ``_lock``）。"""
        now = time.monotonic()
        expired = [
            run_id
            for run_id, record in self._runs.items()
            if record.finished
            and record.finished_at is not None
            and now - record.finished_at > self.ttl_seconds
        ]
        for run_id in expired:
            self._runs.pop(run_id, None)

        if len(self._runs) <= self.max_runs:
            return
        # 还超量就丢最老的**已结束**运行；正在跑的一律不动
        finished = sorted(
            (record for record in self._runs.values() if record.finished),
            key=lambda record: record.created_at,
        )
        for record in finished:
            if len(self._runs) <= self.max_runs:
                break
            self._runs.pop(record.id, None)
