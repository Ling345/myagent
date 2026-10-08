"""网页视觉规范测试：流动浅蓝背景、半透明面板、DeepSeek 式配色与布局。"""

from __future__ import annotations

import re

from agentcode.web.server import STATIC_DIR


def _html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _css() -> str:
    return (STATIC_DIR / "style.css").read_text(encoding="utf-8")


def _js() -> str:
    return (STATIC_DIR / "app.js").read_text(encoding="utf-8")


# ------------------------------------------------------------ 套餐与用量面板


def test_data_panel_has_a_trash_section():
    """回收站要在「我的数据」面板里：删错的东西得有个地方找回来。"""
    html = _html()
    assert 'id="trash-list"' in html
    assert 'id="trash-empty"' in html
    js = _js()
    assert "/api/trash" in js
    assert "/api/trash/restore" in js
    assert "/api/trash/purge" in js
    assert "/api/trash/empty" in js


def test_trash_rows_have_both_actions():
    js = _js()
    assert "renderTrashItem" in js
    assert '"恢复"' in js
    assert '"彻底删除"' in js
    css = " ".join(_css().split())
    assert ".trash-item" in css
    assert ".trash-actions" in css


def test_data_panel_has_a_notification_email_field():
    """邮箱是"出了事找得到人"的唯一通道，页面上要能自己填。"""
    html = _html()
    assert 'id="notify-email"' in html
    assert 'id="notify-save"' in html
    js = _js()
    assert "/api/account/email" in js
    assert "saveNotifyEmail" in js
    assert "loadNotifyEmail" in js
    css = " ".join(_css().split())
    assert ".notify-row" in css


def test_data_panel_has_api_token_management():
    """令牌要在页面上能建、能看到、能吊销——否则用户只能来求运营方。

    单独一个面板：一开始塞进「我的数据」，实测内容超一屏、令牌那块被挤到
    可视区外面去了。
    """
    html = _html()
    assert 'id="token-toggle"' in html
    assert 'id="token-panel"' in html
    assert 'id="token-close"' in html
    assert 'id="token-name"' in html
    assert 'id="token-create"' in html
    assert 'id="token-fresh"' in html
    assert 'id="token-list"' in html
    js = _js()
    assert "/api/tokens/create" in js
    assert "/api/tokens/revoke" in js
    assert "showFreshToken" in js
    # 明文只显示一次这件事必须写在页面上
    assert "只显示这一次" in html
    css = " ".join(_css().split())
    assert ".token-item" in css


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
    """原来写死 58vh，套餐卡片只能挤在一条窄缝里。

    这里**不钉死具体倍数**：面板内容会随功能增加（回收站、通知邮箱……），
    70vh 改到 80vh 是为了别出现内部滚动条。要守的是"按视口封顶 + 不参与收缩"。
    """
    css = " ".join(_css().split())
    assert "max-height: 58vh" not in css
    # 关键：面板不参与收缩，否则会被会话列表挤成一条缝（实测只剩 30px 高）
    assert "flex: 0 0 auto" in css
    assert re.search(r"max-height:\s*\d+vh", css)


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


# ------------------------------------------------------------ 我的数据


def test_data_panel_exists_in_markup():
    html = _html()
    assert 'id="data-panel"' in html
    assert 'id="data-toggle"' in html
    assert 'id="data-export"' in html
    assert 'id="data-purge"' in html
    assert 'id="data-delete"' in html


def test_delete_needs_a_typed_confirmation():
    """注销不可撤销，所以要在界面上真的让用户打一遍用户名。"""
    html = _html()
    assert 'id="data-confirm-input"' in html
    assert 'id="data-confirm-ok"' in html
    assert 'id="data-confirm-name"' in html
    assert "不可撤销" in html


def test_data_panel_script_uses_the_endpoints():
    js = _js()
    assert "/api/export" in js
    assert "/api/account/purge-code" in js
    assert "/api/account/delete" in js


def test_legal_pages_are_linked_from_the_sidebar():
    html = _html()
    assert 'href="/privacy"' in html
    assert 'href="/terms"' in html


def test_post_errors_surface_the_servers_message():
    """回归：postJSON 原来在非 2xx 时直接抛"服务返回了 400"，
    把服务端写好的中文原因（比如"请输入你自己的用户名以确认注销"）丢在半路——
    用户填错名字时页面上什么提示都没有。"""
    js = _js()
    assert "data.error ||" in js
    assert "服务返回了 ${response.status}" in js


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
