"""配置加载：优先级为 显式传入的 .env → 向上查找的 .env → 进程环境变量。

密钥只从这里流出，源码中不出现任何密钥字面量。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from dotenv import dotenv_values

from agentcode.core.errors import ConfigError

#: 必需的环境变量
REQUIRED_LLM_KEYS: tuple[str, ...] = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL_ID")

DEFAULT_TIMEOUT = 60.0
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_STEPS = 6
DEFAULT_TRACE_DIR = "traces"
ENV_FILE_NAME = ".env"
DEFAULT_SEARCH_LEVELS = 3

#: 上下文记忆与网页会话的默认值（须与 core.agent / web.sessions 的默认值一致，
#: 由 tests/test_memory_config.py 守住）
DEFAULT_MEMORY_TURNS = 5
DEFAULT_MAX_SESSIONS = 20

#: 受限代码执行的默认值
DEFAULT_CODE_ROOT = "traces/sandbox"
DEFAULT_CODE_TIMEOUT = 10.0
DEFAULT_CODE_OUTPUT_LIMIT = 4000
#: 写代码需要比闲聊更多的步数（须与 agents.coding 的默认值一致）
DEFAULT_CODING_STEPS = 20


def mask_secret(value: str | None) -> str:
    """对密钥做脱敏展示：保留首尾各 4 位。"""
    if not value:
        return "（未配置）"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 6}{value[-4:]}"


def find_env_file(start: Path | None = None, max_levels: int = DEFAULT_SEARCH_LEVELS) -> Path | None:
    """从 ``start`` 目录起向上查找 ``.env``，最多上溯 ``max_levels`` 层。"""
    current = Path(start or Path.cwd()).resolve()
    for _ in range(max_levels + 1):
        candidate = current / ENV_FILE_NAME
        if candidate.is_file():
            return candidate
        if current.parent == current:
            break
        current = current.parent
    return None


def _to_float(value: Any, default: float) -> float:
    """把配置值转换为浮点数，非法值回退到默认值。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int) -> int:
    """把配置值转换为整数，非法值回退到默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any, default: bool) -> bool:
    """把配置值转换为布尔值，非法值回退到默认值。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class Settings:
    """框架运行所需的全部配置。"""

    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    temperature: float = DEFAULT_TEMPERATURE
    stream: bool = True
    serpapi_key: str | None = None
    max_steps: int = DEFAULT_MAX_STEPS
    trace_dir: str = DEFAULT_TRACE_DIR
    memory_turns: int = DEFAULT_MEMORY_TURNS
    max_sessions: int = DEFAULT_MAX_SESSIONS
    code_root: str = DEFAULT_CODE_ROOT
    code_timeout: float = DEFAULT_CODE_TIMEOUT
    code_output_limit: int = DEFAULT_CODE_OUTPUT_LIMIT
    coding_steps: int = DEFAULT_CODING_STEPS
    env_file: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ 构造

    @classmethod
    def from_env(
        cls,
        env_file: str | None = None,
        search_parents: bool = True,
    ) -> "Settings":
        """从 ``.env`` 与进程环境变量构造配置。

        ``env_file`` 指向的文件不存在时静默忽略，以便测试与 CI 运行；
        真正的缺失在 :meth:`validate` 中报错。
        """
        resolved: Path | None = None
        if env_file:
            candidate = Path(env_file)
            resolved = candidate if candidate.is_file() else None
        elif search_parents:
            resolved = find_env_file()

        file_values: dict[str, str] = {}
        if resolved is not None:
            file_values = {
                key: value for key, value in dotenv_values(resolved).items() if value is not None
            }

        def pick(key: str, default: Any = None) -> Any:
            """进程环境变量优先于 .env 文件。"""
            return os.environ.get(key) or file_values.get(key) or default

        return cls(
            model=pick("LLM_MODEL_ID"),
            api_key=pick("LLM_API_KEY"),
            base_url=pick("LLM_BASE_URL"),
            timeout=_to_float(pick("LLM_TIMEOUT"), DEFAULT_TIMEOUT),
            temperature=_to_float(pick("LLM_TEMPERATURE"), DEFAULT_TEMPERATURE),
            stream=_to_bool(pick("LLM_STREAM"), True),
            serpapi_key=pick("SERPAPI_API_KEY"),
            max_steps=_to_int(pick("AGENT_MAX_STEPS"), DEFAULT_MAX_STEPS),
            trace_dir=str(pick("AGENT_TRACE_DIR", DEFAULT_TRACE_DIR)),
            memory_turns=_to_int(pick("AGENT_MEMORY_TURNS"), DEFAULT_MEMORY_TURNS),
            max_sessions=_to_int(pick("AGENT_MAX_SESSIONS"), DEFAULT_MAX_SESSIONS),
            code_root=str(pick("AGENT_CODE_ROOT", DEFAULT_CODE_ROOT)),
            code_timeout=_to_float(pick("AGENT_CODE_TIMEOUT"), DEFAULT_CODE_TIMEOUT),
            code_output_limit=_to_int(pick("AGENT_CODE_OUTPUT_LIMIT"), DEFAULT_CODE_OUTPUT_LIMIT),
            coding_steps=_to_int(pick("AGENT_CODING_STEPS"), DEFAULT_CODING_STEPS),
            env_file=str(resolved) if resolved else None,
        )

    def apply_overrides(self, overrides: Mapping[str, Any]) -> "Settings":
        """按 JSON 配置文件内容覆盖已有字段，未知字段进入 ``extra``。"""
        known = {f for f in self.__dataclass_fields__ if f not in {"extra", "env_file"}}
        updates: dict[str, Any] = {}
        extra = dict(self.extra)
        for key, value in overrides.items():
            if key in known:
                updates[key] = value
            else:
                extra[key] = value
        merged = replace(self, **updates)
        merged.extra = extra
        return merged

    # ------------------------------------------------------------------ 校验

    def missing_keys(self) -> list[str]:
        """返回尚未配置的必需环境变量名。"""
        values = {
            "LLM_API_KEY": self.api_key,
            "LLM_BASE_URL": self.base_url,
            "LLM_MODEL_ID": self.model,
        }
        return [key for key in REQUIRED_LLM_KEYS if not values.get(key)]

    def validate(self) -> "Settings":
        """校验必需配置，缺失时抛出带中文提示的 :class:`ConfigError`。"""
        missing = self.missing_keys()
        if missing:
            raise ConfigError(
                "缺少大语言模型配置："
                + "、".join(missing)
                + "。请在 .env 中配置，或参考 .env.example。"
            )
        limits = {
            "LLM_TIMEOUT": self.timeout,
            "AGENT_MAX_STEPS": self.max_steps,
            "AGENT_MEMORY_TURNS": self.memory_turns,
            "AGENT_MAX_SESSIONS": self.max_sessions,
            "AGENT_CODE_TIMEOUT": self.code_timeout,
            "AGENT_CODE_OUTPUT_LIMIT": self.code_output_limit,
            "AGENT_CODING_STEPS": self.coding_steps,
        }
        for name, value in limits.items():
            if value <= 0:
                raise ConfigError(f"{name} 必须大于 0。")
        return self

    def max_steps_for(self, agent_name: str) -> int:
        """按智能体选择步数上限：写代码比闲聊需要更多步。"""
        return self.coding_steps if agent_name == "coding" else self.max_steps

    # ------------------------------------------------------------------ 展示

    def masked(self) -> dict[str, str]:
        """返回可安全打印的配置摘要。"""
        return {
            "LLM_MODEL_ID": self.model or "（未配置）",
            "LLM_BASE_URL": self.base_url or "（未配置）",
            "LLM_API_KEY": mask_secret(self.api_key),
            "SERPAPI_API_KEY": mask_secret(self.serpapi_key),
            "LLM_TIMEOUT": str(self.timeout),
            "LLM_TEMPERATURE": str(self.temperature),
            "AGENT_MAX_STEPS": str(self.max_steps),
            "AGENT_CODING_STEPS": str(self.coding_steps),
            "AGENT_MEMORY_TURNS": str(self.memory_turns),
            "AGENT_MAX_SESSIONS": str(self.max_sessions),
            "AGENT_CODE_ROOT": self.code_root,
            "AGENT_CODE_TIMEOUT": str(self.code_timeout),
            "AGENT_TRACE_DIR": self.trace_dir,
            ".env 来源": self.env_file or "（未找到，使用进程环境变量）",
        }
