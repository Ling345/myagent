"""行动解析的转义还原与 Observation 后缀清理（真实运行暴露的两个缺陷）。"""

from __future__ import annotations

from agentcode.core.parsing import parse_action, parse_finish, parse_react_output
from agentcode.tools import ToolRegistry
from agentcode.tools.base import parse_kwargs


def test_quoted_value_unescapes_newlines():
    kwargs = parse_kwargs('path="a.py", content="line1\\nline2"')
    assert kwargs == {"path": "a.py", "content": "line1\nline2"}


def test_quoted_value_unescapes_tabs_and_quotes():
    kwargs = parse_kwargs('content="a\\tb\\"c\\\\d"')
    assert kwargs == {"content": 'a\tb"c\\d'}


def test_unquoted_windows_path_keeps_backslashes():
    kwargs = parse_kwargs("path=traces\\new\\file.py")
    assert kwargs == {"path": "traces\\new\\file.py"}


def test_split_ignores_commas_inside_quotes():
    kwargs = parse_kwargs('code="print(1, 2)", timeout_seconds=5')
    assert kwargs == {"code": "print(1, 2)", "timeout_seconds": "5"}


def test_write_file_gets_real_newlines():
    registry = ToolRegistry()
    written: dict[str, str] = {}

    def fake_write(path: str, content: str = "") -> str:
        written["path"] = path
        written["content"] = content
        return "ok"

    registry.register_tool("write_file", "写文件", fake_write)
    registry.invoke("write_file", 'path="m.py", content="def f():\\n    return 1\\n"')

    assert written["content"] == "def f():\n    return 1\n"
    assert written["content"].count("\n") == 2


def test_action_observation_suffix_is_stripped():
    text = 'Thought: 读一下\nAction: read_file[path="a.py"]\nObservation: 已读取 a.py（12 字符）'
    thought, action = parse_react_output(text)

    assert thought == "读一下"
    assert action == 'read_file[path="a.py"]'
    assert parse_action(action) == ("read_file", 'path="a.py"')


def test_action_observation_suffix_with_chinese_label():
    text = 'Thought: 想想\nAction: run_python[code="print(1)"]\n观察：退出码：0'
    assert parse_react_output(text)[1] == 'run_python[code="print(1)"]'


def test_action_without_suffix_is_unchanged():
    text = "Thought: 查\nAction: search[北京天气]"
    assert parse_react_output(text)[1] == "search[北京天气]"


def test_action_stops_at_repeated_thought_block():
    """真实故障：一次回复里写了第二组 Thought/Action，参数被污染。"""
    text = (
        'Thought: 读文件\nAction: read_file[path="test_prime.py"]\n'
        'Thought: 再看一眼实现\nAction: read_file[path="prime.py"]'
    )
    assert parse_react_output(text)[1] == 'read_file[path="test_prime.py"]'


def test_action_stops_at_repeated_action_line():
    text = (
        'Thought: 写文件\nAction: write_file[path="a.py", content="x"]\n'
        'Action: run_python[code="print(1)"]'
    )
    assert parse_react_output(text)[1] == 'write_file[path="a.py", content="x"]'


def test_action_keeps_multiline_code_value():
    """多行代码本身不该被误切。"""
    text = 'Thought: 写实现\nAction: write_file[path="a.py", content="def f():\n    return 1\n"]'
    parsed = parse_react_output(text)[1]
    assert parsed.endswith('return 1\n"]')


def test_action_with_prompt_label_prefix():
    """真实故障：模型把提示词里的条目文字当成了前缀。"""
    assert parse_action('调用工具：write_file[path="a.py"]') == ("write_file", 'path="a.py"')
    assert parse_action("给出最终答案：Finish[完成]") == ("Finish", "完成")
    assert parse_finish("给出最终答案：Finish[完成]") == "完成"


def test_action_label_prefix_in_react_output():
    text = 'Thought: 写文件\nAction: 调用工具：write_file[path="a.py", content="x"]'
    assert parse_react_output(text)[1] == 'write_file[path="a.py", content="x"]'
