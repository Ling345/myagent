"""邮箱与通知（离线）。

重点不是"调了一次发送函数"，而是三件事：**没配渠道时不能假装发了**、
**同一件事不能反复轰用户**、**通知失败不能影响用户自己的操作**。
"""

from __future__ import annotations

import socket
import socketserver
import threading
from pathlib import Path

import pytest

from agentcode.accounts import AccountStore, normalize_email
from agentcode.config import Settings
from agentcode.core.errors import AgentCodeError
from agentcode.notify import (
    EVENT_ACCOUNT_DELETED,
    EVENT_LOGIN_FAILURES,
    EVENT_PLAN_CHANGED,
    EVENT_PLAN_EXPIRING,
    EVENT_QUOTA_EXHAUSTED,
    Notifier,
    compose,
)


def _store(tmp_path: Path) -> AccountStore:
    return AccountStore(tmp_path / "agentcode.db")


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "model": "m",
        "api_key": "sk-x",
        "base_url": "https://x.invalid",
        "db_path": str(tmp_path / "agentcode.db"),
    }
    values.update(overrides)
    return Settings(**values)


# ---------------------------------------------------------------- 邮箱格式


@pytest.mark.parametrize(
    "value,expected",
    [
        ("  Alice@Example.COM  ", "alice@example.com"),
        ("a.b+c@sub.example.cn", "a.b+c@sub.example.cn"),
        ("", ""),  # 清空是合法的
    ],
)
def test_normalize_email_accepts_and_cleans(value, expected):
    assert normalize_email(value) == expected


@pytest.mark.parametrize("value", ["不是邮箱", "a@b", "a b@c.com", "@example.com", "x@.com", "x" * 300 + "@a.com"])
def test_normalize_email_rejects_junk(value):
    with pytest.raises(AgentCodeError):
        normalize_email(value)


def test_email_is_stored_on_the_account(tmp_path):
    store = _store(tmp_path)
    account = store.create("alice", "password123")
    assert account.email is None

    assert store.set_email("alice", "Alice@Example.com") is True

    assert store.get("alice").email == "alice@example.com"
    # 改密码、换套餐都不该把邮箱弄丢
    store.set_password("alice", "password456")
    assert store.get("alice").email == "alice@example.com"


def test_email_can_be_cleared(tmp_path):
    store = _store(tmp_path)
    store.create("alice", "password123")
    store.set_email("alice", "a@b.com")
    assert store.set_email("alice", "") is True
    assert store.get("alice").email is None


def test_setting_an_email_for_an_unknown_account_reports_false(tmp_path):
    assert _store(tmp_path).set_email("查无此人", "a@b.com") is False


# ---------------------------------------------------------------- 内容


def test_templates_say_what_happened(tmp_path):
    store = _store(tmp_path)
    alice = store.create("alice", "password123")

    subject, body = compose(EVENT_QUOTA_EXHAUSTED, alice, {"limit": 50000, "used": 50000})
    assert "额度" in subject
    assert "alice" in body
    assert "50000" in body
    assert "明天" in body  # 得告诉用户什么时候恢复


# ---------------------------------------------------------------- 投递


def test_without_a_channel_it_records_instead_of_pretending(tmp_path):
    """没配渠道就"发送成功"是最糟的失败方式：你以为通知过用户，其实没有。"""
    store = _store(tmp_path)
    alice = store.create("alice", "password123")
    store.set_email("alice", "alice@example.com")
    notifier = Notifier(store, _settings(tmp_path))

    result = notifier.notify(EVENT_PLAN_CHANGED, account=alice, detail={"plan": "basic"})

    assert result["delivered"] is False
    assert result["status"] == "queued"
    assert "渠道" in result["reason"]
    rows = store.notifications()
    assert len(rows) == 1
    assert rows[0]["event"] == EVENT_PLAN_CHANGED
    assert rows[0]["status"] == "queued"
    assert rows[0]["email"] == "alice@example.com"


def test_webhook_channel_posts_the_notification(tmp_path):
    store = _store(tmp_path)
    alice = store.create("alice", "password123")
    store.set_email("alice", "alice@example.com")
    sent: list[tuple[str, dict]] = []
    notifier = Notifier(
        store,
        _settings(tmp_path, notify_webhook="https://hook.invalid/notify"),
        poster=lambda url, payload: sent.append((url, payload)),
    )

    result = notifier.notify(EVENT_PLAN_EXPIRING, account=alice, detail={"days": 3})

    assert result["delivered"] is True
    assert result["status"] == "sent"
    assert result["channel"] == "webhook"
    url, payload = sent[0]
    assert url == "https://hook.invalid/notify"
    assert payload["event"] == EVENT_PLAN_EXPIRING
    assert payload["email"] == "alice@example.com"
    assert "到期" in payload["subject"]


def test_smtp_channel_sends_mail(tmp_path):
    store = _store(tmp_path)
    alice = store.create("alice", "password123")
    store.set_email("alice", "alice@example.com")
    sent: list[tuple[str, str, str]] = []
    notifier = Notifier(
        store,
        _settings(
            tmp_path,
            smtp_host="smtp.invalid",
            smtp_port=2525,
            smtp_user="bot",
            smtp_password="secret",
            smtp_from="noreply@example.com",
        ),
        mailer=lambda to, subject, body: sent.append((to, subject, body)),
    )

    result = notifier.notify(EVENT_PLAN_CHANGED, account=alice, detail={"plan": "basic"})

    assert result["status"] == "sent"
    assert result["channel"] == "smtp"
    assert sent[0][0] == "alice@example.com"
    assert "套餐" in sent[0][1]


def test_channel_failure_is_recorded_and_never_raises(tmp_path, capsys):
    store = _store(tmp_path)
    alice = store.create("alice", "password123")
    store.set_email("alice", "alice@example.com")

    def _boom(_url, _payload):
        raise OSError("连不上")

    notifier = Notifier(
        store,
        _settings(tmp_path, notify_webhook="https://hook.invalid/notify"),
        poster=_boom,
    )
    result = notifier.notify(EVENT_PLAN_CHANGED, account=alice)

    assert result["delivered"] is False
    assert result["status"] == "failed"
    assert "连不上" in result["error"]
    assert store.notifications()[0]["status"] == "failed"


def test_account_without_an_email_is_skipped(tmp_path):
    store = _store(tmp_path)
    alice = store.create("alice", "password123")
    notifier = Notifier(store, _settings(tmp_path, notify_webhook="https://hook.invalid/n"))

    result = notifier.notify(EVENT_PLAN_CHANGED, account=alice)

    assert result["delivered"] is False
    assert result["status"] == "skipped"
    assert "邮箱" in result["reason"]


def test_the_same_event_is_not_sent_twice(tmp_path):
    """额度用尽这种事件一天只会发生一次；重复推送只会让用户屏蔽你的邮件。"""
    store = _store(tmp_path)
    alice = store.create("alice", "password123")
    store.set_email("alice", "alice@example.com")
    sent: list[dict] = []
    notifier = Notifier(
        store,
        _settings(tmp_path, notify_webhook="https://hook.invalid/notify"),
        poster=lambda _url, payload: sent.append(payload),
    )

    first = notifier.notify(EVENT_QUOTA_EXHAUSTED, account=alice, dedupe_key="2026-10-08")
    second = notifier.notify(EVENT_QUOTA_EXHAUSTED, account=alice, dedupe_key="2026-10-08")

    assert first["delivered"] is True
    assert second["delivered"] is False
    assert "重复" in second["reason"]
    assert len(sent) == 1
    assert len(store.notifications()) == 1


def test_a_different_day_is_a_different_notification(tmp_path):
    store = _store(tmp_path)
    alice = store.create("alice", "password123")
    store.set_email("alice", "alice@example.com")
    notifier = Notifier(
        store,
        _settings(tmp_path, notify_webhook="https://hook.invalid/notify"),
        poster=lambda _url, _payload: None,
    )

    notifier.notify(EVENT_QUOTA_EXHAUSTED, account=alice, dedupe_key="2026-10-08")
    assert notifier.notify(
        EVENT_QUOTA_EXHAUSTED, account=alice, dedupe_key="2026-10-09"
    )["delivered"] is True


# ---------------------------------------------------------------- 触发条件


def test_quota_exhaustion_is_notified_once_per_day(tmp_path):
    store = _store(tmp_path)
    alice = store.create("alice", "password123", daily_token_limit=10)
    store.set_email("alice", "alice@example.com")
    sent: list[dict] = []
    notifier = Notifier(
        store,
        _settings(tmp_path, notify_webhook="https://hook.invalid/notify"),
        poster=lambda _url, payload: sent.append(payload),
    )

    assert notifier.quota_exhausted(alice, limit=10, used=11) is True
    assert notifier.quota_exhausted(alice, limit=10, used=12) is False
    assert len(sent) == 1


def test_login_failure_burst_is_notified_once(tmp_path):
    """同一账号短时间连续失败：这可能是有人在猜密码，得告诉用户本人。"""
    store = _store(tmp_path)
    alice = store.create("alice", "password123")
    store.set_email("alice", "alice@example.com")
    for _ in range(3):
        store.record_audit("login.fail", actor_name="alice", result="denied")
    sent: list[dict] = []
    notifier = Notifier(
        store,
        _settings(tmp_path, notify_webhook="https://hook.invalid/notify"),
        poster=lambda _url, payload: sent.append(payload),
    )

    assert notifier.check_login_failures(account=alice, threshold=3) is True
    assert notifier.check_login_failures(account=alice, threshold=3) is False
    assert len(sent) == 1
    assert "登录" in sent[0]["subject"]


def test_expiring_plans_are_notified_once_per_expiry(tmp_path):
    from datetime import datetime, timedelta, timezone

    store = _store(tmp_path)
    alice = store.create("alice", "password123")
    store.set_email("alice", "alice@example.com")
    expires = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(timespec="seconds")
    store.apply_plan("alice", "basic", started_at="2026-10-01T00:00:00+00:00", expires_at=expires)
    sent: list[dict] = []
    notifier = Notifier(
        store,
        _settings(tmp_path, notify_webhook="https://hook.invalid/notify"),
        poster=lambda _url, payload: sent.append(payload),
    )

    assert notifier.check_expiring(days=3) == 1
    assert notifier.check_expiring(days=3) == 0  # 同一次到期只提醒一次
    assert len(sent) == 1


def test_housekeeping_sends_the_expiry_reminder(tmp_path, monkeypatch):
    """每天一次的杂活里带上"套餐快到期"提醒——不用另开循环。"""
    from datetime import datetime, timedelta, timezone

    from agentcode.lifecycle import housekeeping_once

    store = _store(tmp_path)
    alice = store.create("alice", "password123")
    store.set_email("alice", "alice@example.com")
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
    store.apply_plan("alice", "basic", started_at="2026-10-01T00:00:00+00:00", expires_at=expires)
    sent: list[dict] = []
    import agentcode.notify as notify_module

    monkeypatch.setattr(
        notify_module, "_default_poster", lambda _url, payload: sent.append(payload)
    )
    settings = _settings(tmp_path, notify_webhook="https://hook.invalid/notify")

    report = housekeeping_once(store, settings)

    assert report["notify"] == 1
    assert sent[0]["event"] == EVENT_PLAN_EXPIRING


# ---------------------------------------------------------------- 真实 SMTP


class _SmtpStub(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


class _SmtpHandler(socketserver.StreamRequestHandler):
    """够用就行的 SMTP 服务器：只收一封信，然后把内容记下来。

    标准库的 ``smtpd`` 在 3.12 已经被删了，所以这里自己写一个最小的——
    目的只是让 smtplib 走完真实协议，证明我们没写错参数。
    """

    def handle(self) -> None:
        self.wfile.write(b"220 stub\r\n")
        message: list[str] = []
        in_data = False
        while True:
            line = self.rfile.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            if in_data:
                if text == ".":
                    in_data = False
                    message.append("")
                    self.wfile.write(b"250 ok\r\n")
                else:
                    message.append(text)
                continue
            upper = text.upper()
            if upper.startswith("EHLO") or upper.startswith("HELO"):
                self.wfile.write(b"250-stub\r\n250 AUTH LOGIN PLAIN\r\n")
            elif upper.startswith(("MAIL FROM", "RCPT TO")):
                self.wfile.write(b"250 ok\r\n")
            elif upper.startswith("DATA"):
                in_data = True
                self.wfile.write(b"354 go\r\n")
            elif upper.startswith("QUIT"):
                self.wfile.write(b"221 bye\r\n")
                break
            else:
                self.wfile.write(b"250 ok\r\n")
        self.server.collected.append("\n".join(message))  # type: ignore[attr-defined]


def test_real_smtp_delivery_through_a_stub_server(tmp_path):
    """真的走一遍 SMTP 协议：证明参数（尤其是编码与主题）写对了。"""
    server = _SmtpStub(("127.0.0.1", 0), _SmtpHandler)
    server.collected = []  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        store = _store(tmp_path)
        alice = store.create("alice", "password123")
        store.set_email("alice", "alice@example.com")
        notifier = Notifier(
            store,
            _settings(
                tmp_path,
                smtp_host="127.0.0.1",
                smtp_port=server.server_address[1],
                smtp_from="noreply@example.com",
            ),
        )

        result = notifier.notify(EVENT_PLAN_CHANGED, account=alice, detail={"plan": "basic"})

        assert result["status"] == "sent", result
        raw = "\n".join(server.collected)  # type: ignore[attr-defined]
        assert "alice@example.com" in raw
        assert "Subject:" in raw
        # 中文主题要能被收件端正确还原（RFC 2047 编码）
        assert "=?utf-8?" in raw.lower()
    finally:
        server.shutdown()
        server.server_close()


def test_smtp_error_is_recorded_as_a_failure(tmp_path):
    store = _store(tmp_path)
    alice = store.create("alice", "password123")
    store.set_email("alice", "alice@example.com")
    notifier = Notifier(
        store,
        _settings(tmp_path, smtp_host="127.0.0.1", smtp_port=_free_port(), smtp_from="a@b.com"),
    )

    result = notifier.notify(EVENT_PLAN_CHANGED, account=alice)

    assert result["status"] == "failed"
    assert store.notifications()[0]["status"] == "failed"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
