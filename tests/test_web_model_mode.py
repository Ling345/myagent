"""网页不再提供离线演示选项：页面一律走服务端配置的模型。"""

from __future__ import annotations

from agentcode.cli import build_parser, _web_target
from agentcode.config import Settings
from agentcode.web.server import STATIC_DIR


def test_index_page_has_no_offline_option():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "离线演示" not in html
    assert "新会话" in html


def test_index_page_has_no_model_switch():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'name="llm"' not in html


def test_app_js_does_not_send_llm_mode():
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'data.get("llm")' not in script
    assert "llm:" not in script


def test_web_command_defaults_to_real_model():
    args = build_parser().parse_args(["web"])
    _, _, llm_mode = _web_target(args, Settings())
    assert llm_mode == "openai"


def test_open_command_defaults_to_real_model():
    args = build_parser().parse_args(["open"])
    _, _, llm_mode = _web_target(args, Settings())
    assert llm_mode == "openai"


def test_llm_flag_still_can_ask_for_offline_mode():
    args = build_parser().parse_args(["web", "--llm", "mock"])
    _, _, llm_mode = _web_target(args, Settings())
    assert llm_mode == "mock"
