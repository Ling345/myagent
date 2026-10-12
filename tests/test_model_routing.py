"""多模型档位：按用途路由、整体挂了自动降级、run 内不来回横跳（离线）。"""

from __future__ import annotations

import pytest

from agentcode.agents import ReActAgent
from agentcode.config import Settings
from agentcode.core.errors import ConfigError, LLMError
from agentcode.llm.base import BaseLLM, LLMReply, ToolCallRequest
from agentcode.llm.openai_compatible import RetryableLLMError
from agentcode.llm.routing import (
    ModelProfile,
    RoutedLLM,
    build_llm_for_agent,
    parse_profiles,
    resolve_profile,
)
from agentcode.metrics import MODEL_FALLBACK, MODEL_USED
from agentcode.tools import ToolRegistry


class FakeLLM(BaseLLM):
    """按脚本工作的假后端：要么给文本，要么抛异常。"""

    name = "fake"

    def __init__(self, label: str, *, reply: str = "答复", error: Exception | None = None) -> None:
        super().__init__()
        self.label = label
        self.reply = reply
        self.error = error
        self.calls = 0
        self.seen_tools: list = []

    def think(self, messages, temperature=None) -> str:
        return self.complete(messages).text

    def complete(self, messages, *, tools=None, temperature=None) -> LLMReply:
        self.calls += 1
        self.seen_tools.append(tools)
        if self.error is not None:
            raise self.error
        self._last_usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        return LLMReply(text=f"{self.label}:{self.reply}")


RAW_PROFILES = {
    "strong": {
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "KEY_STRONG",
        "fallback": ["cheap"],
    },
    "cheap": {
        "model": "deepseek-chat",
        "base_url": "https://api.deepseek.com",
        "api_key_env": "KEY_CHEAP",
        "fallback": ["local"],
    },
    "local": {
        "model": "qwen2.5:7b",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key_env": "KEY_LOCAL",
    },
}

ENV = {"KEY_STRONG": "sk-strong", "KEY_CHEAP": "sk-cheap", "KEY_LOCAL": "sk-local"}


# ---------------------------------------------------------------- 解析与校验


def test_parse_profiles_reads_model_base_url_and_keys():
    profiles = parse_profiles(RAW_PROFILES, env=ENV)

    assert set(profiles) == {"strong", "cheap", "local"}
    strong = profiles["strong"]
    assert (strong.model, strong.base_url) == ("deepseek-chat", "https://api.deepseek.com")
    assert strong.keys == ("sk-strong",)
    assert strong.fallback == ("cheap",)


def test_parse_profiles_accepts_multiple_keys_via_api_keys_env():
    raw = {"pool": {"model": "m", "base_url": "https://x", "api_keys_env": "KEYS_POOL"}}

    profile = parse_profiles(raw, env={"KEYS_POOL": "sk-a,sk-b"})["pool"]

    assert profile.keys == ("sk-a", "sk-b")


def test_parse_profiles_reports_missing_fields_in_chinese():
    with pytest.raises(ConfigError, match="model"):
        parse_profiles({"bad": {"base_url": "https://x", "api_key_env": "KEY"}}, env=ENV)
    with pytest.raises(ConfigError, match="base_url"):
        parse_profiles({"bad": {"model": "m", "api_key_env": "KEY"}}, env=ENV)


def test_parse_profiles_rejects_a_profile_without_keys():
    with pytest.raises(ConfigError, match="api_key_env"):
        parse_profiles({"bad": {"model": "m", "base_url": "https://x"}}, env=ENV)


def test_parse_profiles_rejects_an_env_var_that_is_not_set():
    with pytest.raises(ConfigError, match="KEY_MISSING"):
        parse_profiles(
            {"bad": {"model": "m", "base_url": "https://x", "api_key_env": "KEY_MISSING"}}, env=ENV
        )


def test_parse_profiles_rejects_an_unknown_fallback_target():
    raw = {"a": {"model": "m", "base_url": "https://x", "api_key_env": "KEY_STRONG", "fallback": ["nope"]}}

    with pytest.raises(ConfigError, match="nope"):
        parse_profiles(raw, env=ENV)


def test_parse_profiles_rejects_a_fallback_cycle():
    raw = {
        "a": {"model": "m", "base_url": "https://x", "api_key_env": "KEY_STRONG", "fallback": ["b"]},
        "b": {"model": "m", "base_url": "https://x", "api_key_env": "KEY_CHEAP", "fallback": ["a"]},
    }

    with pytest.raises(ConfigError, match="环"):
        parse_profiles(raw, env=ENV)


# ---------------------------------------------------------------- 选档位


def _settings(**overrides) -> Settings:
    values = {
        "model": "m",
        "api_key": "sk-x",
        "base_url": "https://x",
        "model_profiles": RAW_PROFILES,
        "default_profile": "strong",
        "profile_by_agent": {"echo": "cheap"},
    }
    values.update(overrides)
    return Settings(**values)


def test_resolve_profile_prefers_explicit_then_agent_then_default():
    settings = _settings()

    assert resolve_profile(settings, explicit="local") == "local"
    assert resolve_profile(settings, agent="echo") == "cheap"
    assert resolve_profile(settings, agent="react") == "strong"


def test_resolve_profile_returns_empty_without_profiles():
    assert resolve_profile(Settings(model="m", api_key="k", base_url="https://x")) == ""


def test_resolve_profile_rejects_an_unknown_name():
    with pytest.raises(ConfigError, match="没有这个模型档位"):
        resolve_profile(_settings(), explicit="查无此档")


# ---------------------------------------------------------------- 降级


def _chain(*items: tuple[str, BaseLLM], sleep=None) -> RoutedLLM:
    return RoutedLLM(items, sleep=sleep or (lambda _seconds: None))


def test_routed_llm_uses_the_primary_when_it_works():
    primary, backup = FakeLLM("strong"), FakeLLM("cheap")

    reply = _chain(("strong", primary), ("cheap", backup)).complete([{"role": "user", "content": "x"}])

    assert reply.text == "strong:答复"
    assert backup.calls == 0
    assert MODEL_USED.value(profile="strong") >= 1


def test_routed_llm_falls_back_when_a_profile_is_broken():
    primary = FakeLLM("strong", error=LLMError("连接被拒绝"))
    backup = FakeLLM("cheap")
    router = _chain(("strong", primary), ("cheap", backup))

    reply = router.complete([{"role": "user", "content": "x"}])

    assert reply.text == "cheap:答复"
    assert router.used_profile == "cheap"
    assert MODEL_FALLBACK.value(**{"from": "strong", "to": "cheap"}) >= 1


def test_routed_llm_remembers_the_downgrade_within_the_run():
    """降级之后不再回头撞那堵墙：同一个 run 里一直用后面的档位。"""
    primary = FakeLLM("strong", error=LLMError("连接被拒绝"))
    backup = FakeLLM("cheap")
    router = _chain(("strong", primary), ("cheap", backup))

    router.complete([{"role": "user", "content": "第一次"}])
    router.complete([{"role": "user", "content": "第二次"}])

    assert primary.calls == 1  # 只试了一次
    assert backup.calls == 2


def test_routed_llm_waits_before_switching_on_retryable_errors():
    delays: list[float] = []
    primary = FakeLLM("strong", error=RetryableLLMError("调用大语言模型失败：429 限流"))
    router = _chain(("strong", primary), ("cheap", FakeLLM("cheap")), sleep=delays.append)

    router.complete([{"role": "user", "content": "x"}])

    assert delays and delays[0] > 0  # 限流先退避，再降级


def test_routed_llm_fails_loudly_when_every_profile_is_down():
    router = _chain(
        ("strong", FakeLLM("strong", error=LLMError("炸了"))),
        ("cheap", FakeLLM("cheap", error=LLMError("也炸了"))),
    )

    with pytest.raises(LLMError) as error:
        router.complete([{"role": "user", "content": "x"}])

    message = str(error.value)
    assert "strong" in message and "cheap" in message
    assert "全部失败" in message


def test_routed_llm_passes_tools_through():
    primary = FakeLLM("strong")

    _chain(("strong", primary)).complete([{"role": "user", "content": "x"}], tools=[{"a": 1}])

    assert primary.seen_tools == [[{"a": 1}]]


def test_routed_llm_reports_tool_support_of_any_profile():
    class Toolish(FakeLLM):
        supports_tools = True

    assert _chain(("a", Toolish("a")), ("b", FakeLLM("b"))).supports_tools is True
    assert _chain(("a", FakeLLM("a"))).supports_tools is False


# ---------------------------------------------------------------- 构造


def test_build_llm_without_profiles_keeps_the_old_single_model_path():
    settings = Settings(model="m", api_key="sk-x", base_url="https://x")

    llm = build_llm_for_agent(settings, provider_factory=lambda profile, _s: FakeLLM(profile.name))

    assert not isinstance(llm, RoutedLLM)
    assert getattr(llm, "label") == ""


def test_build_llm_with_profiles_returns_a_router_with_the_fallback_chain():
    seen: list[str] = []

    def factory(profile: ModelProfile, _settings) -> BaseLLM:
        seen.append(profile.name)
        return FakeLLM(profile.name)

    llm = build_llm_for_agent(_settings(), agent="echo", provider_factory=factory, env=ENV)

    assert isinstance(llm, RoutedLLM)
    assert seen == ["cheap", "local"]  # echo 走 cheap，再挂就降级到 local


def test_build_llm_rejects_an_explicit_unknown_profile():
    with pytest.raises(ConfigError):
        build_llm_for_agent(_settings(), explicit="查无此档", provider_factory=lambda p, s: FakeLLM(p.name))


def test_a_single_profile_still_reports_which_profile_answered():
    """只配一档时也要能记下"谁答的"——排查问题时不能因为"没配降级"就看不见。"""
    settings = _settings(model_profiles={"only": RAW_PROFILES["local"]}, default_profile="only")

    llm = build_llm_for_agent(
        settings, provider_factory=lambda profile, _s: FakeLLM(profile.name), env={"KEY_LOCAL": "sk-local"}
    )

    llm.complete([{"role": "user", "content": "x"}])

    assert isinstance(llm, RoutedLLM)
    assert llm.used_profile == "only"


# ---------------------------------------------------------------- 与智能体配合


def _weather_tools() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register_tool("get_weather", "查天气", lambda city: f"{city}晴", {"city": "城市"})
    return tools


def test_agent_result_records_which_profile_answered():
    broken = FakeLLM("strong", error=LLMError("连接被拒绝"))
    backup = FakeLLM("cheap")
    backup.complete = lambda messages, tools=None, temperature=None: LLMReply(  # type: ignore[assignment]
        text="Thought: 直接答\nAction: Finish[来自便宜档]"
    )
    router = _chain(("strong", broken), ("cheap", backup))

    result = ReActAgent(llm=router, tools=_weather_tools()).run("北京天气如何")

    assert result.answer == "来自便宜档"
    assert result.extra["model_profile"] == "cheap"


# ---------------------------------------------------------------- 配置入口


def test_settings_can_load_profiles_from_a_file(tmp_path, monkeypatch):
    import json

    path = tmp_path / "models.json"
    path.write_text(json.dumps(RAW_PROFILES, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("KEY_STRONG", "sk-strong")
    monkeypatch.setenv("AGENT_MODEL_PROFILES_FILE", str(path))

    settings = Settings.from_env(env_file=str(tmp_path / "missing.env"), search_parents=False)

    assert set(settings.model_profiles) == {"strong", "cheap", "local"}
    # 没写 default 时，按配置文件里的书写顺序取第一个（strong 写在最前面）
    assert resolve_profile(settings) == "strong"


def test_profile_keys_can_come_from_the_env_file(tmp_path, monkeypatch):
    """档位里的 api_key_env 指向 .env 里的变量时也得取得到。

    框架是自己解析 .env 的，**不会**把它塞进 os.environ——只认 os.environ 的话，
    用户按文档把 key 写在 .env 里反而取不到，这种"配了却说没配"的坑最难查。
    """
    import json

    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_API_KEY=sk-main\nLLM_API_KEY_CHEAP=sk-cheap\n"
        "LLM_MODEL_ID=m\nLLM_BASE_URL=https://x\n",
        encoding="utf-8",
    )
    profiles = tmp_path / "models.json"
    profiles.write_text(
        json.dumps(
            {
                "profiles": {
                    "cheap": {
                        "model": "m",
                        "base_url": "https://x",
                        "api_key_env": "LLM_API_KEY_CHEAP",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_MODEL_PROFILES_FILE", str(profiles))
    monkeypatch.delenv("LLM_API_KEY_CHEAP", raising=False)
    settings = Settings.from_env(env_file=str(env_file), search_parents=False)

    llm = build_llm_for_agent(
        settings, provider_factory=lambda profile, _s: FakeLLM(profile.name), env=None
    )

    assert isinstance(llm, RoutedLLM)
    llm.complete([{"role": "user", "content": "x"}])
    assert llm.used_profile == "cheap"
    settings.validate()  # 校验时也不该报"没配 key"


def test_a_missing_profiles_file_is_a_loud_error(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-x")
    monkeypatch.setenv("LLM_BASE_URL", "https://x")
    monkeypatch.setenv("LLM_MODEL_ID", "m")
    monkeypatch.setenv("AGENT_MODEL_PROFILES_FILE", str(tmp_path / "没有这个文件.json"))

    settings = Settings.from_env(env_file=str(tmp_path / "missing.env"), search_parents=False)

    with pytest.raises(ConfigError, match="模型档位配置"):
        settings.validate()


def test_masked_summary_shows_profiles_without_keys():
    settings = _settings()

    masked = settings.masked()

    assert "strong" in masked["AGENT_MODEL_PROFILES"]
    assert "sk-strong" not in str(masked)
