"""图片进消息：ReAct 的 user 内容块、视觉档位选择与配置读取。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentcode.agents.react import ReActAgent
from agentcode.agents.reflection import ReflectionAgent
from agentcode.agents.plan_and_solve import PlanAndSolveAgent
from agentcode.config import Settings
from agentcode.core.context import RunContext
from agentcode.core.errors import ConfigError
from agentcode.llm.attachments import estimate_prompt_chars
from agentcode.llm.mock import ScriptedLLM
from agentcode.tools.base import ToolRegistry
from agentcode.web import runner


def _image(name: str = "shot.png") -> dict[str, str]:
    return {
        "name": name,
        "media_type": "image/png",
        "data_url": "data:image/png;base64,AAAA",
    }


def test_react_text_loop_sends_image_url_blocks_to_the_model():
    llm = ScriptedLLM(["Thought: 看一眼\nAction: Finish[是一张截图]"])
    agent = ReActAgent(llm=llm, tools=ToolRegistry(), tool_calling="text")
    ctx = RunContext(task="这张图是什么？", images=[_image()])

    result = agent.run("这张图是什么？", context=ctx)

    assert result.success
    user_content = llm.calls[0][1]["content"]
    assert isinstance(user_content, list)
    assert user_content[0]["type"] == "text"
    assert "这张图是什么？" in user_content[0]["text"]
    assert user_content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA"},
    }


def test_react_native_prompt_also_carries_the_images():
    llm = ScriptedLLM(["Thought: 看一眼\nAction: Finish[是一张截图]"])
    agent = ReActAgent(llm=llm, tools=ToolRegistry())
    ctx = RunContext(task="这张图是什么？", images=[_image()])

    messages = agent._build_native_messages("这张图是什么？", "（第一轮）", ctx)

    assert isinstance(messages[1]["content"], list)
    assert messages[1]["content"][1]["type"] == "image_url"


def test_messages_stay_plain_text_without_images():
    llm = ScriptedLLM(["Thought: 好\nAction: Finish[好]"])
    agent = ReActAgent(llm=llm, tools=ToolRegistry(), tool_calling="text")
    ctx = RunContext(task="你好")

    messages = agent._build_messages("你好", [], "（第一轮）", ctx)

    assert isinstance(messages[1]["content"], str)


def test_usage_estimate_does_not_treat_base64_as_text():
    """2 MB 图片的 base64 有几十万字符；按文本算会把预算直接撑爆。"""
    content = [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 500000}},
    ]

    assert estimate_prompt_chars(content) < 5000


def test_reflection_sends_the_image_with_the_first_prompt():
    llm = ScriptedLLM(["初版描述", "无需改进"])
    agent = ReflectionAgent(llm=llm, max_iterations=2)
    ctx = RunContext(task="看图", images=[_image()])

    agent.run("看图", context=ctx)

    assert isinstance(llm.calls[0][0]["content"], list)
    assert llm.calls[0][0]["content"][1]["type"] == "image_url"


def test_plan_and_solve_sends_the_image_to_the_planner():
    llm = ScriptedLLM(['{"steps": ["看图片"]}', "步骤结果"])
    agent = PlanAndSolveAgent(llm=llm)
    ctx = RunContext(task="看图", images=[_image()])

    agent.run("看图", context=ctx)

    assert isinstance(llm.calls[0][0]["content"], list)
    assert llm.calls[0][0]["content"][1]["type"] == "image_url"


def test_mock_mode_answers_with_the_attachment_count():
    events = list(
        runner.run_stream(
            "react",
            "这张图是什么？",
            llm_mode="mock",
            images=[_image("a.png"), _image("b.png")],
        )
    )

    answer = events[-1]["data"]["answer"]
    assert "2 张图片" in answer
    assert "a.png" in answer


def test_real_mode_refuses_images_without_a_vision_model(settings):
    with pytest.raises(ConfigError, match="视觉"):
        runner.create_backend("react", "openai", settings, images=True)


def test_real_mode_uses_the_configured_vision_profile(settings, monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.setenv("VISION_KEY", "sk-vision")

    def fake_build(settings, *, agent=None, explicit=None):
        seen["agent"] = agent
        seen["explicit"] = explicit
        return ScriptedLLM(["Thought: 好\nAction: Finish[好]"])

    monkeypatch.setattr(runner, "build_llm_for_agent", fake_build)
    configured = replace(
        settings,
        model_profiles={
            "vision": {
                "model": "v",
                "base_url": "https://example.invalid/v1",
                "api_key_env": "VISION_KEY",
            }
        },
        vision_profile="vision",
    )

    runner.create_backend("react", "openai", configured, images=True)

    assert seen == {"agent": "react", "explicit": "vision"}


def test_settings_reads_vision_switches(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_VISION_PROFILE", "vision")
    monkeypatch.setenv("AGENT_MODEL_VISION", "true")

    settings = Settings.from_env(env_file=str(tmp_path / "missing.env"))

    assert settings.vision_profile == "vision"
    assert settings.model_vision is True
