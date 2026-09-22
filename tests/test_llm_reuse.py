"""客户端复用：同样的配置应当共享同一个连接池。"""

from __future__ import annotations

from agentcode.llm.openai_compatible import OpenAICompatibleLLM


def test_same_config_shares_one_client():
    first = OpenAICompatibleLLM(model="m", api_key="sk-test", base_url="https://example.invalid")
    second = OpenAICompatibleLLM(model="m", api_key="sk-test", base_url="https://example.invalid")
    assert first.client is second.client


def test_different_config_gets_its_own_client():
    first = OpenAICompatibleLLM(model="m", api_key="sk-test", base_url="https://example.invalid")
    other = OpenAICompatibleLLM(model="m", api_key="sk-test", base_url="https://other.invalid")
    assert first.client is not other.client


def test_injected_client_is_used_as_is():
    marker = object()
    llm = OpenAICompatibleLLM(
        model="m", api_key="sk-test", base_url="https://example.invalid", client=marker
    )
    assert llm.client is marker
