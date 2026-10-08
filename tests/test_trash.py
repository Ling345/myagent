"""回收站（软删除）。

重点不是"文件还在磁盘上"，而是**用户按错了还能回来**：删掉的会话连对话内容一起
恢复、清空的代码目录连文件一起恢复，而且回收站里的东西不能被当成正常工作目录
（agent 看不到它，列表里也不该混进来）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from agentcode.accounts import AccountStore
from agentcode.config import Settings
from agentcode.lifecycle import code_root_for, purge_code
from agentcode.trash import (
    TrashError,
    account_trash,
    empty_trash,
    purge_entry,
    restore_entry,
    sweep_trash,
)
from agentcode.web.sessions import SessionStore


def _settings(tmp_path: Path, *, trash_days: int = 30, audit_days: int = 180) -> Settings:
    return Settings(
        model="m",
        api_key="sk-x",
        base_url="https://x.invalid",
        db_path=str(tmp_path / "data" / "agentcode.db"),
        web_session_dir=str(tmp_path / "data" / "sessions"),
        code_root=str(tmp_path / "data" / "sandbox"),
        trash_days=trash_days,
        audit_days=audit_days,
    )


def _seed_code(settings: Settings, account_id: str, name: str = "calc.py") -> Path:
    root = code_root_for(settings.code_root, account_id)
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text("VALUE = 1\n", encoding="utf-8")
    return root


def _age(path: Path, hours: float) -> None:
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))


# ---------------------------------------------------------------- 会话


def test_deleting_a_session_moves_it_to_the_trash(tmp_path):
    settings = _settings(tmp_path)
    store = SessionStore(settings.web_session_dir)
    session = store.create(name="北京游")
    store.append_message(session.id, "user", "北京怎么玩")

    assert store.delete(session.id) is True

    # 正常列表里没有了，但回收站里在
    assert store.list() == []
    trashed = account_trash(settings, "")["sessions"]
    assert [item.name for item in trashed] == ["北京游"]
    assert trashed[0].deleted_at
    assert trashed[0].files == 1


def test_restoring_a_session_brings_the_conversation_back(tmp_path):
    settings = _settings(tmp_path)
    store = SessionStore(settings.web_session_dir)
    session = store.create(name="北京游")
    store.append_message(session.id, "user", "北京怎么玩")
    store.delete(session.id)
    entry = account_trash(settings, "")["sessions"][0]

    report = restore_entry(settings, "", "session", entry.entry)

    assert report["restored"] is True
    assert account_trash(settings, "")["sessions"] == []
    # 换一个全新的仓库（相当于重启服务）也能读到，而且对话内容在
    fresh = SessionStore(settings.web_session_dir)
    names = [item["name"] for item in fresh.list()]
    assert names == ["北京游"]
    assert fresh.messages(fresh.list()[0]["id"])[0]["content"] == "北京怎么玩"


def test_purging_a_session_removes_it_for_good(tmp_path):
    settings = _settings(tmp_path)
    store = SessionStore(settings.web_session_dir)
    session = store.create(name="不要了")
    store.delete(session.id)
    entry = account_trash(settings, "")["sessions"][0]

    assert purge_entry(settings, "", "session", entry.entry) is True
    assert account_trash(settings, "")["sessions"] == []
    assert list(Path(settings.web_session_dir).rglob("*.json")) == []


# ---------------------------------------------------------------- 代码目录


def test_purging_the_code_directory_moves_it_to_the_trash(tmp_path):
    settings = _settings(tmp_path)
    alice = AccountStore(settings.db_path).create("alice", "password123")
    _seed_code(settings, alice.id)

    moved = purge_code(settings, alice.id)

    assert moved == 1
    assert not code_root_for(settings.code_root, alice.id).exists()
    entries = account_trash(settings, alice.id)["code"]
    assert len(entries) == 1
    assert entries[0].files == 1
    # 回收站放在工作目录**外面**：agent 在代码目录里看不到它
    assert (Path(settings.code_root) / ".trash").is_dir()
    assert not (code_root_for(settings.code_root, alice.id) / ".trash").exists()


def test_restoring_the_code_directory_brings_the_files_back(tmp_path):
    settings = _settings(tmp_path)
    alice = AccountStore(settings.db_path).create("alice", "password123")
    _seed_code(settings, alice.id, "main.py")
    purge_code(settings, alice.id)
    entry = account_trash(settings, alice.id)["code"][0]

    restore_entry(settings, alice.id, "code", entry.entry)

    restored = code_root_for(settings.code_root, alice.id)
    assert (restored / "main.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert account_trash(settings, alice.id)["code"] == []


def test_restoring_code_refuses_to_overwrite_new_work(tmp_path):
    """清空之后又写了新东西，这时候恢复必须停下来问人，不能盖掉。"""
    settings = _settings(tmp_path)
    alice = AccountStore(settings.db_path).create("alice", "password123")
    _seed_code(settings, alice.id, "old.py")
    purge_code(settings, alice.id)
    entry = account_trash(settings, alice.id)["code"][0]
    _seed_code(settings, alice.id, "new.py")

    with pytest.raises(TrashError, match="已经有文件"):
        restore_entry(settings, alice.id, "code", entry.entry)
    assert (code_root_for(settings.code_root, alice.id) / "new.py").is_file()


# ---------------------------------------------------------------- 清理与安全


def test_empty_trash_clears_both_kinds(tmp_path):
    settings = _settings(tmp_path)
    alice = AccountStore(settings.db_path).create("alice", "password123")
    store = SessionStore(Path(settings.web_session_dir) / alice.id)
    session = store.create(name="B")
    store.delete(session.id)
    _seed_code(settings, alice.id)
    purge_code(settings, alice.id)

    report = empty_trash(settings, alice.id)

    assert report == {"sessions": 1, "code": 1}
    trash = account_trash(settings, alice.id)
    assert trash["sessions"] == []
    assert trash["code"] == []


def test_sweep_drops_entries_older_than_the_retention(tmp_path):
    settings = _settings(tmp_path)
    alice = AccountStore(settings.db_path).create("alice", "password123")
    store = SessionStore(Path(settings.web_session_dir) / alice.id)
    old = store.create(name="很久以前")
    store.append_message(old.id, "user", "老对话")
    recent = store.create(name="刚刚")
    store.append_message(recent.id, "user", "新对话")
    store.delete(old.id)
    store.delete(recent.id)
    trash_dir = Path(settings.web_session_dir) / alice.id / ".trash"
    _age(next(trash_dir.glob(f"{old.id}-*.json")), hours=24 * 40)

    report = sweep_trash(settings, [alice.id], days=30)

    assert report["sessions"] == 1
    remaining = account_trash(settings, alice.id)["sessions"]
    assert [item.name for item in remaining] == ["刚刚"]


def test_sweep_with_zero_days_keeps_everything(tmp_path):
    settings = _settings(tmp_path)
    alice = AccountStore(settings.db_path).create("alice", "password123")
    store = SessionStore(Path(settings.web_session_dir) / alice.id)
    session = store.create(name="A")
    store.delete(session.id)

    assert sweep_trash(settings, [alice.id], days=0) == {"sessions": 0, "code": 0}
    assert len(account_trash(settings, alice.id)["sessions"]) == 1


@pytest.mark.parametrize("entry", ["../evil", "/etc/passwd", "a/b.json", "..", ""])
def test_entry_names_cannot_escape_the_trash(tmp_path, entry):
    """回收站的名字来自请求，不能让它顺着路径爬出去。"""
    settings = _settings(tmp_path)
    with pytest.raises(TrashError):
        restore_entry(settings, "", "session", entry)
    with pytest.raises(TrashError):
        purge_entry(settings, "", "session", entry)


def test_unknown_kind_is_rejected(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(TrashError):
        restore_entry(settings, "", "whatever", "x")


# ---------------------------------------------------------------- 定期清理


def test_housekeeping_clears_the_trash_and_the_audit_log(tmp_path):
    """一个后台任务管两件事：过期的回收站条目和过期的审计日志。"""
    from agentcode.lifecycle import housekeeping_once

    settings = _settings(tmp_path, trash_days=30, audit_days=180)
    store = AccountStore(settings.db_path)
    alice = store.create("alice", "password123")
    sessions = SessionStore(Path(settings.web_session_dir) / alice.id)
    old = sessions.create(name="很久以前")
    sessions.append_message(old.id, "user", "老对话")
    sessions.delete(old.id)
    trash_file = next((Path(settings.web_session_dir) / alice.id / ".trash").glob("*.json"))
    _age(trash_file, hours=24 * 40)
    store.record_audit("login.ok", actor_name="alice", at="2020-01-01T00:00:00+00:00")

    report = housekeeping_once(store, settings)

    assert report["trash"]["sessions"] == 1
    assert report["audit"] == 1
    assert account_trash(settings, alice.id)["sessions"] == []
    assert store.audit_entries() == []


def test_housekeeping_with_zero_days_keeps_everything(tmp_path):
    from agentcode.lifecycle import housekeeping_once

    settings = _settings(tmp_path, trash_days=0, audit_days=0)
    store = AccountStore(settings.db_path)
    store.record_audit("login.ok", actor_name="alice", at="2020-01-01T00:00:00+00:00")

    report = housekeeping_once(store, settings)

    assert report["audit"] == 0
    assert report["trash"] == {"sessions": 0, "code": 0}
    assert len(store.audit_entries()) == 1


def test_housekeeping_loop_survives_a_failure(tmp_path, monkeypatch, capsys):
    from agentcode import lifecycle as lifecycle_module
    from agentcode.lifecycle import run_housekeeping_loop

    settings = _settings(tmp_path)
    store = AccountStore(settings.db_path)

    class _Stop(Exception):
        """把循环从 sleep 里拽出来。"""

    def _boom(_store, _settings):
        raise RuntimeError("磁盘满了")

    monkeypatch.setattr(lifecycle_module, "housekeeping_once", _boom)
    monkeypatch.setattr(lifecycle_module.time, "sleep", lambda _seconds: (_ for _ in ()).throw(_Stop))
    with pytest.raises(_Stop):
        run_housekeeping_loop(store, settings, interval_seconds=60)

    assert "磁盘满了" in capsys.readouterr().out
