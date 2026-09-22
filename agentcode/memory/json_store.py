"""把运行轨迹写入 / 读出 JSON 文件。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from agentcode.core.errors import AgentCodeError


class JsonStore:
    """极简 JSON 读写器，自动创建父目录。"""

    @staticmethod
    def save(payload: Mapping[str, Any], path: str | Path) -> Path:
        """把字典写入 JSON 文件，返回实际路径。"""
        target = Path(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            raise AgentCodeError(f"写入文件失败：{target}（{exc}）") from exc
        return target

    @staticmethod
    def load(path: str | Path) -> dict[str, Any]:
        """读取 JSON 文件并返回字典。"""
        target = Path(path)
        try:
            return json.loads(target.read_text(encoding="utf-8"))
        except OSError as exc:
            raise AgentCodeError(f"读取文件失败：{target}（{exc}）") from exc
        except json.JSONDecodeError as exc:
            raise AgentCodeError(f"文件不是合法的 JSON：{target}（{exc}）") from exc
