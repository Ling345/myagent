"""服务通知：出了事，得能找到人。

为什么要有它：数据泄露、服务变更、套餐到期、账号被人猜密码——这些事用户
**有权知道**，而在这之前我们连他的邮箱都没有，只能等他自己上网页看到。

三条规矩：

1. **没配渠道不算发过**。没配 webhook 也没配 SMTP 时，通知会以
   ``queued`` 记进台账、写一行日志——绝不明说"已发送"。假的成功比不发更糟：
   你以为通知过用户，其实他什么都不知道。
2. **同一件事不反复轰**。每条通知带一个去重键（日期 / 到期时间 / 失败批次），
   发过的不会重发；**失败的除外**——服务端抖了一下不该让用户永远收不到。
3. **通知是旁路**。发送失败只记结果、不抛异常，绝不影响用户自己的操作。

渠道用标准库实现，零新依赖：webhook 是 POST 一段 JSON，SMTP 走 :mod:`smtplib`。
"""

from __future__ import annotations

import json
import smtplib
import time
import urllib.error
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from typing import Any, Callable

from agentcode.accounts import Account, AccountStore
from agentcode.config import Settings

#: 事件名（对外可见，也是台账里的分类）
EVENT_ACCOUNT_DELETED = "account.deleted"
EVENT_PLAN_CHANGED = "plan.changed"
EVENT_PLAN_EXPIRING = "plan.expiring"
EVENT_QUOTA_EXHAUSTED = "quota.exhausted"
EVENT_LOGIN_FAILURES = "security.login_failures"

#: 交付状态
STATUS_SENT = "sent"
STATUS_QUEUED = "queued"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

Poster = Callable[[str, dict[str, Any]], None]
Mailer = Callable[[str, str, str], None]


def compose(event: str, account: Account | None, detail: dict[str, Any] | None = None) -> tuple[str, str]:
    """把事件写成 (主题, 正文)。

    写给人看：说清楚**发生了什么**、**要不要做点什么**。
    不写"亲爱的用户您好"这类废话，也不夹带营销。
    """
    data = detail or {}
    name = getattr(account, "name", "") or "你的账号"

    if event == EVENT_ACCOUNT_DELETED:
        return (
            "你的 AgentCode 账号已注销",
            f"{name} 已注销，账号、会话、代码目录与回收站都已删除。\n"
            "这是最后一条消息。如果这不是你做的，请立刻按隐私政策里的联系方式找我们。",
        )
    if event == EVENT_PLAN_CHANGED:
        plan = data.get("plan", "（未知）")
        expires = data.get("expires_at") or "不过期"
        return (
            f"套餐已更新：{plan}",
            f"{name} 的套餐已更新为 {plan}，到期时间：{expires}。\n"
            "额度按新套餐的每日上限计算，每天 UTC 零点重置。",
        )
    if event == EVENT_PLAN_EXPIRING:
        days = data.get("days", 3)
        expires = data.get("expires_at") or "（未知）"
        return (
            f"套餐还有 {days} 天到期",
            f"{name} 的套餐将于 {expires} 到期，到期后会回落到免费额度。\n"
            "需要续期的话，在网页左下角「套餐与用量」里下单就行。",
        )
    if event == EVENT_QUOTA_EXHAUSTED:
        limit = data.get("limit", "?")
        used = data.get("used", "?")
        return (
            "今天的额度用完了",
            f"{name} 今天的额度已用完（{used}/{limit} token）。\n"
            "额度每天 UTC 零点重置；明天可以继续用，也可以升级套餐提高上限。",
        )
    if event == EVENT_LOGIN_FAILURES:
        count = data.get("count", "多次")
        minutes = data.get("minutes", 10)
        return (
            "有人多次尝试登录你的账号",
            f"{name} 在 {minutes} 分钟内连续 {count} 次登录失败。\n"
            "如果是你自己输错了，忽略这封就行；如果不是，建议尽快改密码。",
        )
    return (
        f"AgentCode 通知：{event}",
        json.dumps(data, ensure_ascii=False),
    )


def _default_poster(url: str, payload: dict[str, Any]) -> None:
    """webhook 通道：POST 一段 JSON。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10):
        pass


class Notifier:
    """决定"这条通知怎么发出去"，并把结果记进台账。"""

    def __init__(
        self,
        store: AccountStore,
        settings: Settings,
        *,
        poster: Poster | None = None,
        mailer: Mailer | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.webhook = str(getattr(settings, "notify_webhook", "") or "").strip()
        self.smtp_host = str(getattr(settings, "smtp_host", "") or "").strip()
        self.smtp_port = int(getattr(settings, "smtp_port", 0) or 0)
        self.smtp_user = str(getattr(settings, "smtp_user", "") or "").strip()
        self.smtp_password = getattr(settings, "smtp_password", None)
        self.smtp_from = str(getattr(settings, "smtp_from", "") or "").strip()
        # 测试注入用；默认走真实网络
        self._poster = poster or _default_poster
        self._mailer = mailer or self._smtp_send

    # ------------------------------------------------------------------ 主入口

    def notify(
        self,
        event: str,
        *,
        account: Account | None = None,
        detail: dict[str, Any] | None = None,
        dedupe_key: str | None = None,
        subject: str | None = None,
        body: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        """发一条通知，返回结果字典（``delivered`` / ``status`` / ``reason``）。

        **任何异常都在这里被吃掉**：通知失败不该让用户的操作失败。
        """
        account_id = getattr(account, "id", "") or ""
        account_name = getattr(account, "name", "") or ""
        address = str(email or getattr(account, "email", "") or "").strip()
        if not address and account_id:
            # 传进来的对象可能是"填邮箱之前"取的快照——以库里的为准。
            # 这里如果偷懒，最常见的结果就是"用户填了邮箱，通知却静默跳过"。
            fresh = self.store.get_by_id(account_id)
            if fresh is not None:
                address = str(fresh.email or "").strip()
                account_name = account_name or fresh.name
        if subject is None or body is None:
            default_subject, default_body = compose(event, account, detail)
            subject = subject or default_subject
            body = body or default_body

        if dedupe_key and self.store.notification_taken(account_id, event, dedupe_key):
            return {"delivered": False, "status": STATUS_SKIPPED, "reason": "重复：这条通知已经发过了"}

        if not address:
            self.store.record_notification(
                event=event,
                status=STATUS_SKIPPED,
                account_id=account_id,
                account_name=account_name,
                dedupe_key=dedupe_key,
                subject=subject,
                body=body,
            )
            return {"delivered": False, "status": STATUS_SKIPPED, "reason": "账号没填邮箱"}

        channel = self._channel()
        if channel is None:
            # 关键：不是"成功"，而是"记下来，等配了渠道再说"
            self.store.record_notification(
                event=event,
                status=STATUS_QUEUED,
                account_id=account_id,
                account_name=account_name,
                email=address,
                dedupe_key=dedupe_key,
                subject=subject,
                body=body,
            )
            print(f"通知未发出（没配任何渠道）：{event} → {address}（已记入台账）")
            return {
                "delivered": False,
                "status": STATUS_QUEUED,
                "reason": "没配通知渠道（AGENT_NOTIFY_WEBHOOK 或 AGENT_SMTP_HOST）",
            }

        try:
            if channel == "webhook":
                self._poster(
                    self.webhook,
                    {
                        "event": event,
                        "subject": subject,
                        "body": body,
                        "email": address,
                        "account": account_name,
                        "detail": detail or {},
                    },
                )
            else:
                self._mailer(address, subject, body)
        except Exception as exc:  # noqa: BLE001 - 通知失败不能影响用户的操作
            self.store.record_notification(
                event=event,
                status=STATUS_FAILED,
                account_id=account_id,
                account_name=account_name,
                email=address,
                dedupe_key=dedupe_key,
                subject=subject,
                body=body,
                channel=channel,
                error=str(exc),
            )
            print(f"通知发送失败（{channel}）：{event} → {address}：{exc}")
            return {"delivered": False, "status": STATUS_FAILED, "channel": channel, "error": str(exc)}

        self.store.record_notification(
            event=event,
            status=STATUS_SENT,
            account_id=account_id,
            account_name=account_name,
            email=address,
            dedupe_key=dedupe_key,
            subject=subject,
            body=body,
            channel=channel,
        )
        return {"delivered": True, "status": STATUS_SENT, "channel": channel}

    # ------------------------------------------------------------------ 渠道

    def _channel(self) -> str | None:
        """优先 webhook（不需要账号密码），其次 SMTP；都没配返回 None。"""
        if self.webhook:
            return "webhook"
        if self.smtp_host and self.smtp_from:
            return "smtp"
        return None

    def channel_name(self) -> str:
        """当前会走哪个渠道（给 CLI 显示）。"""
        return self._channel() or "（未配置，通知只会记台账）"

    def _smtp_send(self, to: str, subject: str, body: str) -> None:
        """真的发一封信出去。

        不用 ``EmailMessage`` 也能写对，但**中文主题必须做 RFC 2047 编码**，
        否则收件端看到的是乱码——所以主题走 :class:`Header`。
        """
        message = MIMEText(body, "plain", "utf-8")
        message["Subject"] = Header(subject, "utf-8")
        message["From"] = self.smtp_from
        message["To"] = to
        # 端口决定连法：465 是隐式 TLS，其余走明文、对方支持 STARTTLS 就升上去
        # ——这样 465 / 587 / 25 和本地测试用的裸 SMTP 都能跑
        if self.smtp_port == 465:
            client = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=15)
        else:
            client = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15)
        with client:
            if self.smtp_port != 465 and client.has_extn("starttls"):
                client.starttls()
            if self.smtp_user:
                client.login(self.smtp_user, self.smtp_password or "")
            client.send_message(message)

    # ------------------------------------------------------------------ 触发条件

    def quota_exhausted(self, account: Account, *, limit: int, used: int, day: str | None = None) -> bool:
        """当天额度用尽：一天只提醒一次。"""
        key = day or time.strftime("%Y-%m-%d", time.gmtime())
        result = self.notify(
            EVENT_QUOTA_EXHAUSTED,
            account=account,
            detail={"limit": limit, "used": used},
            dedupe_key=key,
        )
        return bool(result["delivered"])

    def check_login_failures(
        self,
        *,
        account: Account,
        threshold: int | None = None,
        minutes: int | None = None,
        now: float | None = None,
    ) -> bool:
        """短时间内连续登录失败就提醒本人（审计表就是事实来源，不另记一份）。"""
        limit = int(threshold or getattr(self.settings, "notify_login_failures", 5) or 5)
        window = int(minutes or getattr(self.settings, "notify_login_window_minutes", 10) or 10)
        moment = now if now is not None else time.time()
        since = moment - window * 60
        failures = [
            item
            for item in self.store.audit_entries(limit=200, action="login.fail", actor_name=account.name)
            if _timestamp(item["at"]) >= since
        ]
        if len(failures) < limit:
            return False
        # 去重键取"批次起点"：同一批失败只提醒一次，过一阵再有新批次就再提醒
        key = time.strftime("%Y-%m-%dT%H:%M", time.gmtime(since))
        result = self.notify(
            EVENT_LOGIN_FAILURES,
            account=account,
            detail={"count": len(failures), "minutes": window},
            dedupe_key=key,
        )
        return bool(result["delivered"])

    def check_expiring(self, *, days: int | None = None, now: float | None = None) -> int:
        """套餐快到期：每次到期时间只提醒一次，返回发出去的条数。"""
        from agentcode.plans import is_expired

        span = int(days or getattr(self.settings, "notify_expiring_days", 3) or 3)
        moment = now if now is not None else time.time()
        deadline = moment + span * 86400
        sent = 0
        for account in self.store.list():
            if not account.plan_expires_at or is_expired(account.plan_expires_at):
                continue
            expires = _timestamp(account.plan_expires_at)
            if expires > deadline:
                continue
            result = self.notify(
                EVENT_PLAN_EXPIRING,
                account=account,
                detail={"days": span, "expires_at": account.plan_expires_at},
                dedupe_key=account.plan_expires_at,  # 同一次到期只提醒一次
            )
            if result["delivered"]:
                sent += 1
        return sent


def _timestamp(value: str) -> float:
    """把 ISO 时间串转成 epoch 秒；看不懂就当成"很久以前"。"""
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()
