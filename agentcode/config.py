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
from agentcode.llm.keypool import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_FAILURE_THRESHOLD,
    mask_key,
    parse_keys,
)

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
#: 网页会话落盘目录（须与 web.sessions 的默认值一致）
DEFAULT_WEB_SESSION_DIR = "traces/web-sessions"

#: 受限代码执行的默认值
DEFAULT_CODE_ROOT = "traces/sandbox"
DEFAULT_CODE_TIMEOUT = 10.0
DEFAULT_CODE_OUTPUT_LIMIT = 4000
#: 代码执行后端：local=本机受限直跑（非沙箱），docker=一次性容器隔离
DEFAULT_EXECUTION_BACKEND = "local"
DEFAULT_DOCKER_IMAGE = "python:3.13-slim"
#: docker 命令本身；装了 Docker Desktop 就是 docker，用 WSL 里的 docker 可写
#: "wsl -d Ubuntu -- docker"
DEFAULT_DOCKER_BINARY = "docker"
DEFAULT_DOCKER_MEMORY = "256m"
DEFAULT_DOCKER_CPUS = "0.5"
DEFAULT_DOCKER_PIDS_LIMIT = 64
#: 容器内跑代码用的用户；留空则不传 --user（以镜像默认用户运行）
DEFAULT_DOCKER_USER = "65534:65534"
#: 写代码需要比闲聊更多的步数（须与 agents.coding 的默认值一致）
DEFAULT_CODING_STEPS = 20

#: 账号与配额（收费产品的地基）
DEFAULT_DB_PATH = "traces/agentcode.db"
DEFAULT_DAILY_TOKEN_LIMIT = 50_000
#: 频率与并发闸门：防止单用户打满服务
DEFAULT_RATE_LIMIT_PER_MINUTE = 30
DEFAULT_MAX_CONCURRENT_RUNS = 2
#: 单次运行的 token 上限（超出中断，避免一个任务吃掉整天额度）
DEFAULT_RUN_TOKEN_BUDGET = 30_000
#: 面向公网时默认不允许执行代码；本地 CLI 使用不受影响
DEFAULT_ALLOW_CODE_TOOLS = False
#: API key 熔断：连续失败几次摘掉、冷却多久放回来
DEFAULT_KEY_FAILURE_THRESHOLD = DEFAULT_FAILURE_THRESHOLD
DEFAULT_KEY_COOLDOWN_SECONDS = DEFAULT_COOLDOWN_SECONDS
#: 告警：窗口多长、同一规则多久不重复推、错误率阈值
DEFAULT_ALERT_WINDOW_SECONDS = 300.0
DEFAULT_ALERT_COOLDOWN_SECONDS = 900.0
DEFAULT_ALERT_ERROR_RATE = 0.5
#: 文件上传：单个文件上限、每个用户工作目录总上限
DEFAULT_UPLOAD_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_UPLOAD_QUOTA_BYTES = 50 * 1024 * 1024
#: 自动清理：会话与代码文件留多久。0 = 不自动清理（默认，删不删由用户自己决定）
DEFAULT_RETENTION_DAYS = 0
#: 模型单价（分 / 百万 token）。默认 0 = 没配，那就算不了钱，只能看 token
DEFAULT_PRICE_INPUT_PER_MILLION = 0
DEFAULT_PRICE_OUTPUT_PER_MILLION = 0


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
    #: 多个 key（``LLM_API_KEYS``）；为空时回落到 ``api_key`` 单个
    api_keys: list[str] = field(default_factory=list)
    base_url: str | None = None
    timeout: float = DEFAULT_TIMEOUT
    temperature: float = DEFAULT_TEMPERATURE
    stream: bool = True
    serpapi_key: str | None = None
    max_steps: int = DEFAULT_MAX_STEPS
    trace_dir: str = DEFAULT_TRACE_DIR
    memory_turns: int = DEFAULT_MEMORY_TURNS
    max_sessions: int = DEFAULT_MAX_SESSIONS
    web_session_dir: str = DEFAULT_WEB_SESSION_DIR
    code_root: str = DEFAULT_CODE_ROOT
    code_timeout: float = DEFAULT_CODE_TIMEOUT
    code_output_limit: int = DEFAULT_CODE_OUTPUT_LIMIT
    execution_backend: str = DEFAULT_EXECUTION_BACKEND
    docker_image: str = DEFAULT_DOCKER_IMAGE
    docker_binary: str = DEFAULT_DOCKER_BINARY
    docker_memory: str = DEFAULT_DOCKER_MEMORY
    docker_cpus: str = DEFAULT_DOCKER_CPUS
    docker_pids_limit: int = DEFAULT_DOCKER_PIDS_LIMIT
    docker_user: str = DEFAULT_DOCKER_USER
    coding_steps: int = DEFAULT_CODING_STEPS
    db_path: str = DEFAULT_DB_PATH
    secret_key: str | None = None
    daily_token_limit: int = DEFAULT_DAILY_TOKEN_LIMIT
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE
    max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS
    run_token_budget: int = DEFAULT_RUN_TOKEN_BUDGET
    allow_code_tools: bool = DEFAULT_ALLOW_CODE_TOOLS
    key_failure_threshold: int = DEFAULT_KEY_FAILURE_THRESHOLD
    key_cooldown_seconds: float = DEFAULT_KEY_COOLDOWN_SECONDS
    #: 告警 webhook；为空表示只在日志里报，不往外推
    alert_webhook: str | None = None
    alert_window_seconds: float = DEFAULT_ALERT_WINDOW_SECONDS
    alert_cooldown_seconds: float = DEFAULT_ALERT_COOLDOWN_SECONDS
    alert_error_rate: float = DEFAULT_ALERT_ERROR_RATE
    upload_max_bytes: int = DEFAULT_UPLOAD_MAX_BYTES
    upload_quota_bytes: int = DEFAULT_UPLOAD_QUOTA_BYTES
    #: 抓指标用的令牌；设了之后 /metrics 允许带它免登录访问
    metrics_token: str | None = None
    #: 自动清理：会话与代码文件留多久。0 = 不自动清理
    retention_days: int = DEFAULT_RETENTION_DAYS
    #: 模型单价（分 / 百万 token）。0 = 未配置
    price_input_per_million: int = DEFAULT_PRICE_INPUT_PER_MILLION
    price_output_per_million: int = DEFAULT_PRICE_OUTPUT_PER_MILLION
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

        # LLM_API_KEYS 支持逗号/换行分隔的多个 key；没配就回落到单个 LLM_API_KEY
        api_keys = parse_keys(pick("LLM_API_KEYS") or pick("LLM_API_KEY"))

        return cls(
            model=pick("LLM_MODEL_ID"),
            api_key=api_keys[0] if api_keys else pick("LLM_API_KEY"),
            api_keys=api_keys,
            base_url=pick("LLM_BASE_URL"),
            timeout=_to_float(pick("LLM_TIMEOUT"), DEFAULT_TIMEOUT),
            temperature=_to_float(pick("LLM_TEMPERATURE"), DEFAULT_TEMPERATURE),
            stream=_to_bool(pick("LLM_STREAM"), True),
            serpapi_key=pick("SERPAPI_API_KEY"),
            max_steps=_to_int(pick("AGENT_MAX_STEPS"), DEFAULT_MAX_STEPS),
            trace_dir=str(pick("AGENT_TRACE_DIR", DEFAULT_TRACE_DIR)),
            memory_turns=_to_int(pick("AGENT_MEMORY_TURNS"), DEFAULT_MEMORY_TURNS),
            max_sessions=_to_int(pick("AGENT_MAX_SESSIONS"), DEFAULT_MAX_SESSIONS),
            web_session_dir=str(pick("AGENT_WEB_SESSION_DIR", DEFAULT_WEB_SESSION_DIR)),
            code_root=str(pick("AGENT_CODE_ROOT", DEFAULT_CODE_ROOT)),
            code_timeout=_to_float(pick("AGENT_CODE_TIMEOUT"), DEFAULT_CODE_TIMEOUT),
            code_output_limit=_to_int(pick("AGENT_CODE_OUTPUT_LIMIT"), DEFAULT_CODE_OUTPUT_LIMIT),
            execution_backend=str(
                pick("AGENT_EXECUTION_BACKEND", DEFAULT_EXECUTION_BACKEND)
            ).strip().lower(),
            docker_image=str(pick("AGENT_DOCKER_IMAGE", DEFAULT_DOCKER_IMAGE)),
            docker_binary=str(pick("AGENT_DOCKER_BINARY", DEFAULT_DOCKER_BINARY)),
            docker_memory=str(pick("AGENT_DOCKER_MEMORY", DEFAULT_DOCKER_MEMORY)),
            docker_cpus=str(pick("AGENT_DOCKER_CPUS", DEFAULT_DOCKER_CPUS)),
            docker_pids_limit=_to_int(
                pick("AGENT_DOCKER_PIDS_LIMIT"), DEFAULT_DOCKER_PIDS_LIMIT
            ),
            docker_user=str(pick("AGENT_DOCKER_USER", DEFAULT_DOCKER_USER)),
            coding_steps=_to_int(pick("AGENT_CODING_STEPS"), DEFAULT_CODING_STEPS),
            db_path=str(pick("AGENT_DB_PATH", DEFAULT_DB_PATH)),
            secret_key=pick("AGENT_SECRET_KEY"),
            daily_token_limit=_to_int(pick("AGENT_DAILY_TOKEN_LIMIT"), DEFAULT_DAILY_TOKEN_LIMIT),
            rate_limit_per_minute=_to_int(
                pick("AGENT_RATE_LIMIT_PER_MINUTE"), DEFAULT_RATE_LIMIT_PER_MINUTE
            ),
            max_concurrent_runs=_to_int(
                pick("AGENT_MAX_CONCURRENT_RUNS"), DEFAULT_MAX_CONCURRENT_RUNS
            ),
            run_token_budget=_to_int(pick("AGENT_RUN_TOKEN_BUDGET"), DEFAULT_RUN_TOKEN_BUDGET),
            allow_code_tools=_to_bool(pick("AGENT_ALLOW_CODE_TOOLS"), DEFAULT_ALLOW_CODE_TOOLS),
            key_failure_threshold=_to_int(
                pick("AGENT_KEY_FAILURE_THRESHOLD"), DEFAULT_KEY_FAILURE_THRESHOLD
            ),
            key_cooldown_seconds=_to_float(
                pick("AGENT_KEY_COOLDOWN_SECONDS"), DEFAULT_KEY_COOLDOWN_SECONDS
            ),
            alert_webhook=pick("AGENT_ALERT_WEBHOOK"),
            alert_window_seconds=_to_float(
                pick("AGENT_ALERT_WINDOW_SECONDS"), DEFAULT_ALERT_WINDOW_SECONDS
            ),
            alert_cooldown_seconds=_to_float(
                pick("AGENT_ALERT_COOLDOWN_SECONDS"), DEFAULT_ALERT_COOLDOWN_SECONDS
            ),
            alert_error_rate=_to_float(
                pick("AGENT_ALERT_ERROR_RATE"), DEFAULT_ALERT_ERROR_RATE
            ),
            upload_max_bytes=_to_int(
                pick("AGENT_UPLOAD_MAX_BYTES"), DEFAULT_UPLOAD_MAX_BYTES
            ),
            upload_quota_bytes=_to_int(
                pick("AGENT_UPLOAD_QUOTA_BYTES"), DEFAULT_UPLOAD_QUOTA_BYTES
            ),
            metrics_token=pick("AGENT_METRICS_TOKEN"),
            retention_days=_to_int(pick("AGENT_RETENTION_DAYS"), DEFAULT_RETENTION_DAYS),
            price_input_per_million=_to_int(
                pick("AGENT_PRICE_INPUT_PER_MILLION"), DEFAULT_PRICE_INPUT_PER_MILLION
            ),
            price_output_per_million=_to_int(
                pick("AGENT_PRICE_OUTPUT_PER_MILLION"), DEFAULT_PRICE_OUTPUT_PER_MILLION
            ),
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

    def api_key_list(self) -> list[str]:
        """返回全部可用的模型 key：``LLM_API_KEYS`` 优先，回落到单个 ``LLM_API_KEY``。"""
        keys = [key for key in (self.api_keys or []) if key]
        if not keys and self.api_key:
            keys = [self.api_key]
        return keys

    def missing_keys(self) -> list[str]:
        """返回尚未配置的必需环境变量名。"""
        values = {
            "LLM_API_KEY": self.api_key_list() or None,
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
            "AGENT_DAILY_TOKEN_LIMIT": self.daily_token_limit,
            "AGENT_RATE_LIMIT_PER_MINUTE": self.rate_limit_per_minute,
            "AGENT_MAX_CONCURRENT_RUNS": self.max_concurrent_runs,
            "AGENT_RUN_TOKEN_BUDGET": self.run_token_budget,
            "AGENT_KEY_FAILURE_THRESHOLD": self.key_failure_threshold,
            "AGENT_KEY_COOLDOWN_SECONDS": self.key_cooldown_seconds,
        }
        for name, value in limits.items():
            if value <= 0:
                raise ConfigError(f"{name} 必须大于 0。")
        if self.execution_backend not in {"local", "docker"}:
            raise ConfigError(
                f"AGENT_EXECUTION_BACKEND 只支持 local 或 docker，当前是：{self.execution_backend}。"
            )
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
            "LLM_API_KEYS": self._keys_summary(),
            "SERPAPI_API_KEY": mask_secret(self.serpapi_key),
            "LLM_TIMEOUT": str(self.timeout),
            "LLM_TEMPERATURE": str(self.temperature),
            "AGENT_MAX_STEPS": str(self.max_steps),
            "AGENT_CODING_STEPS": str(self.coding_steps),
            "AGENT_DAILY_TOKEN_LIMIT": str(self.daily_token_limit),
            "AGENT_RATE_LIMIT_PER_MINUTE": str(self.rate_limit_per_minute),
            "AGENT_MAX_CONCURRENT_RUNS": str(self.max_concurrent_runs),
            "AGENT_RUN_TOKEN_BUDGET": str(self.run_token_budget),
            "AGENT_ALLOW_CODE_TOOLS": "是" if self.allow_code_tools else "否",
            "AGENT_KEY_FAILURE_THRESHOLD": str(self.key_failure_threshold),
            "AGENT_KEY_COOLDOWN_SECONDS": str(self.key_cooldown_seconds),
            # webhook 的 URL 里通常带一串密钥，按密钥处理
            "AGENT_ALERT_WEBHOOK": mask_secret(self.alert_webhook),
            "AGENT_ALERT_WINDOW_SECONDS": str(self.alert_window_seconds),
            "AGENT_ALERT_COOLDOWN_SECONDS": str(self.alert_cooldown_seconds),
            "AGENT_ALERT_ERROR_RATE": str(self.alert_error_rate),
            "AGENT_UPLOAD_MAX_BYTES": str(self.upload_max_bytes),
            "AGENT_UPLOAD_QUOTA_BYTES": str(self.upload_quota_bytes),
            "AGENT_METRICS_TOKEN": mask_secret(self.metrics_token),
            "AGENT_RETENTION_DAYS": str(self.retention_days) + (
                "（不自动清理）" if self.retention_days <= 0 else ""
            ),
            "AGENT_PRICE_INPUT_PER_MILLION": (
                f"{self.price_input_per_million} 分/百万 token"
                if self.price_input_per_million
                else "（未配置，算不了钱）"
            ),
            "AGENT_PRICE_OUTPUT_PER_MILLION": (
                f"{self.price_output_per_million} 分/百万 token"
                if self.price_output_per_million
                else "（未配置，算不了钱）"
            ),
            "AGENT_SECRET_KEY": mask_secret(self.secret_key),
            "AGENT_DB_PATH": self.db_path,
            "AGENT_MEMORY_TURNS": str(self.memory_turns),
            "AGENT_MAX_SESSIONS": str(self.max_sessions),
            "AGENT_WEB_SESSION_DIR": self.web_session_dir,
            "AGENT_CODE_ROOT": self.code_root,
            "AGENT_CODE_TIMEOUT": str(self.code_timeout),
            "AGENT_EXECUTION_BACKEND": self.execution_backend,
            "AGENT_DOCKER_IMAGE": self.docker_image,
            "AGENT_DOCKER_BINARY": self.docker_binary,
            "AGENT_DOCKER_MEMORY": self.docker_memory,
            "AGENT_DOCKER_CPUS": self.docker_cpus,
            "AGENT_DOCKER_PIDS_LIMIT": str(self.docker_pids_limit),
            "AGENT_TRACE_DIR": self.trace_dir,
            ".env 来源": self.env_file or "（未找到，使用进程环境变量）",
        }

    def _keys_summary(self) -> str:
        """把 key 池脱敏成一行摘要。"""
        keys = self.api_key_list()
        if not keys:
            return "（未配置）"
        if len(keys) == 1:
            return f"共 1 个：{mask_key(keys[0])}"
        return f"共 {len(keys)} 个：" + "、".join(mask_key(key) for key in keys)
