"""行动解析的转义还原与 Observation 后缀清理（真实运行暴露的两个缺陷）。"""

from __future__ import annotations

from agentcode.core.parsing import parse_action, parse_react_output
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
