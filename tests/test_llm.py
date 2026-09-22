"""LLM 后端测试（全部离线）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentcode.config import Settings
from agentcode.core.errors import ConfigError, LLMError
from agentcode.llm import OpenAICompatibleLLM, ScriptedLLM


def test_scripted_llm_returns_responses_in_order():
    llm = ScriptedLLM(["第一步", "第二步"])
    messages = [{"role": "user", "content": "x"}]
    assert llm.think(messages) == "第一步"
    assert llm.think(messages) == "第二步"
    assert llm.think(messages) == "第二步"


def test_scripted_llm_records_calls_and_usage():
    llm = ScriptedLLM(["回复"])
    llm.think([{"role": "user", "content": "你好"}])
    assert len(llm.calls) == 1
    assert llm.last_usage["estimated"] is True


def test_empty_script_without_fallback_raises():
    with pytest.raises(LLMError):
        ScriptedLLM([]).think([{"role": "user", "content": "x"}])


def test_scripted_llm_uses_fallback_when_exhausted():
    llm = ScriptedLLM([], fallback="固定回复")
    assert llm.think([{"role": "user", "content": "x"}]) == "固定回复"


def test_openai_llm_without_key_raises_config_error():
    with pytest.raises(ConfigError):
        OpenAICompatibleLLM(model="m", api_key="", base_url="https://example.com")


def test_openai_llm_from_settings_validates(monkeypatch):
    settings = Settings(model=None, api_key=None, base_url=None)
    with pytest.raises(ConfigError):
        OpenAICompatibleLLM.from_settings(settings)


def test_openai_llm_reads_stream_chunks():
    def chunk(text: str | None):
        delta = SimpleNamespace(content=text)
        return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)

    class FakeCompletions:
        def create(self, **kwargs):
            assert kwargs["model"] == "fake-model"
            return [chunk("你好"), chunk("，"), chunk("世界")]

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    llm = OpenAICompatibleLLM(
        model="fake-model",
        api_key="sk-fake",
        base_url="https://example.invalid/v1",
        client=fake_client,
    )
    assert llm.think([{"role": "user", "content": "打个招呼"}]) == "你好，世界"


def test_openai_llm_wraps_errors_as_llm_error():
    class ExplodingCompletions:
        def create(self, **kwargs):
            raise RuntimeError("连接被拒绝")

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=ExplodingCompletions()))
    llm = OpenAICompatibleLLM(
        model="fake-model",
        api_key="sk-fake",
        base_url="https://example.invalid/v1",
        stream=False,
        client=fake_client,
    )
    with pytest.raises(LLMError):
        llm.think([{"role": "user", "content": "你好"}])
