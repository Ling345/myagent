"""解析函数测试。"""

from __future__ import annotations

from agentcode.core.parsing import parse_action, parse_finish, parse_plan, parse_react_output


def test_parse_react_output_extracts_thought_and_action():
    text = "Thought: 需要查天气\nAction: Search[北京天气]"
    assert parse_react_output(text) == ("需要查天气", "Search[北京天气]")


def test_parse_react_output_tolerates_multiline_thought():
    text = "Thought: 第一行\n第二行\nAction: Finish[完成]"
    thought, action = parse_react_output(text)
    assert thought == "第一行\n第二行"
    assert action == "Finish[完成]"


def test_parse_react_output_without_action_returns_none():
    assert parse_react_output("只是一段普通回答") == (None, None)


def test_parse_action_supports_bracket_form():
    assert parse_action("Search[北京天气]") == ("Search", "北京天气")


def test_parse_action_supports_parentheses_form():
    assert parse_action('Search(query="北京天气")') == ("Search", 'query="北京天气"')


def test_parse_action_supports_bare_name():
    assert parse_action("Finish") == ("Finish", "")


def test_parse_action_strips_trailing_code_fence():
    assert parse_action("Search[北京天气]\n```") == ("Search", "北京天气")


def test_parse_finish_from_bracket_form():
    assert parse_finish("Finish[北京晴，适合去故宫]") == "北京晴，适合去故宫"


def test_parse_finish_from_function_form():
    assert parse_finish('finish(answer="完成啦")') == "完成啦"


def test_parse_finish_returns_none_for_other_actions():
    assert parse_finish("Search[北京天气]") is None


def test_parse_plan_extracts_python_list_from_fence():
    assert parse_plan('说明\n```python\n["第一步", "第二步"]\n```') == ["第一步", "第二步"]


def test_parse_plan_falls_back_to_lines():
    text = "1. 查天气\n2. 推荐景点\n- 输出结论"
    assert parse_plan(text) == ["查天气", "推荐景点", "输出结论"]


def test_parse_plan_returns_empty_list_on_garbage():
    assert parse_plan("") == []
