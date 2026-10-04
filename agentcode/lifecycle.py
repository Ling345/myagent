"""数据生命周期：导出、清理、注销。

用户会问三个问题：「你们存了我什么、存多久、我要删怎么删」。这个模块负责第三个——
把"删除"真的做干净：库里的记录、会话目录、代码目录（含上传的文件）一个不留。

两条原则：

1. **只能删自己的**。所有函数都要求一个明确的 ``account_id``；空 id 一律拒绝，
   因为空 id 在别处表示"共享目录"（免登录本地模式），照着删就是把所有人的删了。
2. **导出让删除可用**。只能删不能导出的话，"删除"就成了单向门，用户不敢按。
"""

from __future__ import annotations

import io
import shutil
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agentcode.config import Settings
from agentcode.accounts import Account, AccountStore

#: 导出包里的说明文件
EXPORT_README = """AgentCode 数据导出
====================

这个包里是你在 AgentCode 上的全部数据：

  sessions/   你和智能体的对话记录（每条会话一个 JSON 文件）
  code/       你的代码工作目录：你上传的文件、智能体生成的文件与测试

账号本身的信息（用户名、套餐、用量账本）在运营方的账号库里，
不在这个包中；需要的话请联系运营方单独导出。

导出时间：{exported_at}
"""


def code_root_for(base: str | Path, account_id: str | None) -> Path:
    """某个账号的代码工作目录。

    会话目录一直按用户隔离，代码目录**曾经是全局共享的**——也就是说 alice 让
    agent 写进去的文件，bob 换个账号就能读到。所以这里也按账号分一层。

    免登录模式（``account_id`` 为空，本地自用）退回共享目录，行为不变。
    """
    root = Path(base).resolve()
    if not account_id:
        return root
    return (root / str(account_id)).resolve()


@dataclass(frozen=True)
class AccountData:
    """一个账号的数据散在哪几个目录里。"""

    account_id: str
    sessions: Path
    code: Path


def account_data(settings: Settings, account_id: str) -> AccountData:
    """定位某个账号的数据目录。

    ``account_id`` 为空时抛错，而不是退回共享目录——这个函数的结果是要拿去
    删东西的，退回共享目录等于误删所有人。
    """
    cleaned = str(account_id or "").strip()
    if not cleaned:
        raise ValueError("必须指定账号 id：不能对共享目录做导出或删除。")
    return AccountData(
        account_id=cleaned,
        sessions=(Path(settings.web_session_dir) / cleaned).resolve(),
        code=code_root_for(settings.code_root, cleaned),
    )


def _iter_files(root: Path) -> list[Path]:
    """目录下所有文件（不含目录本身）。"""
    if not root.is_dir():
        return []
    return [item for item in sorted(root.rglob("*")) if item.is_file()]


def export_archive(
    settings: Settings, account_id: str, *, account_name: str = "", now: datetime | None = None
) -> bytes:
    """把某个账号的会话与代码目录打包成 zip，返回字节。

    只装这个账号自己的目录，别的一概不碰。
    """
    data = account_data(settings, account_id)
    moment = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "README.txt",
            EXPORT_README.format(exported_at=moment)
            + (f"\n导出账号：{account_name}\n" if account_name else ""),
        )
        for label, root in (("sessions", data.sessions), ("code", data.code)):
            for path in _iter_files(root):
                archive.write(path, arcname=f"{label}/{path.relative_to(root).as_posix()}")
    return buffer.getvalue()


def purge_code(settings: Settings, account_id: str) -> int:
    """清空某个账号的代码目录，返回删掉的文件数。会话不动。"""
    data = account_data(settings, account_id)
    removed = len(_iter_files(data.code))
    shutil.rmtree(data.code, ignore_errors=True)
    return removed


def delete_account_data(settings: Settings, account_id: str) -> dict[str, int]:
    """删掉某个账号在磁盘上的全部数据：会话目录 + 代码目录。"""
    data = account_data(settings, account_id)
    result = {
        "sessions": len(_iter_files(data.sessions)),
        "code": len(_iter_files(data.code)),
    }
    shutil.rmtree(data.sessions, ignore_errors=True)
    shutil.rmtree(data.code, ignore_errors=True)
    return result


def delete_account(store: AccountStore, settings: Settings, account: Account) -> dict[str, object]:
    """注销：磁盘上的数据 + 账号库里的记录，一起删干净。

    顺序是先删库再删文件？不。**先删文件**：万一中途失败，留下的是"账号还在、
    数据没了"（用户能再登进来看一眼、再删一次），而不是"账号没了、数据还在"
    ——后者用户自己再也处理不了。
    """
    removed = delete_account_data(settings, account.id)
    return {
        **removed,
        "account_removed": store.delete_account(account.id),
    }


def sweep_expired(
    settings: Settings,
    account_ids: list[str],
    *,
    days: int,
    now: float | None = None,
) -> dict[str, int]:
    """清理超过 ``days`` 天没动过的会话与代码文件。

    ``days <= 0`` 表示**不自动清理**（这是默认值）——要删只能用户自己删。
    清完顺手把空目录收掉，不然用户看着还是"东西还在"。
    """
    if days <= 0:
        return {"removed": 0}

    cutoff = (now if now is not None else time.time()) - days * 86400
    removed = 0
    for account_id in account_ids:
        try:
            data = account_data(settings, account_id)
        except ValueError:
            continue
        for root in (data.sessions, data.code):
            for path in _iter_files(root):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                except OSError:
                    continue
            # 只收空目录，别把还有内容的目录带走
            if root.is_dir() and not any(root.iterdir()):
                try:
                    root.rmdir()
                except OSError:
                    pass
    return {"removed": removed}


def run_retention_loop(
    store: AccountStore, settings: Settings, interval_seconds: float = 86400.0
) -> None:
    """后台循环：定期清理过期数据。

    默认不启用（``AGENT_RETENTION_DAYS`` 为 0）——删不删由用户自己决定，
    这是产品默认值。配置了才跑，而且**先睡再扫**：启动那一次由 serve() 单独做，
    免得和启动流程抢时间。
    """
    while True:
        time.sleep(interval_seconds)
        if settings.retention_days <= 0:
            continue
        try:
            sweep_expired(
                settings, [account.id for account in store.list()], days=settings.retention_days
            )
        except Exception:  # noqa: BLE001 - 清理线程不能把服务带下去
            continue
