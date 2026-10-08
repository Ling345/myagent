"""回收站：删除先进回收站，误删还能回来。

为什么：用户删错一个会话、手滑清空代码目录，是**一定会发生**的事；直接
``rmtree`` 的代价是"找不回来了"，而客服能做的只有道歉。所以删除改成两步：
先搬到回收站（可恢复），到期或用户说"彻底删掉"才真的消失。

三条约定：

1. **回收站放在用户工作目录之外**。会话回收站是 ``<会话目录>/.trash/``，
   代码回收站是 ``<代码根目录>/.trash/``——不在 ``<代码根>/<账号>/`` 里面，
   否则 agent 列目录时会看到一堆自己不该碰的东西。
2. **回收站的名字来自请求，所以必须当外部输入处理**：只能是一个不含分隔符的
   简单名字，绝对不许出现 ``..`` 或绝对路径。
3. **注销账号不进回收站**。隐私政策里承诺的是真删除，回收站会让那句话变成谎话
   （注销走 :func:`agentcode.lifecycle.delete_account_data`，直接删干净）。
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agentcode.config import Settings

#: 回收站目录名（会话目录下 / 代码根目录下）
TRASH_DIRNAME = ".trash"
#: 条目名里的时间戳格式
STAMP_FORMAT = "%Y%m%d-%H%M%S"
#: 会话回收站条目的后缀
SESSION_SUFFIX = ".json"


class TrashError(RuntimeError):
    """回收站操作失败——消息里写清下一步怎么办。"""


@dataclass(frozen=True)
class TrashEntry:
    """回收站里的一条。"""

    kind: str  # "session" 或 "code"
    entry: str  # 回收站目录里的名字（恢复/彻底删都用它）
    name: str  # 给人看的名字
    deleted_at: str
    files: int
    bytes: int

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "entry": self.entry,
            "name": self.name,
            "deleted_at": self.deleted_at,
            "files": self.files,
            "bytes": self.bytes,
        }


# ------------------------------------------------------------------ 路径


def session_trash_dir(session_dir: str | Path) -> Path:
    """某个会话目录对应的回收站。"""
    return Path(session_dir) / TRASH_DIRNAME


def code_trash_dir(code_root: str | Path) -> Path:
    """代码根目录对应的回收站。"""
    return Path(code_root) / TRASH_DIRNAME


def account_code_dir(settings: Settings, account_id: str) -> Path:
    """某个账号的代码工作目录。"""
    return Path(settings.code_root) / str(account_id)


def trash_name(stem: str, moment: float | None = None) -> str:
    """造一个回收站条目名：``<原名>-<时间戳>``。"""
    stamp = datetime.fromtimestamp(
        moment if moment is not None else time.time(), tz=timezone.utc
    ).strftime(STAMP_FORMAT)
    return f"{stem}-{stamp}"


def _safe_entry(entry: str) -> str:
    """把条目名收成安全的单段名字；不合法就报错。

    这是**信任边界**：条目名来自网页请求，绝对不能让 ``../..`` 之类的东西
    爬出回收站。
    """
    cleaned = str(entry or "").strip()
    if not cleaned or len(cleaned) > 200:
        raise TrashError("回收站条目的名字不合法。")
    if "/" in cleaned or "\\" in cleaned or cleaned in {".", ".."} or ".." in cleaned:
        raise TrashError(f"回收站条目的名字不合法：{entry}。")
    if cleaned.startswith("."):
        raise TrashError(f"回收站条目的名字不合法：{entry}。")
    return cleaned


def _entry_time(path: Path) -> str:
    """条目是什么时候被删的：优先看文件里的记录，否则看修改时间。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        stamp = str(payload.get("deleted_at") or "")
        if stamp:
            return stamp
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    try:
        moment = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return ""
    return moment.isoformat(timespec="seconds")


def _tree_size(root: Path) -> tuple[int, int]:
    """(文件数, 字节数)。"""
    if root.is_file():
        try:
            return 1, root.stat().st_size
        except OSError:
            return 1, 0
    files = total = 0
    for item in root.rglob("*"):
        if not item.is_file():
            continue
        files += 1
        try:
            total += item.stat().st_size
        except OSError:
            continue
    return files, total


# ------------------------------------------------------------------ 查询


def account_trash(
    settings: Settings, account_id: str, *, session_dir: str | Path | None = None
) -> dict[str, list[TrashEntry]]:
    """某个账号的回收站：``{"sessions": [...], "code": [...]}``，新的在前。"""
    sessions = Path(session_dir) if session_dir else _session_dir(settings, account_id)
    return {
        "sessions": _list_sessions(sessions),
        "code": _list_code(Path(settings.code_root), str(account_id)),
    }


def _session_dir(settings: Settings, account_id: str) -> Path:
    """会话目录：有账号就分账号一层，免登录模式就是根目录。"""
    root = Path(settings.web_session_dir)
    cleaned = str(account_id or "").strip()
    return (root / cleaned) if cleaned else root


def _list_sessions(session_dir: Path) -> list[TrashEntry]:
    folder = session_trash_dir(session_dir)
    if not folder.is_dir():
        return []
    entries: list[TrashEntry] = []
    for path in folder.glob(f"*{SESSION_SUFFIX}"):
        if not path.is_file():
            continue
        files, size = _tree_size(path)
        entries.append(
            TrashEntry(
                kind="session",
                entry=path.name,
                name=_session_name(path),
                deleted_at=_entry_time(path),
                files=files,
                bytes=size,
            )
        )
    return sorted(entries, key=lambda item: item.deleted_at, reverse=True)


def _session_name(path: Path) -> str:
    """回收站里那个会话原来叫什么（读不出来就退回文件名）。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        name = str(payload.get("name") or "").strip()
        if name:
            return name
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return path.stem


def _list_code(code_root: Path, account_id: str) -> list[TrashEntry]:
    folder = code_trash_dir(code_root)
    if not folder.is_dir():
        return []
    prefix = f"{account_id}-"
    entries: list[TrashEntry] = []
    for path in folder.iterdir():
        if not path.is_dir() or not path.name.startswith(prefix):
            continue
        files, size = _tree_size(path)
        entries.append(
            TrashEntry(
                kind="code",
                entry=path.name,
                name=f"{account_id} 的代码目录",
                deleted_at=_entry_time(path),
                files=files,
                bytes=size,
            )
        )
    return sorted(entries, key=lambda item: item.deleted_at, reverse=True)


# ------------------------------------------------------------------ 恢复 / 彻底删


def restore_entry(
    settings: Settings,
    account_id: str,
    kind: str,
    entry: str,
    *,
    session_dir: str | Path | None = None,
) -> dict[str, object]:
    """把回收站里的一条放回去。"""
    name = _safe_entry(entry)
    if kind == "session":
        target = _restore_session(Path(session_dir) if session_dir else _session_dir(settings, account_id), name)
        return {"restored": True, "kind": kind, "path": str(target)}
    if kind == "code":
        target = _restore_code(Path(settings.code_root), str(account_id), name)
        return {"restored": True, "kind": kind, "path": str(target)}
    raise TrashError(f"不认识的回收站类型：{kind}。")


def _restore_session(session_dir: Path, entry: str) -> Path:
    source = session_trash_dir(session_dir) / entry
    if not source.is_file():
        raise TrashError(f"回收站里没有这一条：{entry}。")
    target = session_dir / entry
    if target.exists():
        raise TrashError(f"这个会话已经在了：{target.name}——先删掉现在的那份，或者手动处理。")
    try:
        session_dir.mkdir(parents=True, exist_ok=True)
        source.replace(target)
    except OSError as exc:
        raise TrashError(f"恢复失败：{exc}。") from exc
    return target


def _restore_code(code_root: Path, account_id: str, entry: str) -> Path:
    source = code_trash_dir(code_root) / entry
    if not source.is_dir():
        raise TrashError(f"回收站里没有这一条：{entry}。")
    if not entry.startswith(f"{account_id}-"):
        raise TrashError(f"这一条不属于当前账号：{entry}。")
    target = code_root / account_id
    if target.exists() and any(target.iterdir()):
        raise TrashError(
            f"代码工作目录已经有文件了（{target}）——直接恢复会盖掉现在的东西。"
            "先导出或清空，再来恢复。"
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.rmdir()  # 空的才收掉；非空上面已经拦住了
        source.replace(target)
    except OSError as exc:
        raise TrashError(f"恢复失败：{exc}。") from exc
    return target


def purge_entry(
    settings: Settings,
    account_id: str,
    kind: str,
    entry: str,
    *,
    session_dir: str | Path | None = None,
) -> bool:
    """彻底删掉回收站里的一条（这一步没有撤销）。"""
    name = _safe_entry(entry)
    if kind == "session":
        base = Path(session_dir) if session_dir else _session_dir(settings, account_id)
        return _remove(session_trash_dir(base) / name)
    if kind == "code":
        path = code_trash_dir(Path(settings.code_root)) / name
        if not path.is_dir() or not name.startswith(f"{account_id}-"):
            return False
        return _remove(path)
    raise TrashError(f"不认识的回收站类型：{kind}。")


def _remove(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def empty_trash(
    settings: Settings, account_id: str, *, session_dir: str | Path | None = None
) -> dict[str, int]:
    """清空某个账号的回收站，返回两类各删了几条。"""
    trash = account_trash(settings, account_id, session_dir=session_dir)
    removed = {"sessions": 0, "code": 0}
    for item in trash["sessions"]:
        if purge_entry(settings, account_id, "session", item.entry, session_dir=session_dir):
            removed["sessions"] += 1
    for item in trash["code"]:
        if purge_entry(settings, account_id, "code", item.entry):
            removed["code"] += 1
    return removed


def sweep_trash(
    settings: Settings,
    account_ids: list[str],
    *,
    days: int,
    now: float | None = None,
) -> dict[str, int]:
    """把超过 ``days`` 天的回收站条目真正删掉；``days <= 0`` 表示不自动清理。"""
    if days <= 0:
        return {"sessions": 0, "code": 0}
    cutoff = (now if now is not None else time.time()) - days * 86400
    removed = {"sessions": 0, "code": 0}
    for account_id in account_ids:
        cleaned = str(account_id or "").strip()
        if not cleaned:
            continue
        session_dir = _session_dir(settings, cleaned)
        for item in _list_sessions(session_dir):
            if _entry_mtime(session_trash_dir(session_dir) / item.entry) < cutoff:
                if purge_entry(settings, cleaned, "session", item.entry, session_dir=session_dir):
                    removed["sessions"] += 1
        for item in _list_code(Path(settings.code_root), cleaned):
            if _entry_mtime(code_trash_dir(Path(settings.code_root)) / item.entry) < cutoff:
                if purge_entry(settings, cleaned, "code", item.entry):
                    removed["code"] += 1
    return removed


def _entry_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return float("inf")  # 读不到就当它很新：宁可不删


def delete_trash_for_account(
    settings: Settings, account_id: str, *, session_dir: str | Path | None = None
) -> dict[str, int]:
    """注销账号时连回收站一起真删（回收站不能成为"删不掉"的后门）。"""
    return empty_trash(settings, account_id, session_dir=session_dir)
