"""命令行备份 / 演练 / 恢复 / 列清单。"""

from __future__ import annotations

import json
from pathlib import Path

from agentcode.accounts import AccountStore
from agentcode.cli import main


def _install(tmp_path: Path, monkeypatch) -> Path:
    """造一份隔离的安装：库、会话、代码目录全在临时目录里。"""
    data = tmp_path / "data"
    monkeypatch.setenv("AGENT_DB_PATH", str(data / "agentcode.db"))
    monkeypatch.setenv("AGENT_WEB_SESSION_DIR", str(data / "web-sessions"))
    monkeypatch.setenv("AGENT_CODE_ROOT", str(data / "sandbox"))

    store = AccountStore(data / "agentcode.db")
    alice = store.create("alice", "password123")
    store.record_usage(alice.id, 500, prompt_tokens=400, completion_tokens=100, run_id="r1")

    sessions = data / "web-sessions" / alice.id
    sessions.mkdir(parents=True)
    (sessions / "chat-1.json").write_text("{}", encoding="utf-8")
    code = data / "sandbox" / alice.id
    code.mkdir(parents=True)
    (code / "calc.py").write_text("VALUE = 1\n", encoding="utf-8")
    return data


def _make_backup(tmp_path: Path, monkeypatch, capsys, name: str = "备份.tar.gz") -> Path:
    """先造数据再备一份，返回包路径（顺手把生成那次的输出清掉）。"""
    _install(tmp_path, monkeypatch)
    archive = tmp_path / name
    assert main(["backup", "create", "--out", str(archive)]) == 0
    capsys.readouterr()
    return archive


# ---------------------------------------------------------------- create


def test_cli_backup_create_then_verify(tmp_path, monkeypatch, capsys):
    data = _install(tmp_path, monkeypatch)
    archive = tmp_path / "备份.tar.gz"

    assert main(["backup", "create", "--out", str(archive)]) == 0
    output = capsys.readouterr().out
    assert archive.is_file()
    assert "备份完成" in output
    assert "账号 1 个" in output
    # 生成之后要提醒演练——"备完就不管了"是这类功能最常见的失败方式
    assert "backup verify" in output

    assert main(["backup", "verify", str(archive)]) == 0
    output = capsys.readouterr().out
    assert "alice" in output
    assert "schema v" in output


def test_cli_backup_create_json_output(tmp_path, monkeypatch, capsys):
    _install(tmp_path, monkeypatch)
    archive = tmp_path / "备份.tar.gz"
    assert main(["backup", "create", "--out", str(archive), "--json"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output[output.index("{") :])
    assert payload["accounts"] == 1
    assert payload["session_files"] == 1
    assert payload["code_files"] == 1
    assert payload["path"].endswith("备份.tar.gz")


def test_cli_backup_create_defaults_to_a_timestamped_file(tmp_path, monkeypatch, capsys):
    """不写 --out 时放进 backups/，文件名带时间：连着备两次不会互相覆盖。"""
    monkeypatch.chdir(tmp_path)
    _install(tmp_path, monkeypatch)
    assert main(["backup", "create"]) == 0
    capsys.readouterr()
    made = sorted((tmp_path / "backups").glob("agentcode-*.tar.gz"))
    assert len(made) == 1
    assert made[0].stat().st_size > 0


def test_cli_backup_create_refuses_to_overwrite(tmp_path, monkeypatch, capsys):
    _install(tmp_path, monkeypatch)
    archive = tmp_path / "备份.tar.gz"
    archive.write_text("已有文件", encoding="utf-8")
    assert main(["backup", "create", "--out", str(archive)]) == 1
    assert "已经存在" in capsys.readouterr().out
    assert archive.read_text(encoding="utf-8") == "已有文件"


# ---------------------------------------------------------------- verify


def test_cli_backup_verify_rejects_a_file_that_is_not_a_package(tmp_path, capsys):
    junk = tmp_path / "x.tar.gz"
    junk.write_text("这不是备份", encoding="utf-8")
    assert main(["backup", "verify", str(junk)]) == 1
    assert "不是有效的备份" in capsys.readouterr().out


def test_cli_backup_verify_json_output(tmp_path, monkeypatch, capsys):
    archive = _make_backup(tmp_path, monkeypatch, capsys)
    assert main(["backup", "verify", str(archive), "--json"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output[output.index("{") :])
    assert payload["account_names"] == ["alice"]


# ---------------------------------------------------------------- restore


def test_cli_backup_restore_needs_confirmation_and_can_be_cancelled(
    tmp_path, monkeypatch, capsys
):
    archive = _make_backup(tmp_path, monkeypatch, capsys)
    target = tmp_path / "restored"
    monkeypatch.setattr("builtins.input", lambda _prompt="": "no")

    assert main(["backup", "restore", str(archive), "--to", str(target)]) == 1
    output = capsys.readouterr().out
    assert "已取消" in output
    assert not (target / "agentcode.db").exists()


def test_cli_backup_restore_into_a_fresh_directory(tmp_path, monkeypatch, capsys):
    archive = _make_backup(tmp_path, monkeypatch, capsys)
    target = tmp_path / "restored"

    assert main(["backup", "restore", str(archive), "--to", str(target), "--yes"]) == 0
    output = capsys.readouterr().out
    assert "恢复完成" in output

    store = AccountStore(target / "agentcode.db")
    account = store.get("alice")
    assert account is not None
    assert store.usage_today(account.id)[0] == 500
    assert (target / "sandbox").is_dir()


def test_cli_backup_restore_with_force_moves_the_old_data_aside(
    tmp_path, monkeypatch, capsys
):
    """就地覆盖现役数据是最危险的操作：旧数据必须留一份，不能删。"""
    archive = _make_backup(tmp_path, monkeypatch, capsys)
    target = tmp_path / "restored"
    target.mkdir()
    (target / "agentcode.db").write_text("现役数据", encoding="utf-8")

    assert main(["backup", "restore", str(archive), "--to", str(target), "--force", "--yes"]) == 0
    output = capsys.readouterr().out
    assert "旧数据留在" in output

    aside = [item for item in tmp_path.glob("restored.旧数据-*") if item.is_dir()]
    assert len(aside) == 1
    assert (aside[0] / "agentcode.db").read_text(encoding="utf-8") == "现役数据"
    assert AccountStore(target / "agentcode.db").get("alice") is not None


def test_cli_backup_restore_without_force_stops_on_a_non_empty_target(
    tmp_path, monkeypatch, capsys
):
    archive = _make_backup(tmp_path, monkeypatch, capsys)
    target = tmp_path / "restored"
    target.mkdir()
    (target / "别动.txt").write_text("重要", encoding="utf-8")

    assert main(["backup", "restore", str(archive), "--to", str(target), "--yes"]) == 1
    assert "非空" in capsys.readouterr().out
    assert (target / "别动.txt").read_text(encoding="utf-8") == "重要"


def test_cli_backup_restore_turns_a_filesystem_error_into_a_readable_message(
    tmp_path, monkeypatch, capsys
):
    """恢复写盘失败（磁盘满 / 文件被占用）要给一句人话，不能甩调用栈。"""
    from agentcode import backup as backup_module

    archive = _make_backup(tmp_path, monkeypatch, capsys)

    def _boom(_archive, _target, **_kwargs):
        raise OSError("文件被另一个进程占用")

    monkeypatch.setattr(backup_module, "restore_backup", _boom)
    target = tmp_path / "restored"
    assert main(["backup", "restore", str(archive), "--to", str(target), "--yes"]) == 1
    output = capsys.readouterr().out
    assert "错误" in output
    assert "被占用" in output


# ---------------------------------------------------------------- list


def test_cli_backup_list_shows_what_is_inside_each_package(tmp_path, monkeypatch, capsys):
    _install(tmp_path, monkeypatch)
    assert main(["backup", "create", "--out", str(tmp_path / "一.tar.gz")]) == 0
    assert main(["backup", "create", "--out", str(tmp_path / "二.tar.gz")]) == 0
    capsys.readouterr()

    assert main(["backup", "list", "--dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "一.tar.gz" in output
    assert "二.tar.gz" in output
    assert "账号 1" in output


def test_cli_backup_list_survives_a_broken_file(tmp_path, monkeypatch, capsys):
    """目录里混进一个坏文件不能把列清单搞崩——那正是你最想看看有哪些备份的时候。"""
    _install(tmp_path, monkeypatch)
    assert main(["backup", "create", "--out", str(tmp_path / "好.tar.gz")]) == 0
    (tmp_path / "坏.tar.gz").write_text("这不是包", encoding="utf-8")
    capsys.readouterr()

    assert main(["backup", "list", "--dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "好.tar.gz" in output
    assert "坏.tar.gz" in output
    assert "读不出来" in output


def test_cli_backup_list_on_an_empty_directory(tmp_path, capsys):
    assert main(["backup", "list", "--dir", str(tmp_path / "还没有")]) == 0
    assert "还没有备份" in capsys.readouterr().out
