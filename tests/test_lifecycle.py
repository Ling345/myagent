"""数据生命周期：导出、清理、注销（离线）。

这一步要回答的是用户的第三个问题：「我要删，怎么删」。
所以测试的重点不是"接口返回了 200"，而是**删完真的不在了**。
"""

from __future__ import annotations

import io
import os
import time
import zipfile
from pathlib import Path

import pytest

from agentcode.config import Settings
from agentcode.lifecycle import (
    account_data,
    delete_account_data,
    export_archive,
    purge_code,
    sweep_expired,
)


def _settings(tmp_path) -> Settings:
    """一份把三个目录都指到临时目录的配置。"""
    return Settings(
        model="m",
        api_key="sk-x",
        base_url="https://x.invalid",
        web_session_dir=str(tmp_path / "sessions"),
        code_root=str(tmp_path / "sandbox"),
        trace_dir=str(tmp_path / "traces"),
    )


def _seed(settings: Settings, account_id: str, *, session: str = "", code: str = "") -> None:
    """造点该账号的数据出来。"""
    sessions = Path(settings.web_session_dir) / account_id
    sessions.mkdir(parents=True, exist_ok=True)
    if session:
        (sessions / f"{session}.json").write_text('{"id": "%s"}' % session, encoding="utf-8")

    code_dir = Path(settings.code_root) / account_id
    code_dir.mkdir(parents=True, exist_ok=True)
    if code:
        (code_dir / code).write_text("VALUE = 1\n", encoding="utf-8")


# ---------------------------------------------------------------- 数据在哪


def test_account_data_points_at_per_account_directories(tmp_path):
    settings = _settings(tmp_path)
    data = account_data(settings, "alice-id")
    assert data.sessions == (tmp_path / "sessions" / "alice-id").resolve()
    assert data.code == (tmp_path / "sandbox" / "alice-id").resolve()


def test_account_data_refuses_an_empty_account_id(tmp_path):
    """没账号 id 就等于指向共享目录——真让它删就把所有人的东西删了。"""
    with pytest.raises(ValueError):
        account_data(_settings(tmp_path), "")


# ---------------------------------------------------------------- 导出


def test_export_contains_the_users_sessions_and_code(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, "alice-id", session="chat-1", code="calc.py")

    blob = export_archive(settings, "alice-id", account_name="alice")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = set(archive.namelist())
        assert "README.txt" in names
        assert "sessions/chat-1.json" in names
        assert "code/calc.py" in names
        assert b"VALUE = 1" in archive.read("code/calc.py")


def test_export_does_not_leak_other_accounts(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, "alice-id", session="alice-chat", code="alice.py")
    _seed(settings, "bob-id", session="bob-chat", code="bob.py")

    blob = export_archive(settings, "alice-id")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        joined = "\n".join(archive.namelist())
        assert "alice" in joined
        assert "bob" not in joined


def test_export_of_an_empty_account_still_works(tmp_path):
    """没数据不该报错，给个空包 + 说明。"""
    blob = export_archive(_settings(tmp_path), "nobody-id")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        assert archive.namelist() == ["README.txt"]


def test_export_readme_explains_what_is_inside(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, "alice-id", code="calc.py")
    with zipfile.ZipFile(io.BytesIO(export_archive(settings, "alice-id"))) as archive:
        readme = archive.read("README.txt").decode("utf-8")
    assert "sessions" in readme
    assert "code" in readme


# ---------------------------------------------------------------- 清理代码目录


def test_purge_code_removes_files_but_keeps_sessions(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, "alice-id", session="chat-1", code="calc.py")

    removed = purge_code(settings, "alice-id")
    assert removed == 1
    assert not (Path(settings.code_root) / "alice-id" / "calc.py").exists()
    assert (Path(settings.web_session_dir) / "alice-id" / "chat-1.json").is_file()


def test_purge_code_only_touches_that_account(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, "alice-id", code="alice.py")
    _seed(settings, "bob-id", code="bob.py")

    purge_code(settings, "alice-id")
    assert (Path(settings.code_root) / "bob-id" / "bob.py").is_file()


def test_purge_code_on_a_missing_directory_is_harmless(tmp_path):
    assert purge_code(_settings(tmp_path), "never-existed") == 0


# ---------------------------------------------------------------- 注销


def test_delete_account_data_removes_everything_on_disk(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, "alice-id", session="chat-1", code="calc.py")

    result = delete_account_data(settings, "alice-id")
    assert result["sessions"] == 1
    assert result["code"] == 1
    assert not (Path(settings.web_session_dir) / "alice-id").exists()
    assert not (Path(settings.code_root) / "alice-id").exists()


def test_delete_account_data_leaves_others_alone(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, "alice-id", session="a", code="a.py")
    _seed(settings, "bob-id", session="b", code="b.py")

    delete_account_data(settings, "alice-id")
    assert (Path(settings.web_session_dir) / "bob-id" / "b.json").is_file()
    assert (Path(settings.code_root) / "bob-id" / "b.py").is_file()


# ---------------------------------------------------------------- 过期清理


def _age(path: Path, days: float) -> None:
    """把文件的修改时间往前拨。"""
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


def test_sweep_removes_only_files_older_than_the_window(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings, "alice-id", session="old", code="old.py")
    _seed(settings, "alice-id", session="new", code="new.py")
    _age(Path(settings.web_session_dir) / "alice-id" / "old.json", 40)
    _age(Path(settings.code_root) / "alice-id" / "old.py", 40)

    result = sweep_expired(settings, ["alice-id"], days=30)
    assert result["removed"] == 2
    assert (Path(settings.web_session_dir) / "alice-id" / "new.json").is_file()
    assert (Path(settings.code_root) / "alice-id" / "new.py").is_file()
    assert not (Path(settings.web_session_dir) / "alice-id" / "old.json").exists()


def test_sweep_removes_empty_directories_too(tmp_path):
    """清完文件留一堆空目录，用户看着还是"东西还在"。"""
    settings = _settings(tmp_path)
    _seed(settings, "alice-id", session="old")
    _age(Path(settings.web_session_dir) / "alice-id" / "old.json", 40)

    sweep_expired(settings, ["alice-id"], days=30)
    assert not (Path(settings.web_session_dir) / "alice-id").exists()


def test_sweep_with_zero_days_is_a_no_op(tmp_path):
    """没配 AGENT_RETENTION_DAYS 就是"永不自动清理"，别把数据删了。"""
    settings = _settings(tmp_path)
    _seed(settings, "alice-id", session="ancient")
    _age(Path(settings.web_session_dir) / "alice-id" / "ancient.json", 3650)

    assert sweep_expired(settings, ["alice-id"], days=0)["removed"] == 0
    assert (Path(settings.web_session_dir) / "alice-id" / "ancient.json").is_file()
