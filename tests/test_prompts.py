"""提示词里的输出风格约束：答案要干净。"""

from __future__ import annotations

from agentcode.agents.prompts import CODING_SYSTEM_PROMPT, REACT_SYSTEM_PROMPT


def test_react_prompt_forbids_links_and_sources():
    assert "不要列出网址" in REACT_SYSTEM_PROMPT
    assert "来源" in REACT_SYSTEM_PROMPT


def test_coding_prompt_forbids_test_report():
    assert "不要提测试" in CODING_SYSTEM_PROMPT
    assert "通过与否" in CODING_SYSTEM_PROMPT
    assert "stdout" in CODING_SYSTEM_PROMPT


def test_coding_prompt_still_requires_running_tests():
    """答案里不报测试结果，但流程上仍然必须真的跑测试。"""
    assert "必须真的运行一次" in CODING_SYSTEM_PROMPT
    assert "测试全部通过" in CODING_SYSTEM_PROMPT
