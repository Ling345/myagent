"""联网搜索工具：密钥注入、结果解析与失败提示（全部离线，用假客户端）。"""

from __future__ import annotations

import pytest

from agentcode.tools import ToolRegistry
from agentcode.tools.builtin import register_builtin_tools, web_search


class _FakeSerpApi:
    """替身：记录调用参数并返回预设结果。"""

    params: dict = {}
    payload: dict = {}

    def __init__(self, params: dict) -> None:
        type(self).params = params

    def get_dict(self) -> dict:
        return type(self).payload


@pytest.fixture
def fake_serpapi(monkeypatch):
    """把 serpapi 客户端换成替身，函数返回设置结果的方法。"""
    monkeypatch.setattr("serpapi.SerpApiClient", _FakeSerpApi)
    _FakeSerpApi.payload = {}

    def configure(payload: dict) -> None:
        _FakeSerpApi.payload = payload

    return configure


# ---------------------------------------------------------------- 密钥注入


def test_registry_injects_configured_key(fake_serpapi):
    """回归：密钥来自配置，不再依赖进程环境变量（否则搜索永远"未配置"）。"""
    fake_serpapi({"organic_results": []})
    registry = register_builtin_tools(ToolRegistry(), serpapi_key="key-from-settings")

    registry.invoke("web_search", "北京天气")

    assert _FakeSerpApi.params["api_key"] == "key-from-settings"
    assert _FakeSerpApi.params["q"] == "北京天气"
    assert _FakeSerpApi.params["hl"] == "zh-cn"


def test_search_works_without_env_var(fake_serpapi, monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    fake_serpapi({"organic_results": [{"title": "标题", "snippet": "摘要"}]})
    registry = register_builtin_tools(ToolRegistry(), serpapi_key="only-in-config")
    assert "摘要" in registry.invoke("web_search", "随便问问")


def test_search_reports_missing_key(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    registry = register_builtin_tools(ToolRegistry(), serpapi_key=None)
    assert "未配置 SERPAPI_API_KEY" in registry.invoke("web_search", "北京天气")


def test_direct_call_falls_back_to_env(monkeypatch, fake_serpapi):
    monkeypatch.setenv("SERPAPI_API_KEY", "key-from-env")
    fake_serpapi({"organic_results": []})
    web_search("北京天气")
    assert _FakeSerpApi.params["api_key"] == "key-from-env"


# ---------------------------------------------------------------- 结果解析


def test_answer_box_wins(fake_serpapi):
    fake_serpapi(
        {
            "answer_box": {"answer": "25 摄氏度"},
            "organic_results": [{"title": "其它结果", "snippet": "不该出现"}],
        }
    )
    assert web_search("北京天气", api_key="k") == "25 摄氏度"


def test_knowledge_graph_used_when_no_answer_box(fake_serpapi):
    fake_serpapi({"knowledge_graph": {"description": "北京是中国的首都"}})
    assert web_search("北京", api_key="k") == "北京是中国的首都"


def test_organic_results_have_no_source_link(fake_serpapi):
    """结果里不带链接：模型会把链接照抄进最终答案，而答案要保持干净。"""
    fake_serpapi(
        {
            "organic_results": [
                {"title": "第一条", "snippet": "摘要一", "link": "https://example.com/a"},
                {"title": "第二条", "snippet": "摘要二"},
            ]
        }
    )
    output = web_search("测试", api_key="k")
    assert "[1] 第一条" in output
    assert "摘要一" in output
    assert "[2] 第二条" in output
    assert "来源" not in output
    assert "https://" not in output


def test_max_results_limits_output(fake_serpapi):
    fake_serpapi(
        {"organic_results": [{"title": f"第{i}条", "snippet": "x"} for i in range(1, 6)]}
    )
    output = web_search("测试", api_key="k", max_results=2)
    assert "[2] 第2条" in output
    assert "第3条" not in output


def test_no_results_message(fake_serpapi):
    fake_serpapi({"organic_results": []})
    assert "没有找到" in web_search("极冷门关键词", api_key="k")


# ---------------------------------------------------------------- 失败提示


def test_invalid_key_hint(monkeypatch, fake_serpapi):
    def raise_error(params):
        raise RuntimeError("401 Invalid API key")

    monkeypatch.setattr("serpapi.SerpApiClient", raise_error)
    output = web_search("北京", api_key="bad")
    assert "网页搜索失败" in output
    assert "密钥可能无效" in output


def test_quota_hint(monkeypatch):
    def raise_error(params):
        raise RuntimeError("429 You have run out of searches")

    monkeypatch.setattr("serpapi.SerpApiClient", raise_error)
    assert "额度可能已用完" in web_search("北京", api_key="k")


def test_generic_error_is_reported(monkeypatch):
    def raise_error(params):
        raise RuntimeError("连接超时")

    monkeypatch.setattr("serpapi.SerpApiClient", raise_error)
    assert "连接超时" in web_search("北京", api_key="k")
