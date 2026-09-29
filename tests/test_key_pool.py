"""API key 池与熔断测试（全部离线，不需要真 key）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentcode.config import Settings
from agentcode.core.errors import ConfigError, LLMError
from agentcode.llm import openai_compatible as llm_module
from agentcode.llm.keypool import KeyPool, mask_key, parse_keys
from agentcode.llm.openai_compatible import OpenAICompatibleLLM, is_retryable


class FakeClock:
    """可控时钟，用来测冷却到期。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeAPIError(Exception):
    """带 HTTP 状态码的假错误。"""

    def __init__(self, status_code: int, message: str = "服务端不干了") -> None:
        super().__init__(message)
        self.status_code = status_code


# ------------------------------------------------------------------ 解析与脱敏


def test_parse_keys_splits_and_dedupes():
    assert parse_keys("sk-a, sk-b\nsk-c;sk-a") == ["sk-a", "sk-b", "sk-c"]


def test_parse_keys_empty_input():
    assert parse_keys(None) == []
    assert parse_keys("   ") == []


def test_mask_key_keeps_edges():
    assert mask_key("sk-abcdefghijklmn") == "sk-a******klmn"
    assert mask_key("short") == "*****"


def test_pool_rejects_empty_key_list():
    with pytest.raises(ConfigError, match="key 池是空的"):
        KeyPool([])


# ------------------------------------------------------------------ 轮换与熔断


def test_pool_rotates_round_robin():
    pool = KeyPool(["a", "b", "c"])
    picked = [pool.acquire().key for _ in range(4)]
    assert picked == ["a", "b", "c", "a"]


def test_failure_below_threshold_keeps_key_in_service():
    pool = KeyPool(["a", "b"], failure_threshold=3)
    state = pool.acquire()
    pool.report_failure(state)
    pool.report_failure(state)
    assert state.failures == 2
    assert pool.snapshot()[0]["state"] == "closed"


def test_threshold_trips_key_and_others_keep_serving():
    pool = KeyPool(["a", "b"], failure_threshold=2, cooldown=60)
    bad = pool.acquire()  # a
    pool.report_failure(bad)
    pool.report_failure(bad)
    assert pool.snapshot()[0]["state"] == "open"

    # 后续只应该轮换到 b
    assert [pool.acquire().key for _ in range(3)] == ["b", "b", "b"]


def test_key_recovers_after_cooldown():
    clock = FakeClock()
    pool = KeyPool(["a"], failure_threshold=1, cooldown=30, clock=clock)
    pool.report_failure(pool.acquire())
    assert pool.acquire() is None

    clock.advance(31)
    assert pool.acquire().key == "a"


def test_success_resets_failure_count():
    pool = KeyPool(["a"], failure_threshold=3)
    state = pool.acquire()
    pool.report_failure(state)
    pool.report_failure(state)
    pool.report_success(state)
    assert state.failures == 0
    assert pool.snapshot()[0]["state"] == "closed"


def test_next_ready_in_reports_earliest_cooldown():
    clock = FakeClock()
    pool = KeyPool(["a", "b"], failure_threshold=1, cooldown=30, clock=clock)
    pool.report_failure(pool.acquire())  # a
    clock.advance(10)
    pool.report_failure(pool.acquire())  # b，还有 30 秒
    assert pool.next_ready_in() == pytest.approx(20)


def test_snapshot_masks_keys_and_flags_open():
    clock = FakeClock()
    pool = KeyPool(
        ["sk-aaaaaaaaaaaa", "sk-bbbbbbbbbbbb"], failure_threshold=1, cooldown=5, clock=clock
    )
    pool.report_failure(pool.acquire())
    first, second = pool.snapshot()
    assert first["key"] == "sk-a******aaaa"
    assert first["state"] == "open"
    assert first["cooldown_remaining"] == pytest.approx(5)
    assert second["state"] == "closed"


# ------------------------------------------------------------------ 错误分类


def test_is_retryable_classification():
    assert is_retryable(FakeAPIError(429)) is True
    assert is_retryable(FakeAPIError(401)) is True
    assert is_retryable(FakeAPIError(503)) is True
    assert is_retryable(RuntimeError("连接超时")) is True
    assert is_retryable(FakeAPIError(400)) is False
    assert is_retryable(FakeAPIError(422)) is False


# ------------------------------------------------------------------ 后端轮换


def _ok_client(text: str):
    def create(**kwargs):
        delta = SimpleNamespace(content=text)
        return [SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)]

    return create


def _failing_client(status: int):
    def create(**kwargs):
        raise FakeAPIError(status)

    return create


def _patch_clients(monkeypatch, behaviors: dict):
    """按 key 提供不同的假客户端，并记录建了哪些。"""
    seen: list[str] = []

    def fake_shared_client(api_key: str, base_url: str, timeout: float):
        seen.append(api_key)
        create = behaviors[api_key]
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(llm_module, "_shared_client", fake_shared_client)
    return seen


def _messages():
    return [{"role": "user", "content": "你好"}]


def test_llm_rotates_to_next_key_when_rate_limited(monkeypatch):
    _patch_clients(monkeypatch, {"sk-bad": _failing_client(429), "sk-good": _ok_client("成功了")})
    llm = OpenAICompatibleLLM(
        model="m", api_keys=["sk-bad", "sk-good"], base_url="https://x.invalid"
    )
    assert llm.think(_messages()) == "成功了"
    assert llm.last_key_index == 1
    # 坏 key 记了一次失败，好 key 记了一次成功
    assert llm.pool.snapshot()[0]["failures"] == 1
    assert llm.pool.snapshot()[1]["successes"] == 1


def test_llm_raises_when_every_key_fails(monkeypatch):
    _patch_clients(monkeypatch, {"sk-a": _failing_client(429), "sk-b": _failing_client(503)})
    llm = OpenAICompatibleLLM(model="m", api_keys=["sk-a", "sk-b"], base_url="https://x.invalid")
    with pytest.raises(LLMError, match="全部失败"):
        llm.think(_messages())


def test_llm_does_not_rotate_on_bad_request(monkeypatch):
    """400 是我们自己请求写错了，换 key 没意义，也不该污染熔断计数。"""
    calls: list[str] = []

    def bad(**kwargs):
        calls.append("bad")
        raise FakeAPIError(400)

    def good(**kwargs):
        calls.append("good")
        return _ok_client("不该走到这里")(**kwargs)

    _patch_clients(monkeypatch, {"sk-a": bad, "sk-b": good})
    llm = OpenAICompatibleLLM(model="m", api_keys=["sk-a", "sk-b"], base_url="https://x.invalid")
    with pytest.raises(LLMError, match="调用大语言模型失败"):
        llm.think(_messages())
    assert calls == ["bad"]
    assert llm.pool.snapshot()[0]["failures"] == 0


def test_llm_reports_when_all_keys_are_cooling(monkeypatch):
    # 构造客户端时会先给第一个 key 建连接，所以这里也要给个行为
    _patch_clients(monkeypatch, {"sk-a": _failing_client(429)})
    clock = FakeClock()
    pool = KeyPool(["sk-a"], failure_threshold=1, cooldown=30, clock=clock)
    pool.report_failure(pool.primary())
    llm = OpenAICompatibleLLM(model="m", base_url="https://x.invalid", key_pool=pool)
    with pytest.raises(LLMError, match="熔断冷却"):
        llm.think(_messages())


# ------------------------------------------------------------------ 配置接线


def test_settings_parses_multiple_keys_from_env_file(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        # .env 里多行值需要引号，所以文件里用逗号分隔；换行分隔由 parse_keys 直接兜住
        "LLM_API_KEYS=sk-one,sk-two,sk-three\n"
        "LLM_API_KEY=sk-fallback\n"
        "LLM_BASE_URL=https://x.invalid/v1\nLLM_MODEL_ID=m\n",
        encoding="utf-8",
    )
    settings = Settings.from_env(env_file=str(env_file), search_parents=False).validate()
    assert settings.api_key_list() == ["sk-one", "sk-two", "sk-three"]
    assert settings.api_key == "sk-one"


def test_settings_falls_back_to_single_key(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_API_KEY=sk-single\nLLM_BASE_URL=https://x.invalid/v1\nLLM_MODEL_ID=m\n",
        encoding="utf-8",
    )
    settings = Settings.from_env(env_file=str(env_file), search_parents=False).validate()
    assert settings.api_key_list() == ["sk-single"]


def test_missing_keys_accepts_key_pool(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_API_KEYS=sk-one,sk-two\nLLM_BASE_URL=https://x.invalid/v1\nLLM_MODEL_ID=m\n",
        encoding="utf-8",
    )
    settings = Settings.from_env(env_file=str(env_file), search_parents=False)
    assert settings.missing_keys() == []


def test_masked_config_summarises_key_pool(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_API_KEYS=sk-aaaaaaaaaaaa,sk-bbbbbbbbbbbb\n"
        "LLM_BASE_URL=https://x.invalid/v1\nLLM_MODEL_ID=m\n",
        encoding="utf-8",
    )
    masked = Settings.from_env(env_file=str(env_file), search_parents=False).masked()
    assert masked["LLM_API_KEYS"] == "共 2 个：sk-a******aaaa、sk-b******bbbb"
    assert "sk-aaaaaaaaaaaa" not in masked["LLM_API_KEYS"]
