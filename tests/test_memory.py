"""记忆模块测试。"""

from __future__ import annotations

import pytest

from agentcode.core.errors import AgentCodeError
from agentcode.memory import JsonStore, ShortTermMemory


def test_short_term_memory_keeps_only_recent_turns():
    memory = ShortTermMemory(max_turns=2)
    for index in range(3):
        memory.add_turn([{"role": "user", "content": f"第{index}轮"}])
    assert len(memory) == 2
    assert [message["content"] for message in memory.get_messages()] == ["第1轮", "第2轮"]


def test_short_term_memory_flattens_messages_in_order():
    memory = ShortTermMemory()
    memory.add_turn([{"role": "user", "content": "问题"}])
    memory.add_turn([{"role": "assistant", "content": "回答"}])
    assert memory.get_messages() == [
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "回答"},
    ]


def test_short_term_memory_clear():
    memory = ShortTermMemory()
    memory.add_turn([{"role": "user", "content": "问题"}])
    memory.clear()
    assert memory.get_messages() == []


def test_short_term_memory_rejects_invalid_max_turns():
    with pytest.raises(ValueError):
        ShortTermMemory(max_turns=0)


def test_json_store_roundtrip(tmp_path):
    path = tmp_path / "nested" / "trace.json"
    JsonStore.save({"agent": "react", "答案": "好的"}, path)
    loaded = JsonStore.load(path)
    assert loaded["agent"] == "react"
    assert loaded["答案"] == "好的"


def test_json_store_reports_missing_file(tmp_path):
    with pytest.raises(AgentCodeError):
        JsonStore.load(tmp_path / "不存在.json")
