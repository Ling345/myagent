"""接续文档（`docs/state.md`）不许过期。

这份文件是"新对话接手项目"的第一读物，它一旦过时，接手的人就会照着错的
版本号/清单干活。所以用测试守着最容易被忽略、也最要命的那个数字。
"""

from __future__ import annotations

from pathlib import Path

from agentcode.storage.migrations import MIGRATIONS

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "state.md"


def test_state_doc_exists_and_is_linked_from_the_readme():
    assert DOC.is_file()
    assert "docs/state.md" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_state_doc_declares_the_current_schema_version():
    """加了迁移却忘了改这份文档 → 这条会红。"""
    latest = max(migration.version for migration in MIGRATIONS)
    text = DOC.read_text(encoding="utf-8")
    assert f"**v{latest}**" in text


def test_state_doc_tells_the_next_session_how_to_start():
    """开头就要写清楚"新对话第一句该说什么"，不然它只是另一份没人读的文档。"""
    first_lines = "\n".join(DOC.read_text(encoding="utf-8").splitlines()[:12])
    assert "新开一个对话" in first_lines
    assert "docs/state.md" in first_lines
