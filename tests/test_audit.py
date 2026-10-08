"""操作审计日志（离线）。

重点不是"能写一行字"，而是：**谁在什么时候对谁做了什么，事后查得到**，
而且审计本身绝不能泄露密码、也绝不能把用户的操作搞失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcode.accounts import AccountStore
from agentcode.audit import AuditLog, run_audit_loop
from agentcode.config import Settings


def _store(tmp_path: Path) -> AccountStore:
    return AccountStore(tmp_path / "agentcode.db")


def _settings(tmp_path: Path, *, days: int = 180) -> Settings:
    return Settings(
        model="m",
        api_key="sk-x",
        base_url="https://x.invalid",
        db_path=str(tmp_path / "agentcode.db"),
        audit_days=days,
    )


# ---------------------------------------------------------------- 存取


def test_entries_are_stored_newest_first(tmp_path):
    store = _store(tmp_path)
    store.record_audit("login.ok", actor_name="alice")
    store.record_audit("session.delete", actor_name="alice", target="会话 A")

    entries = store.audit_entries(limit=10)
    assert [item["action"] for item in entries] == ["session.delete", "login.ok"]
    assert entries[0]["target"] == "会话 A"
    assert entries[0]["at"]


def test_filters_by_actor_action_and_limit(tmp_path):
    store = _store(tmp_path)
    store.record_audit("login.ok", actor_name="alice")
    store.record_audit("login.fail", actor_name="bob", result="denied")
    store.record_audit("session.delete", actor_name="alice", target="会话 A")

    assert [item["action"] for item in store.audit_entries(actor_name="alice")] == [
        "session.delete",
        "login.ok",
    ]
    assert [item["action"] for item in store.audit_entries(action="login.fail")] == ["login.fail"]
    assert len(store.audit_entries(limit=2)) == 2
    # 按前缀查一类动作（"所有登录"）
    assert len(store.audit_entries(action_prefix="login.")) == 2


def test_detail_is_stored_as_readable_json(tmp_path):
    store = _store(tmp_path)
    store.record_audit("code.purge", actor_name="alice", detail={"files": 3})
    assert store.audit_entries()[0]["detail"] == {"files": 3}


def test_prune_drops_only_old_entries(tmp_path):
    store = _store(tmp_path)
    store.record_audit("login.ok", actor_name="alice", at="2020-01-01T00:00:00+00:00")
    store.record_audit("login.ok", actor_name="alice")

    removed = store.prune_audit(days=180)

    assert removed == 1
    remaining = store.audit_entries()
    assert len(remaining) == 1
    assert remaining[0]["at"] > "2026"


def test_prune_with_zero_days_keeps_everything(tmp_path):
    store = _store(tmp_path)
    store.record_audit("login.ok", actor_name="alice", at="2020-01-01T00:00:00+00:00")
    assert store.prune_audit(days=0) == 0
    assert len(store.audit_entries()) == 1


# ---------------------------------------------------------------- 门面


def test_log_accepts_an_account_object(tmp_path):
    store = _store(tmp_path)
    alice = store.create("alice", "password123")
    log = AuditLog(store)

    log.record("session.delete", account=alice, target="会话 A")

    entry = log.recent()[0]
    assert entry["actor_id"] == alice.id
    assert entry["actor_name"] == "alice"


def test_log_never_stores_a_password(tmp_path):
    """审计日志泄露密码是经典事故：不管调用方传什么，敏感字段一律抹掉。"""
    store = _store(tmp_path)
    log = AuditLog(store)

    log.record(
        "account.create",
        actor_name="admin",
        detail={"name": "alice", "password": "WYH1q2w3e4r", "nested": {"密码": "x", "ok": 1}},
    )

    detail = log.recent()[0]["detail"]
    assert "WYH1q2w3e4r" not in str(detail)
    assert detail["name"] == "alice"
    assert detail["password"] == "***"
    assert detail["nested"]["密码"] == "***"
    assert detail["nested"]["ok"] == 1


def test_log_never_raises_even_if_the_database_is_gone(tmp_path, capsys):
    """审计是旁路：它写不进去，也不能让用户的操作失败。"""
    store = _store(tmp_path)
    log = AuditLog(store)
    store.close()

    log.record("login.ok", actor_name="alice")

    assert "审计" in capsys.readouterr().out


def test_sweep_uses_the_configured_retention(tmp_path):
    store = _store(tmp_path)
    log = AuditLog(store)
    store.record_audit("login.ok", actor_name="alice", at="2020-01-01T00:00:00+00:00")

    assert log.sweep(_settings(tmp_path, days=180)) == 1
    assert log.recent() == []


def test_run_audit_loop_sweeps_once_then_sleeps(tmp_path, monkeypatch, capsys):
    from agentcode import audit as audit_module

    store = _store(tmp_path)
    log = AuditLog(store)
    store.record_audit("login.ok", actor_name="alice", at="2020-01-01T00:00:00+00:00")
    calls: list[object] = []

    class _Stop(Exception):
        """把循环从 sleep 里拽出来。"""

    def _fake_sweep(item_store, settings):
        assert item_store is store
        calls.append(settings)
        return log.sweep(settings)

    monkeypatch.setattr(audit_module, "sweep_audit", _fake_sweep)
    monkeypatch.setattr(audit_module.time, "sleep", lambda _seconds: _raise(_Stop))
    with pytest.raises(_Stop):
        run_audit_loop(store, _settings(tmp_path), interval_seconds=60)

    assert len(calls) == 1
    assert "审计" in capsys.readouterr().out


def _raise(error: type[BaseException]) -> None:
    raise error
