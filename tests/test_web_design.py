"""网页视觉规范测试：流动浅蓝背景、半透明面板、DeepSeek 式配色与布局。"""

from __future__ import annotations

from agentcode.web.server import STATIC_DIR


def _html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _css() -> str:
    return (STATIC_DIR / "style.css").read_text(encoding="utf-8")


def _js() -> str:
    return (STATIC_DIR / "app.js").read_text(encoding="utf-8")


# ------------------------------------------------------------ 套餐与用量面板


def test_billing_panel_exists_in_markup():
    html = _html()
    assert 'id="billing-panel"' in html
    assert 'id="billing-toggle"' in html
    assert 'id="billing-bar-fill"' in html
    assert 'id="billing-orders"' in html


def test_billing_panel_is_hidden_until_opened():
    """面板默认收起——主界面只给结果，这是既有约定。"""
    css = " ".join(_css().split())
    assert "#billing-panel[hidden] { display: none; }" in css


def test_billing_panel_script_uses_the_new_endpoints():
    js = _js()
    assert "/api/billing" in js
    assert "/api/plans" in js
    assert "/api/billing/checkout" in js


def test_account_quota_shows_unlimited():
    js = _js()
    assert "unlimited" in js


# -------------------------------------------------------- 侧栏折叠与拖拽调宽


def test_agent_section_can_be_collapsed():
    html = _html()
    assert 'id="agent-toggle"' in html
    assert 'aria-controls="agent-list"' in html
    assert 'aria-expanded="true"' in html
    js = _js()
    assert "agentcode-agents-collapsed" in js
    assert "setAgentsCollapsed" in js


def test_sidebar_width_is_draggable():
    html = _html()
    assert 'id="sidebar-resizer"' in html
    assert 'role="separator"' in html
    css = " ".join(_css().split())
    assert "--sidebar-width" in css
    assert "cursor: col-resize" in css
    js = _js()
    assert "agentcode-sidebar-width" in js
    assert "pointermove" in js


def test_sidebar_width_is_clamped():
    """拖到离谱的宽度要兜住，否则侧栏会把聊天区挤没。"""
    js = _js()
    assert "MIN_SIDEBAR_WIDTH = 240" in js
    assert "MAX_SIDEBAR_WIDTH = 420" in js
    assert "clampSidebarWidth" in js


def test_billing_panel_is_no_longer_height_capped():
    """原来写死 58vh，套餐卡片只能挤在一条窄缝里。"""
    css = " ".join(_css().split())
    assert "max-height: 58vh" not in css
    # 关键：面板不参与收缩，否则会被会话列表挤成一条缝（实测只剩 30px 高）
    assert "flex: 0 0 auto" in css
    assert "max-height: 70vh" in css


def test_resizer_is_hidden_on_narrow_screens():
    css = " ".join(_css().split())
    assert "@media (max-width: 880px)" in css
    narrow = css.split("@media (max-width: 880px)", 1)[1]
    assert ".sidebar-resizer { display: none; }" in narrow


# ------------------------------------------------------------ 上传文件


def test_upload_controls_exist_in_markup():
    html = _html()
    assert 'id="upload-button"' in html
    assert 'id="upload-input"' in html
    assert 'id="upload-chips"' in html
    assert 'type="file"' in html


def test_upload_script_uses_the_endpoint_and_supports_drop():
    js = _js()
    assert "/api/upload?path=" in js
    assert "uploadFiles" in js
    assert "dragover" in js
    assert "drop" in js


def test_upload_chip_opens_the_existing_viewer():
    """上传后点标签就能看内容，复用已有的查看器，不另造一个。"""
    js = _js()
    assert "addUploadChip" in js
    assert "openViewer(path, bytes)" in js


def test_upload_chips_are_hidden_until_something_is_uploaded():
    css = " ".join(_css().split())
    assert ".upload-chips[hidden] { display: none; }" in css


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


def test_sidebar_has_no_model_panel():
    """模型信息与它下面那一整块已经移除，腾出的高度留给会话列表。"""
    html = _html()
    assert 'id="config-list"' not in html
    assert 'id="session-hint"' not in html
    assert 'id="export-button"' not in html
    assert "sidebar-foot" not in html


def test_session_list_fills_freed_space():
    css = _css()
    assert "flex: 1 1 auto" in css  # 会占据侧栏剩余高度
    assert "max-height: none" in css  # 不再被人为限高


def test_message_area_is_height_constrained():
    """回归：grid 行高必须写死且子项允许收缩，否则内容会撑破 100vh、
    多出来的部分既看不见也滚不到（真实故障：同一问题多问几次后滑不到底）。"""
    css = _css()
    assert "grid-template-rows: 100vh" in css
    assert "min-height: 0" in css


def test_hidden_attribute_always_hides():
    """回归：.login{display:flex} 曾盖过 hidden 的默认 display:none，
    登录框一直蒙在应用上面（点任何按钮都被它拦截）。"""
    css = _css()
    assert "[hidden]" in css
    assert "display: none !important" in css
