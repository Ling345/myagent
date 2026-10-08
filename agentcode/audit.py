"""操作审计：谁、什么时候、对谁、做了什么，结果如何。

为什么要有这个：用户投诉"我明明没删过那个会话"、或者账号突然被停用，
没有日志就只能靠猜。审计表是**事后说清楚**的唯一凭据，所以它有两个硬约束：

1. **绝不记密钥**。密码、令牌这类字段，不管调用方传什么，进门一律抹成 ``***``
   ——审计日志本身成了泄露源是经典事故。
2. **绝不阻断主流程**。审计是旁路：写不进去就打印警告，用户的操作照常完成。
   反过来（审计写失败就让删除失败）会让人以为"没删掉"，比丢一条日志更糟。

动作名用 ``域.动作`` 的点号风格（``login.ok`` / ``session.delete``），
这样 ``audit list --action-prefix login.`` 就能一次看全所有登录尝试。
"""

from __future__ import annotations

import time
from typing import Any

from agentcode.accounts import Account, AccountStore
from agentcode.config import Settings

#: 登录：成功 / 失败（失败不区分"账号不存在"和"密码错"，防账号枚举）
LOGIN_OK = "login.ok"
LOGIN_FAIL = "login.fail"
#: 会话：删除（进回收站）/ 改名 / 清空
SESSION_DELETE = "session.delete"
SESSION_RENAME = "session.rename"
SESSION_RESET = "session.reset"
#: 数据：导出 / 清空代码目录 / 注销账号
DATA_EXPORT = "data.export"
CODE_PURGE = "code.purge"
ACCOUNT_DELETE = "account.delete"
#: 回收站
TRASH_RESTORE = "trash.restore"
TRASH_PURGE = "trash.purge"
#: 运营动作（命令行）
ACCOUNT_CREATE = "account.create"
ACCOUNT_DISABLE = "account.disable"
ACCOUNT_ENABLE = "account.enable"
ACCOUNT_LIMIT = "account.limit"
ACCOUNT_PASSWD = "account.passwd"
ACCOUNT_PLAN = "account.plan"
ACCOUNT_EMAIL = "account.email"
BILLING_ORDER = "billing.order"
BILLING_CONFIRM = "billing.confirm"
#: 运维动作
BACKUP_CREATE = "backup.create"
BACKUP_RESTORE = "backup.restore"

#: 详情里出现这些键就抹掉——审计日志不能成为泄露源
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "密码",
        "password_hash",
        "token",
        "secret",
        "api_key",
        "api_keys",
        "authorization",
        "cookie",
    }
)
#: 抹掉之后的占位
REDACTED = "***"


def scrub_detail(value: Any) -> Any:
    """递归抹掉详情里的敏感字段（密码、令牌……）。

    只按**键名**判断，不看值——值可能是任何东西，猜不得。
    """
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if str(key).strip().lower() in SENSITIVE_KEYS:
                cleaned[key] = REDACTED
            else:
                cleaned[key] = scrub_detail(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [scrub_detail(item) for item in value]
    return value


class AuditLog:
    """审计门面：记一条、查一批、按保留期清理。"""

    def __init__(self, store: AccountStore) -> None:
        self.store = store

    def record(
        self,
        action: str,
        *,
        account: Account | None = None,
        actor_name: str = "",
        target: str = "",
        result: str = "ok",
        detail: Any = None,
        ip: str = "",
    ) -> None:
        """写一条审计记录。**任何异常都只打印警告**——旁路不能挡主流程。"""
        try:
            self.store.record_audit(
                action,
                actor_id=getattr(account, "id", "") or "",
                actor_name=getattr(account, "name", "") or actor_name,
                target=target,
                result=result,
                detail=scrub_detail(detail) if detail is not None else None,
                ip=ip,
            )
        except Exception as exc:  # noqa: BLE001 - 审计失败不能影响用户的操作
            print(f"警告：审计日志写入失败（{exc}）。操作本身已经完成。")

    def recent(
        self,
        *,
        limit: int = 50,
        actor_name: str | None = None,
        action: str | None = None,
        action_prefix: str | None = None,
    ) -> list[dict[str, object]]:
        """最近的审计记录，新的在前。"""
        return self.store.audit_entries(
            limit=limit, actor_name=actor_name, action=action, action_prefix=action_prefix
        )

    def sweep(self, settings: Settings) -> int:
        """按 ``AGENT_AUDIT_DAYS`` 清理过期记录（0 = 永久保留）。"""
        return self.store.prune_audit(int(getattr(settings, "audit_days", 0) or 0))


def sweep_audit(store: AccountStore, settings: Settings) -> int:
    """按配置清理审计日志，返回删掉的条数。"""
    return AuditLog(store).sweep(settings)


def run_audit_loop(
    store: AccountStore, settings: Settings, *, interval_seconds: float = 86400.0
) -> None:
    """后台循环：定期清理过期审计记录。

    和留存清理同一个套路：**先做一次再睡**（启动那一刻正是最该清的时候），
    出错只打印——后台的看门活儿不能把服务带下去。
    """
    while True:
        try:
            removed = sweep_audit(store, settings)
            if removed:
                print(f"审计日志：清理掉 {removed} 条超过 {settings.audit_days} 天的记录")
        except Exception as exc:  # noqa: BLE001 - 后台任务不能把服务带下去
            print(f"审计日志清理失败（服务继续跑）：{exc}")
        time.sleep(max(60.0, interval_seconds))
