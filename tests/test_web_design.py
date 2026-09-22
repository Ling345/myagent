"""网页视觉规范测试：流动浅蓝背景、半透明面板、DeepSeek 式配色与布局。"""

from __future__ import annotations

from agentcode.web.server import STATIC_DIR


def _html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _css() -> str:
    return (STATIC_DIR / "style.css").read_text(encoding="utf-8")


def test_page_has_flowing_background_layer():
    assert 'class="flow"' in _html()
    assert "drift-a" in _css()
    assert "drift-b" in _css()


def test_flowing_background_uses_light_blue():
    css = _css()
    assert "--bg-base: #f7faff" in css
    assert "#d3e4ff" in css  # 流动光斑的浅蓝


def test_background_motion_respects_reduced_motion():
    css = _css()
    assert "prefers-reduced-motion: reduce" in css
    assert ".blob" in css


def test_panels_are_translucent():
    css = _css()
    assert "backdrop-filter" in css
    assert "rgba(255, 255, 255, 0.55)" in css


def test_uses_deepseek_like_accent():
    css = _css()
    assert "--accent: #3f5cf0" in css
    assert "--accent-soft" in css


def test_layout_is_sidebar_plus_chat():
    html = _html()
    assert 'class="sidebar"' in html
    assert 'class="chat"' in html
    assert 'id="messages"' in html
    assert 'class="composer"' in html


def test_page_has_sample_prompts_with_agents():
    html = _html()
    assert 'class="sample"' in html
    assert 'data-agent="coding"' in html
    assert 'data-agent="react"' in html
