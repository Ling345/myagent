"""按已知参数名切分参数：容忍模型写出的未转义引号与逗号。"""

from __future__ import annotations

from agentcode.tools import ToolRegistry
from agentcode.tools.base import parse_known_kwargs


def test_slices_value_with_unescaped_quotes():
    raw = 'path="t.py", content="import pytest\ndef test_x():\n    assert f("a", "b")"'
    kwargs = parse_known_kwargs(raw, {"path", "content"})

    assert kwargs["path"] == "t.py"
    assert kwargs["content"].startswith("import pytest")
    assert '"a", "b"' in kwargs["content"]


def test_slices_value_with_commas():
    raw = 'path="m.py", content="def f(a, b):\\n    return a + b\\n"'
    kwargs = parse_known_kwargs(raw, {"path", "content"})
    assert kwargs["content"] == "def f(a, b):\n    return a + b\n"


def test_returns_none_without_known_keys():
    assert parse_known_kwargs("只是一段普通输入", {"path"}) is None


def test_returns_none_without_keys():
    assert parse_known_kwargs('path="a.py"', set()) is None


def test_registry_falls_back_to_known_keys():
    """真实故障场景：内容里有未转义引号时，写入仍然要成功。"""
    registry = ToolRegistry()
    written: dict[str, str] = {}

    def write_file(path: str, content: str = "") -> str:
        written["path"] = path
        written["content"] = content
        return "ok"

    registry.register_tool(
        "write_file",
        "写文件",
        write_file,
        {"path": "路径", "content": "内容"},
    )
    registry.invoke(
        "write_file",
        'path="test_is_prime.py", content="\n@pytest.mark.parametrize("n, expected", [\n    (2, True),\n])\n',
    )

    assert written["path"] == "test_is_prime.py"
    assert '@pytest.mark.parametrize("n, expected"' in written["content"]


def test_registry_still_supports_positional_input():
    registry = ToolRegistry()
    registry.register_tool("echo", "回显", lambda text: f"收到：{text}", {"text": "内容"})
    assert registry.invoke("echo", "北京天气") == "收到：北京天气"
