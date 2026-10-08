"""备份与恢复：把"数据只有一份"变成"有两份，而且第二份真的打得开"。

用户的东西全都在一台机器的一块磁盘上：账号、用量账本、订单、会话记录、
代码工作区（含上传的文件）。没有第二份的那天，就是"硬盘坏了"的那天——
所以在做别的功能之前，先把备份和**恢复演练**做出来。

两条硬规则：

1. **备份不是拷文件**。SQLite 正在写入时，磁盘上的 ``.db`` 可能是写了一半的
   状态；直接 copy 出来的包平时看着没事，真要恢复的那天才发现坏了。所以这里
   走 sqlite3 的在线备份 API，拿到的是一致性快照。
2. **备份必须演练**。没打开过的备份等于没有备份——:func:`verify_backup` 会把包
   解开、真的把库打开读一遍账号，而不是看看文件名在不在。

包的结构（顶层）：

    manifest.json   这次备份的清单：什么时候备的、多少账号、多少文件、schema 版本
    agentcode.db    账号库的一致性快照
    web-sessions/   各用户的会话记录
    sandbox/        各用户的代码工作区

恢复是**往一个新目录里放**，不是原地覆盖现役数据；真要覆盖得显式 ``force=True``，
而且旧数据会被改名留一份，不删除——出事还能回头。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from agentcode.config import Settings
from agentcode.storage.migrations import current_version

#: 包里的清单文件名
MANIFEST_NAME = "manifest.json"
#: 包里账号库的文件名（固定这个名字，恢复端才不用猜）
DB_NAME = "agentcode.db"
#: 会话记录目录在包里的名字
SESSIONS_DIR = "web-sessions"
#: 代码工作区在包里的名字
CODE_DIR = "sandbox"
#: 包里允许出现的顶层名字；别的一律当成可疑内容拒收
ALLOWED_TOP_LEVEL = frozenset({MANIFEST_NAME, DB_NAME, SESSIONS_DIR, CODE_DIR})
#: 清单里参与"和实际内容对账"的字段
CHECKED_FIELDS = ("accounts", "ledger_rows", "session_files", "code_files", "schema_version")


class BackupError(RuntimeError):
    """备份、校验、恢复过程中出的错——消息里写清楚下一步该怎么办。"""


def _as_int(value: Any, default: int = 0) -> int:
    """把清单里的值转成整数；转不了就给默认值（比抛异常更能说清问题）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class BackupManifest:
    """一次备份的清单。

    它同时能当路径用（``tarfile.open(manifest)``、``manifest.is_file()``）：
    拿到备份的人真正要回答的两个问题是"它在哪"和"里面有什么"，让同一个对象
    回答这两件事，比返回 ``(路径, 清单)`` 两个值更不容易对错（对不上就是灾难）。
    """

    created_at: str
    accounts: int = 0
    ledger_rows: int = 0
    session_files: int = 0
    code_files: int = 0
    schema_version: int = 0
    #: 包文件在磁盘上的位置；从包里读出来的清单没有这个（它没有对应文件）
    path: Path | None = None

    #: 写进 manifest.json 的字段（path 是本机概念，不进包）
    JSON_FIELDS = (
        "created_at",
        "accounts",
        "ledger_rows",
        "session_files",
        "code_files",
        "schema_version",
    )

    def to_dict(self) -> dict[str, object]:
        """转成可以写进 ``manifest.json`` 的字典。"""
        return {name: getattr(self, name) for name in self.JSON_FIELDS}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, path: Path | None = None) -> "BackupManifest":
        """从 ``manifest.json`` 的内容还原清单。

        少写的键按 0 处理——老版本的包可能没有新加的计数字段，
        "没写"不等于"写错了"。
        """
        return cls(
            created_at=str(data.get("created_at") or ""),
            accounts=_as_int(data.get("accounts")),
            ledger_rows=_as_int(data.get("ledger_rows")),
            session_files=_as_int(data.get("session_files")),
            code_files=_as_int(data.get("code_files")),
            schema_version=_as_int(data.get("schema_version")),
            path=path,
        )

    def __fspath__(self) -> str:
        if self.path is None:
            raise TypeError("这份清单不对应磁盘上的文件（它是从包里读出来的）。")
        return str(self.path)

    def is_file(self) -> bool:
        """包还在不在磁盘上（清单本身是数据，只有它对应的文件才有"在不在"）。"""
        return self.path is not None and self.path.is_file()


# ---------------------------------------------------------------------- 生成备份


def create_backup(settings: Settings, target: str | Path) -> BackupManifest:
    """把整个安装打成一个 ``.tar.gz``，返回这次备份的清单。

    目标文件已存在就报错，绝不覆盖：磁盘上那份可能是你唯一的救命稻草，
    悄悄盖掉它比备份失败严重得多。目标目录不存在会自动建。
    """
    destination = Path(target)
    if destination.exists():
        raise BackupError(
            f"备份文件已经存在：{destination}。备份不覆盖已有文件——换个路径，"
            "或者先把旧的那份挪走。"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)

    source_db = Path(settings.db_path)
    if not source_db.is_file():
        raise BackupError(f"账号数据库不存在：{source_db}。先启动一次服务，让它建好库再备份。")

    with tempfile.TemporaryDirectory(prefix="agentcode-backup-") as work:
        work_dir = Path(work)
        # 先把库拍成一致性快照，再连快照一起打包
        _snapshot_database(source_db, work_dir / DB_NAME)
        version, accounts, ledger_rows, _names = _inspect_database(work_dir / DB_NAME)
        session_files = _copy_tree(Path(settings.web_session_dir), work_dir / SESSIONS_DIR)
        code_files = _copy_tree(Path(settings.code_root), work_dir / CODE_DIR)

        manifest = BackupManifest(
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            accounts=accounts,
            ledger_rows=ledger_rows,
            session_files=session_files,
            code_files=code_files,
            schema_version=version,
            path=destination,
        )
        (work_dir / MANIFEST_NAME).write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        _write_archive(work_dir, destination)
    return manifest


def _snapshot_database(source: Path, destination: Path) -> None:
    """用 SQLite 的在线备份 API 导出一致性快照。

    直接 copy 一个正在被写入的 ``.db``，可能拿到"事务做了一半"的库；``backup()``
    由 SQLite 自己在事务边界上搬数据，拿到的快照一定能打开。
    """
    try:
        source_conn = sqlite3.connect(str(source))
        try:
            target_conn = sqlite3.connect(str(destination))
            try:
                source_conn.backup(target_conn)
            finally:
                target_conn.close()
        finally:
            source_conn.close()
    except sqlite3.Error as exc:
        raise BackupError(f"读不了账号数据库（{source}）：{exc}。") from exc


def _copy_tree(source: Path, destination: Path) -> int:
    """整个目录拷进包里，返回拷进去的文件数。

    源目录不存在（还没人用过）不算错：空目录照样进包，这样恢复出来的结构和
    备份那天一模一样，而不是"少了一个目录"让人怀疑是不是没备全。
    """
    destination.mkdir(parents=True, exist_ok=True)
    if not source.is_dir():
        return 0
    count = 0
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            count += 1
    return count


def _write_archive(work_dir: Path, destination: Path) -> None:
    """把工作目录里的四样东西打成 ``.tar.gz``。

    写坏了就把半成品删掉：留着它，下一次备份会被"已经存在"挡住，
    而它其实根本不是个备份。
    """
    try:
        with tarfile.open(destination, "w:gz") as tar:
            for name in (MANIFEST_NAME, DB_NAME, SESSIONS_DIR, CODE_DIR):
                path = work_dir / name
                if path.exists():
                    tar.add(path, arcname=name)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------- 校验（演练）


def verify_backup(
    archive: str | Path, *, detailed: bool = False
) -> BackupManifest | dict[str, object]:
    """打开一个备份包，报告它里面**实际**有什么。

    这不是"看文件在不在"：会把包解开，真的把库打开读一遍（完整性检查 + 账号 +
    账本 + schema 版本），再拿清单和实际内容对账。对不上就报错——那说明包要么
    做的时候就不完整，要么在传输里坏了。

    ``detailed=True`` 时返回字典，多带账号名列表，给"恢复演练"的报告用。
    """
    path = _as_path(archive)
    if not path.is_file():
        raise BackupError(f"找不到备份文件：{path}")

    with _open_archive(path) as tar:
        declared, raw = _read_manifest(tar)
        session_files, code_files = _count_archived_files(tar)
        with tempfile.TemporaryDirectory(prefix="agentcode-verify-") as work:
            db_file = _extract_database(tar, Path(work))
            version, accounts, ledger_rows, names = _inspect_database(db_file)

    actual = BackupManifest(
        created_at=declared.created_at or _file_mtime(path),
        accounts=accounts,
        ledger_rows=ledger_rows,
        session_files=session_files,
        code_files=code_files,
        schema_version=version,
        path=path,
    )
    _check_against_manifest(raw, actual)
    if not detailed:
        return actual
    return {
        "path": str(path),
        "created_at": actual.created_at,
        "accounts": names,
        "account_names": names,
        "account_count": accounts,
        "ledger_rows": ledger_rows,
        "session_files": session_files,
        "code_files": code_files,
        "schema_version": version,
    }


def _file_mtime(path: Path) -> str:
    """包没写时间就用文件自己的修改时间顶上（总比空着强）。"""
    try:
        stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return ""
    return stamp.isoformat(timespec="seconds")


@contextmanager
def _open_archive(path: Path) -> Iterator[tarfile.TarFile]:
    """打开备份包；不是 gzip tar 就统一转成中文可读的错误。"""
    try:
        tar = tarfile.open(path, "r:gz")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise BackupError(f"{path} 不是有效的备份包（打不开的 gzip tar）：{exc}。") from exc
    try:
        yield tar
    finally:
        tar.close()


def _read_manifest(tar: tarfile.TarFile) -> tuple[BackupManifest, dict[str, Any]]:
    """读清单，同时把原始内容留着（对账时要知道它"真的写了哪几个键"）。"""
    try:
        member = tar.getmember(MANIFEST_NAME)
    except KeyError as exc:
        raise BackupError(f"这个包里没有 {MANIFEST_NAME}，不像是 AgentCode 的备份。") from exc
    handle = tar.extractfile(member)
    if handle is None:
        raise BackupError(f"包里的 {MANIFEST_NAME} 读不出来。")
    try:
        raw = json.loads(handle.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError(f"包里的 {MANIFEST_NAME} 不是合法的 JSON：{exc}。") from exc
    if not isinstance(raw, dict):
        raise BackupError(f"包里的 {MANIFEST_NAME} 不是一份清单（应该是 JSON 对象）。")
    return BackupManifest.from_dict(raw), raw


def _count_archived_files(tar: tarfile.TarFile) -> tuple[int, int]:
    """数一数包里有多少会话文件、多少代码文件（看成员列表就够，不用解包）。"""
    sessions = code = 0
    for member in tar.getmembers():
        if not member.isfile():
            continue
        if member.name.startswith(f"{SESSIONS_DIR}/"):
            sessions += 1
        elif member.name.startswith(f"{CODE_DIR}/"):
            code += 1
    return sessions, code


def _extract_database(tar: tarfile.TarFile, work_dir: Path) -> Path:
    """把库解到临时目录，等着被真打开一次。"""
    try:
        member = tar.getmember(DB_NAME)
    except KeyError as exc:
        raise BackupError(f"包里没有 {DB_NAME}（账号库），不能用来恢复。") from exc
    handle = tar.extractfile(member)
    if handle is None:
        raise BackupError(f"包里的 {DB_NAME} 读不出来。")
    destination = work_dir / DB_NAME
    with open(destination, "wb") as out:
        shutil.copyfileobj(handle, out)
    return destination


def _inspect_database(db_path: Path) -> tuple[int, int, int, list[str]]:
    """真的把库打开读一遍：schema 版本、账号数、账本行数、账号名。

    打开失败（不是 SQLite 文件、文件被截断、结构不对）一律转成 BackupError——
    "有备份但恢复不了"必须在演练时就被戳穿，不能等到真出事那天。
    """
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()
            if check is None or str(check[0]).lower() != "ok":
                raise BackupError(f"备份里的数据库没通过完整性检查：{check}。")
            version = current_version(conn)
            accounts = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            ledger_rows = conn.execute("SELECT COUNT(*) FROM usage_ledger").fetchone()[0]
            names = [
                str(row[0])
                for row in conn.execute("SELECT name FROM accounts ORDER BY created_at, name")
            ]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BackupError(f"备份里的数据库打不开：{exc}。这个包不能用来恢复。") from exc
    return int(version), int(accounts), int(ledger_rows), names


def _check_against_manifest(raw: Mapping[str, Any], actual: BackupManifest) -> None:
    """清单写的和包里实际的必须对得上。

    只比清单里**真的写了**的字段：老版本的包可能少几个键，
    少写不等于写错，不能拿默认值去冤枉它。
    """
    for field in CHECKED_FIELDS:
        if field not in raw:
            continue
        declared = _as_int(raw[field])
        real = getattr(actual, field)
        if declared != real:
            raise BackupError(
                f"包里的清单说 {field}={declared}，实际打开来是 {real}——"
                "内容和清单对不上，别拿它恢复。"
            )


# ---------------------------------------------------------------------- 恢复


def restore_backup(
    archive: str | Path, target: str | Path, *, force: bool = False
) -> dict[str, object]:
    """把备份解到一个目录里，返回恢复报告。

    **先演练再落盘**：包打不开、库读不了，目标目录一个字节都不会被动。
    目标目录非空时默认拒绝；``force=True`` 才继续，而且旧数据是改名留一份，
    不是删除——"以为恢复了，其实盖错了"是这里唯一不能犯的错。
    """
    path = _as_path(archive)
    manifest = verify_backup(path)
    destination = Path(target)
    if destination.exists() and not destination.is_dir():
        raise BackupError(f"目标路径已经是个文件了：{destination}。恢复要一个目录。")

    moved_aside: Path | None = None
    if destination.is_dir() and any(destination.iterdir()):
        if not force:
            raise BackupError(
                f"目标目录非空：{destination}。就地恢复会盖掉现役数据；确认要继续的话"
                "加 force（旧数据会改名留一份，不删除）。"
            )
        moved_aside = _move_aside(destination)
    destination.mkdir(parents=True, exist_ok=True)

    with _open_archive(path) as tar:
        files = _extract(tar, destination)
    return {
        "path": str(destination),
        "accounts": manifest.accounts,
        "files": files,
        "moved_aside": str(moved_aside) if moved_aside else None,
        "created_at": manifest.created_at,
    }


def _move_aside(target: Path) -> Path:
    """把现有目录改名留一份（放在同一个父目录下，紧跟时间戳）。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    candidate = target.parent / f"{target.name}.旧数据-{stamp}"
    suffix = 1
    while candidate.exists():
        suffix += 1
        candidate = target.parent / f"{target.name}.旧数据-{stamp}-{suffix}"
    target.rename(candidate)
    return candidate


def _extract(tar: tarfile.TarFile, destination: Path) -> int:
    """把包里的内容解到目标目录，返回解出来的文件数。

    只认约定的四个顶层名字，并且拒绝绝对路径、``..`` 和链接：备份包也是外部
    输入，解包就是信任边界，不能由着它往宿主的任意位置写。
    """
    files = 0
    for member in tar.getmembers():
        name = member.name
        if not _is_safe_member(name):
            raise BackupError(f"备份里有不该出现的内容：{name}。为安全起见停止恢复。")
        if member.issym() or member.islnk() or member.isdev():
            raise BackupError(f"备份里有链接或设备文件：{name}。为安全起见停止恢复。")
        target = destination / name
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not member.isfile():
            continue
        source = tar.extractfile(member)
        if source is None:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as out:
            shutil.copyfileobj(source, out)
        files += 1
    return files


def _is_safe_member(name: str) -> bool:
    """成员名必须是"约定目录下的相对路径"。"""
    cleaned = str(name or "").replace("\\", "/")
    if not cleaned or cleaned.startswith("/"):
        return False
    if ".." in cleaned.split("/"):
        return False
    return cleaned.split("/", 1)[0] in ALLOWED_TOP_LEVEL


def _as_path(value: str | Path) -> Path:
    """把"可以当路径用的东西"统一成 ``Path``（清单对象也走这里）。"""
    return Path(os.fspath(value))
