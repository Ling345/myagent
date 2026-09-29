"""告警规则与推送测试（离线，不真的发请求）。"""

from __future__ import annotations

import json

import pytest

from agentcode.alerts import Alert, AlertMonitor
from agentcode.metrics import METRICS


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _clean_metrics():
    METRICS.reset()
    yield
    METRICS.reset()


def _monitor(clock=None, posted=None, **kwargs):
    """造一个不真的发请求的监控器。"""
    sent: list[tuple[str, dict]] = posted if posted is not None else []

    def poster(url: str, payload: dict) -> None:
        sent.append((url, payload))

    monitor = AlertMonitor(
        clock=clock or FakeClock(),
        poster=poster,
        webhook="https://hooks.example.invalid/abc",
        **kwargs,
    )
    return monitor, sent


def _hit_http(status: str, times: int) -> None:
    counter = METRICS.counter("agentcode_http_requests_total", "")
    for _ in range(times):
        counter.inc(path="/api/run", status=status)


def _hit_llm(result: str, times: int) -> None:
    counter = METRICS.counter("agentcode_llm_calls_total", "")
    for _ in range(times):
        counter.inc(key="0", result=result)


# ---------------------------------------------------------------- 规则


def test_no_alerts_when_everything_is_fine():
    monitor, sent = _monitor()
    _hit_http("200", 100)
    monitor.evaluate()  # 第一拍只建基线
    assert monitor.evaluate() == []
    assert sent == []


def test_http_5xx_rate_triggers():
    monitor, _ = _monitor()
    monitor.evaluate()  # 基线
    _hit_http("200", 10)
    _hit_http("500", 10)  # 一半是 5xx

    alerts = monitor.evaluate()
    assert [alert.rule for alert in alerts] == ["http_error_rate"]
    assert "5xx" in alerts[0].message


def test_http_4xx_does_not_trigger_the_error_rate():
    """4xx 多半是用户自己的问题（没登录、参数错），不该半夜告警。"""
    monitor, _ = _monitor()
    monitor.evaluate()
    _hit_http("401", 30)
    assert monitor.evaluate() == []


def test_too_few_samples_do_not_trigger():
    """刚起来就两个请求、其中一个是 5xx，不该被判成"服务挂了"。"""
    monitor, _ = _monitor()
    monitor.evaluate()
    _hit_http("200", 1)
    _hit_http("500", 1)
    assert monitor.evaluate() == []


def test_llm_failure_rate_triggers():
    monitor, _ = _monitor()
    monitor.evaluate()
    _hit_llm("ok", 5)
    _hit_llm("failed", 25)

    alerts = monitor.evaluate()
    assert "llm_failure_rate" in [alert.rule for alert in alerts]


def test_sandbox_timeout_triggers():
    monitor, _ = _monitor()
    monitor.evaluate()
    METRICS.counter("agentcode_sandbox_runs_total", "").inc(backend="local", result="timeout")

    alerts = monitor.evaluate()
    assert [alert.rule for alert in alerts] == ["sandbox_timeout"]
    assert "死循环" in alerts[0].message


def test_keypool_trip_triggers():
    monitor, _ = _monitor()
    monitor.evaluate()
    METRICS.counter("agentcode_keypool_trips_total", "").inc(key="0")

    alerts = monitor.evaluate()
    assert [alert.rule for alert in alerts] == ["keypool_trip"]


def test_window_slides_so_old_traffic_stops_counting():
    clock = FakeClock()
    monitor, _ = _monitor(clock=clock, window_seconds=300)
    monitor.evaluate()
    _hit_http("500", 30)
    assert monitor.evaluate() != []

    clock.advance(600)  # 窗口滑过去了，那批 5xx 不再计入
    assert monitor.evaluate() == []


# ---------------------------------------------------------------- 冷却与推送


def test_cooldown_suppresses_repeats():
    clock = FakeClock()
    monitor, sent = _monitor(clock=clock, cooldown_seconds=600)
    monitor.evaluate()
    _hit_http("500", 30)
    assert len(monitor.check()) == 1
    assert len(sent) == 1

    clock.advance(60)
    assert monitor.check() == []  # 还在冷却里
    assert len(sent) == 1


def test_cooldown_expires_and_fires_again():
    clock = FakeClock()
    # 窗口要比冷却长：冷却到期时基线样本还得在窗口里，否则算不出增量
    monitor, sent = _monitor(clock=clock, window_seconds=1200, cooldown_seconds=600)
    monitor.evaluate()
    _hit_http("500", 30)
    monitor.check()

    clock.advance(700)
    _hit_http("500", 30)
    monitor.check()
    assert len(sent) == 2


def test_webhook_payload_shape():
    clock = FakeClock()
    monitor, sent = _monitor(clock=clock)
    monitor.evaluate()
    _hit_http("500", 30)
    monitor.check()

    url, payload = sent[0]
    assert url == "https://hooks.example.invalid/abc"
    assert payload["rule"] == "http_error_rate"
    assert payload["severity"] == "critical"
    assert "message" in payload
    assert isinstance(payload["details"], dict)


def test_without_webhook_no_request_is_made_but_alert_is_returned():
    monitor = AlertMonitor(clock=FakeClock(), poster=None, webhook="")
    monitor.evaluate()
    _hit_http("500", 30)
    alerts = monitor.check()
    assert [alert.rule for alert in alerts] == ["http_error_rate"]


def test_poster_failure_does_not_break_the_monitor():
    def broken(url: str, payload: dict) -> None:
        raise RuntimeError("网络不通")

    monitor = AlertMonitor(clock=FakeClock(), poster=broken, webhook="https://x.invalid")
    monitor.evaluate()
    _hit_http("500", 30)
    assert len(monitor.check()) == 1  # 推送失败不影响告警本身


def test_alert_payload_is_json_serialisable():
    alert = Alert(rule="r", message="m", severity="warning", details={"a": 1})
    payload = alert.to_payload()
    assert payload["rule"] == "r"
    assert json.loads(json.dumps(payload))["details"] == {"a": 1}


# ---------------------------------------------------------------- 配置接线


def test_config_reads_alert_settings(tmp_path, monkeypatch):
    from agentcode.config import Settings

    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_API_KEY=sk-x\nLLM_BASE_URL=https://x.invalid/v1\nLLM_MODEL_ID=m\n"
        "AGENT_ALERT_WEBHOOK=https://hooks.example.invalid/secret-token\n"
        "AGENT_ALERT_WINDOW_SECONDS=600\n"
        "AGENT_ALERT_COOLDOWN_SECONDS=1800\n"
        "AGENT_ALERT_ERROR_RATE=0.3\n",
        encoding="utf-8",
    )
    settings = Settings.from_env(env_file=str(env_file), search_parents=False)
    assert settings.alert_webhook == "https://hooks.example.invalid/secret-token"
    assert settings.alert_window_seconds == 600
    assert settings.alert_cooldown_seconds == 1800
    assert settings.alert_error_rate == 0.3


def test_webhook_is_masked_in_config_output(tmp_path):
    """webhook 的 URL 里挂着密钥，打印配置时必须脱敏。"""
    from agentcode.config import Settings

    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_API_KEY=sk-x\nLLM_BASE_URL=https://x.invalid/v1\nLLM_MODEL_ID=m\n"
        "AGENT_ALERT_WEBHOOK=https://hooks.example.invalid/secret-token-abcdef\n",
        encoding="utf-8",
    )
    masked = Settings.from_env(env_file=str(env_file), search_parents=False).masked()
    assert "secret-token-abcdef" not in masked["AGENT_ALERT_WEBHOOK"]
    assert masked["AGENT_ALERT_WEBHOOK"].startswith("http")


def test_create_server_attaches_a_monitor(tmp_path):
    from agentcode.accounts import AccountStore
    from agentcode.web.server import create_server

    server = create_server(
        host="127.0.0.1",
        port=0,
        llm_mode="mock",
        quiet=True,
        session_dir=str(tmp_path / "sessions"),
        accounts=AccountStore(tmp_path / "accounts.db"),
    )
    try:
        assert server.alert_monitor is not None  # type: ignore[attr-defined]
    finally:
        server.server_close()
