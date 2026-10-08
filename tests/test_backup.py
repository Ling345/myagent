"""备份与恢复（离线）。

重点不是"打包成功了"，而是**备份里的数据库真的能打开、内容真的对得上**——
没演练过的备份等于没有备份。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tarfile
import time
from pathlib import Path

import pytest

from agentcode.accounts import AccountStore
from agentcode.backup import (
    BackupError,
    auto_backup,
    create_backup,
    read_manifest,
    restore_backup,
    run_backup_loop,
    verify_backup,
)
from agentcode.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        model="m",
        api_key="sk-x",
        base_url="https://x.invalid",
        db_path=str(tmp_path / "data" / "agentcode.db"),
        web_session_dir=str(tmp_path / "data" / "web-sessions"),
        code_root=str(tmp_path / "data" / "sandbox"),
        trace_dir=str(tmp_path / "data" / "traces"),
    )


def _seed(settings: Settings) -> AccountStore:
    """造一份有真实内容的安装。"""
    store = AccountStore(settings.db_path)
    alice = store.create("alice", "password123")
    store.record_usage(alice.id, 500, prompt_tokens=400, completion_tokens=100, run_id="r1")

    sessions = Path(settings.web_session_dir) / alice.id
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "chat-1.json").write_text('{"id": "chat-1"}', encoding="utf-8")

    code = Path(settings.code_root) / alice.id
    code.mkdir(parents=True, exist_ok=True)
    (code / "calc.py").write_text("VALUE = 1\n", encoding="utf-8")
    return store


# ---------------------------------------------------------------- 生成备份


def test_backup_contains_the_database_sessions_and_code(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)
    archive = create_backup(settings, tmp_path / "backup.tar.gz")

    with tarfile.open(archive, "r:gz") as tar:
        names = set(tar.getnames())
    assert "manifest.json" in names
    assert "agentcode.db" in names
    assert any(name.startswith("web-sessions/") for name in names)
    assert any(name.startswith("sandbox/") for name in names)


def test_manifest_counts_what_is_inside(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)
    manifest = create_backup(settings, tmp_path / "backup.tar.gz")

    assert manifest.accounts == 1
    assert manifest.ledger_rows == 1
    assert manifest.session_files == 1
    assert manifest.code_files == 1
    assert manifest.schema_version >= 1
    assert manifest.created_at


def test_backup_of_a_fresh_install_works(tmp_path):
    """还没人用过的安装也要能备份——不然"上线第一天先备份"就做不了。"""
    settings = _settings(tmp_path)
    AccountStore(settings.db_path)
    manifest = create_backup(settings, tmp_path / "backup.tar.gz")
    assert manifest.accounts == 0
    assert manifest.session_files == 0


def test_backup_refuses_to_overwrite_an_existing_file(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)
    target = tmp_path / "backup.tar.gz"
    target.write_text("已经是别的文件了", encoding="utf-8")
    with pytest.raises(BackupError, match="已经存在"):
        create_backup(settings, target)


def test_backup_creates_missing_directories(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)
    archive = create_backup(settings, tmp_path / "深层" / "目录" / "b.tar.gz")
    assert archive.is_file()


# ---------------------------------------------------------------- 校验（演练）


def test_verify_reports_the_same_counts(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)
    archive = create_backup(settings, tmp_path / "backup.tar.gz")
    verified = verify_backup(archive)
    assert verified.accounts == 1
    assert verified.session_files == 1


def test_verify_actually_opens_the_database(tmp_path):
    """这条是"演练"的核心：不是看文件在不在，而是真的把库打开读一遍。"""
    settings = _settings(tmp_path)
    store = _seed(settings)
    archive = create_backup(settings, tmp_path / "backup.tar.gz")

    result = verify_backup(archive, detailed=True)
    assert result["accounts"] == ["alice"]
    assert "alice" in result["account_names"]
    # 备份里的库跟原库在同一个 schema 版本上
    with sqlite3.connect(store.path) as conn:
        stored = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()[0]
    # schema_meta 里存的是文本，两边都按整数比
    assert result["schema_version"] == int(stored)


def test_verify_rejects_a_file_that_is_not_a_tarball(tmp_path):
    junk = tmp_path / "not-a-backup.tar.gz"
    junk.write_text("这是一段普通文本", encoding="utf-8")
    with pytest.raises(BackupError, match="不是有效的备份"):
        verify_backup(junk)


def test_verify_rejects_an_archive_without_a_manifest(tmp_path):
    archive = tmp_path / "no-manifest.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        payload = tmp_path / "whatever.txt"
        payload.write_text("hi", encoding="utf-8")
        tar.add(payload, arcname="whatever.txt")
    with pytest.raises(BackupError, match="manifest"):
        verify_backup(archive)


def test_verify_rejects_a_corrupt_database_inside(tmp_path):
    """库在包里存在但打不开——必须报错，不能"看起来有备份"就放行。"""
    settings = _settings(tmp_path)
    _seed(settings)
    good = create_backup(settings, tmp_path / "good.tar.gz")

    broken = tmp_path / "broken.tar.gz"
    with tarfile.open(broken, "w:gz") as tar:
        manifest = tmp_path / "manifest.json"
        manifest.write_text(
            json.dumps({"created_at": "2026-01-01T00:00:00+00:00", "accounts": 0}), encoding="utf-8"
        )
        tar.add(manifest, arcname="manifest.json")
        corrupt = tmp_path / "agentcode.db"
        corrupt.write_bytes(b"\x00" * 4096)  # 不是 SQLite 文件
        tar.add(corrupt, arcname="agentcode.db")

    with pytest.raises(BackupError, match="数据库"):
        verify_backup(broken)
    assert good.is_file()


def test_read_manifest_does_not_need_the_database(tmp_path):
    """列一列有哪些备份，不该依赖库能打开——库坏的那天，正是你要看它的时候。"""
    settings = _settings(tmp_path)
    _seed(settings)
    archive = create_backup(settings, tmp_path / "backup.tar.gz")

    manifest = read_manifest(archive)
    assert manifest.accounts == 1
    assert manifest.session_files == 1
    assert manifest.path == Path(archive)


# ---------------------------------------------------------------- 恢复


def test_restore_into_a_fresh_directory(tmp_path):
    settings = _settings(tmp_path)
    _seed(settings)
    archive = create_backup(settings, tmp_path / "backup.tar.gz")

    target = tmp_path / "restored"
    report = restore_backup(archive, target)
    assert report["accounts"] == 1
    assert report["files"] >= 3

    # 还原出来的库真的能用
    store = AccountStore(target / "agentcode.db")
    assert store.get("alice") is not None
    assert store.usage_today(store.get("alice").id)[0] == 500
    assert (target / "sandbox").is_dir()


def test_restore_refuses_a_non_empty_target(tmp_path):
    """就地覆盖现役数据是最危险的操作，必须显式要求。"""
    settings = _settings(tmp_path)
    _seed(settings)
    archive = create_backup(settings, tmp_path / "backup.tar.gz")

    target = tmp_path / "restored"
    target.mkdir()
    (target / "别动我.txt").write_text("重要数据", encoding="utf-8")

    with pytest.raises(BackupError, match="非空"):
        restore_backup(archive, target)
    assert (target / "别动我.txt").read_text(encoding="utf-8") == "重要数据"


def test_restore_with_force_moves_the_old_data_aside(tmp_path):
    """强制恢复也不能直接删——把旧数据改名留一份，出事还能回头。"""
    settings = _settings(tmp_path)
    _seed(settings)
    archive = create_backup(settings, tmp_path / "backup.tar.gz")

    target = tmp_path / "restored"
    target.mkdir()
    (target / "旧文件.txt").write_text("旧数据", encoding="utf-8")

    report = restore_backup(archive, target, force=True)
    assert report["moved_aside"]
    assert Path(report["moved_aside"]).is_dir()
    assert (Path(report["moved_aside"]) / "旧文件.txt").read_text(encoding="utf-8") == "旧数据"
    assert (target / "agentcode.db").is_file()


# ---------------------------------------------------------------- 启动时自动备份


def _settings_with_backup(tmp_path: Path, *, hours: float = 24.0, keep: int = 7) -> Settings:
    """开了自动备份的配置。"""
    settings = _settings(tmp_path)
    settings.backup_dir = str(tmp_path / "backups")
    settings.backup_interval_hours = hours
    settings.backup_keep = keep
    return settings


def _age(path: Path, hours: float) -> None:
    """把文件改成 ``hours`` 小时以前（用来假装"上次备份是很久之前"）。"""
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))


def test_auto_backup_is_off_by_default(tmp_path):
    """不配 AGENT_BACKUP_DIR 就不自动备份：往磁盘上写东西得由用户说了算。"""
    settings = _settings(tmp_path)
    _seed(settings)
    assert auto_backup(settings) is None
    assert not (tmp_path / "backups").exists()


def test_auto_backup_creates_one_then_skips_until_the_interval_passes(tmp_path):
    settings = _settings_with_backup(tmp_path)
    _seed(settings)

    first = auto_backup(settings)
    assert first is not None
    assert Path(first.path).is_file()

    # 刚备过，不该再备第二份
    assert auto_backup(settings) is None

    # 上一份已经是 30 小时前的了（间隔 24 小时），该再备一份
    _age(Path(first.path), hours=30)
    second = auto_backup(settings)
    assert second is not None
    assert len(list(Path(settings.backup_dir).glob("agentcode-*.tar.gz"))) == 2


def test_auto_backup_treats_an_unreadable_package_as_no_backup(tmp_path):
    """目录里躺着一个坏包不能算"已经备过了"——那种包救不了你。"""
    settings = _settings_with_backup(tmp_path)
    _seed(settings)
    directory = Path(settings.backup_dir)
    directory.mkdir(parents=True)
    (directory / "agentcode-坏掉的.tar.gz").write_text("这不是包", encoding="utf-8")

    manifest = auto_backup(settings)
    assert manifest is not None
    assert Path(manifest.path).is_file()


def test_auto_backup_keeps_only_the_newest_packages(tmp_path):
    """备份目录不能无限长大：只留最近的几份，而且只删自己生成的那种名字。"""
    settings = _settings_with_backup(tmp_path, keep=2)
    _seed(settings)

    made: list[Path] = []
    for _ in range(3):
        manifest = auto_backup(settings)
        assert manifest is not None
        _age(Path(manifest.path), hours=30)  # 让下一轮认为"该再备一份"
        made.append(Path(manifest.path))

    remaining = sorted(item.name for item in Path(settings.backup_dir).glob("agentcode-*.tar.gz"))
    assert len(remaining) == 2
    assert made[0].name not in remaining  # 最老的那份被清掉了


def test_auto_backup_never_touches_foreign_files(tmp_path):
    settings = _settings_with_backup(tmp_path, keep=1)
    _seed(settings)
    directory = Path(settings.backup_dir)
    directory.mkdir(parents=True)
    (directory / "手工放的.tar.gz").write_text("别人的包", encoding="utf-8")
    (directory / "重要.txt").write_text("别删我", encoding="utf-8")

    manifest = auto_backup(settings)
    assert manifest is not None
    _age(Path(manifest.path), hours=30)
    assert auto_backup(settings) is not None

    assert (directory / "手工放的.tar.gz").read_text(encoding="utf-8") == "别人的包"
    assert (directory / "重要.txt").read_text(encoding="utf-8") == "别删我"


def test_run_backup_loop_checks_once_then_sleeps(tmp_path, monkeypatch, capsys):
    from agentcode import backup as backup_module

    settings = _settings_with_backup(tmp_path)
    _seed(settings)
    calls: list[object] = []
    real = backup_module.auto_backup
    monkeypatch.setattr(backup_module, "auto_backup", lambda item: calls.append(item) or real(item))

    class _Stop(Exception):
        """把循环从 sleep 里拽出来。"""

    monkeypatch.setattr(backup_module.time, "sleep", lambda _seconds: _raise(_Stop))
    with pytest.raises(_Stop):
        run_backup_loop(settings)

    assert len(calls) == 1
    assert "自动备份" in capsys.readouterr().out


def test_run_backup_loop_keeps_going_when_a_backup_fails(tmp_path, monkeypatch, capsys):
    """备份失败不能让服务挂掉——它是后台的看门活儿，不是主流程。"""
    from agentcode import backup as backup_module

    settings = _settings_with_backup(tmp_path)

    class _Stop(Exception):
        """把循环从 sleep 里拽出来。"""

    def _boom(_settings):
        raise RuntimeError("磁盘满了")

    monkeypatch.setattr(backup_module, "auto_backup", _boom)
    monkeypatch.setattr(backup_module.time, "sleep", lambda _seconds: _raise(_Stop))
    with pytest.raises(_Stop):
        run_backup_loop(settings)

    assert "磁盘满了" in capsys.readouterr().out


def _raise(error: type[BaseException]) -> None:
    raise error
