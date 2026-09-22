"""测试公共夹具。"""

from __future__ import annotations

import os

import pytest

from agentcode.config import Settings
from agentcode.llm.mock import ScriptedLLM
from agentcode.tools.base import ToolRegistry


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch):
    """避免真实 .env 干扰测试：默认清空环境变量，需要时由用例自行设置。"""
    for key in (
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL_ID",
        "SERPAPI_API_KEY",
        "LLM_TIMEOUT",
        "LLM_TEMPERATURE",
        "LLM_STREAM",
        "AGENT_MAX_STEPS",
        "AGENT_TRACE_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def tools() -> ToolRegistry:
    """一个空工具注册表。"""
    return ToolRegistry()


@pytest.fixture
def scripted_llm() -> ScriptedLLM:
    """默认脚本模型：直接给出最终答案。"""
    return ScriptedLLM(["Thought: 直接回答\nAction: Finish[好的]"])


@pytest.fixture
def settings() -> Settings:
    """一份可用的假配置。"""
    return Settings(
        model="test-model",
        api_key="sk-test-1234567890",
        base_url="https://example.invalid/v1",
    )
