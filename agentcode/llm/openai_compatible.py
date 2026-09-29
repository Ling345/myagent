"""OpenAI 兼容后端：可对接 DeepSeek、通义、本地 vLLM 等任意兼容服务。

支持**多 key 轮换 + 熔断**：配了 ``LLM_API_KEYS``（逗号或换行分隔）之后，
请求会在多个 key 之间轮着用；某个 key 连续失败到阈值就先摘掉，
冷却一段时间再自动放回来。详见 :mod:`agentcode.llm.keypool`。
"""

from __future__ import annotations

import threading
from typing import Any, Iterable, Mapping, Sequence

from agentcode.core.errors import ConfigError, LLMError
from agentcode.llm.base import BaseLLM, Message
from agentcode.llm.keypool import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_FAILURE_THRESHOLD,
    KeyPool,
    parse_keys,
)

#: 按 (base_url, api_key, timeout) 复用同一个 SDK 客户端，
#: 避免每次请求都重新建连接池、重做 TLS 握手
_CLIENT_CACHE: dict[tuple[str, str, float], Any] = {}
_CLIENT_LOCK = threading.Lock()

#: 这些状态码换一个 key 重试是有意义的：
#: 限流、额度打满、key 被停用、服务端抖动、网关超时
RETRYABLE_STATUS = frozenset({401, 403, 408, 409, 425, 429, 500, 502, 503, 504, 522, 524})


class _RetryableLLMError(LLMError):
    """换个 key 还有救的错误（外部不要依赖这个类型）。"""


def _shared_client(api_key: str, base_url: str, timeout: float) -> Any:
    """取（或创建）可复用的 OpenAI 客户端。"""
    key = (base_url, api_key, float(timeout))
    with _CLIENT_LOCK:
        client = _CLIENT_CACHE.get(key)
        if client is None:
            from openai import OpenAI

            client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
            _CLIENT_CACHE[key] = client
        return client


def _usage_to_dict(usage: Any) -> dict[str, Any] | None:
    """把 SDK 的 usage 对象转成字典。"""
    if usage is None:
        return None
    if isinstance(usage, Mapping):
        return dict(usage)
    data: dict[str, Any] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if value is not None:
            data[key] = value
    return data or None


def _status_of(exc: BaseException) -> int | None:
    """尽量从异常里挖出 HTTP 状态码。"""
    for attribute in ("status_code", "http_status", "code"):
        value = getattr(exc, attribute, None)
        if isinstance(value, int):
            return value
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def is_retryable(exc: BaseException) -> bool:
    """判断"换一个 key 再试"是否有意义。

    拿不到状态码（连接超时、DNS 失败等）时按可重试处理；
    400/404/422 这类是我们自己请求写错了，换 key 也救不回来。
    """
    status = _status_of(exc)
    if status is None:
        return True
    return status in RETRYABLE_STATUS or status >= 500


class OpenAICompatibleLLM(BaseLLM):
    """调用任何兼容 OpenAI 接口的服务。"""

    name = "openai"

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 60.0,
        temperature: float = 0.0,
        stream: bool = True,
        client: Any | None = None,
        *,
        api_keys: Iterable[str] | None = None,
        key_pool: KeyPool | None = None,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown: float = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        super().__init__(temperature=temperature)
        keys = parse_keys(api_keys) or parse_keys(api_key)
        if not model or not base_url or not (keys or key_pool is not None):
            raise ConfigError(
                "初始化模型客户端失败：LLM_MODEL_ID、LLM_API_KEY（或 LLM_API_KEYS）、"
                "LLM_BASE_URL 必须齐全。"
            )
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.stream = stream
        self.pool = key_pool or KeyPool(
            keys, failure_threshold=failure_threshold, cooldown=cooldown
        )
        self._injected_client = client
        # ``client`` 保持指向第一个 key 的客户端，方便自检与既有调用方
        self.client = client if client is not None else self._client_for(self.pool.primary().key)
        #: 最近一次调用用的是哪个 key（下标）
        self.last_key_index: int | None = None

    # ------------------------------------------------------------------ 构造

    @classmethod
    def from_settings(cls, settings: Any) -> "OpenAICompatibleLLM":
        """按 :class:`Settings` 构造后端（自动带上 key 池配置）。"""
        settings.validate()
        return cls(
            model=settings.model,
            base_url=settings.base_url,
            timeout=settings.timeout,
            temperature=settings.temperature,
            stream=settings.stream,
            api_keys=settings.api_key_list(),
            failure_threshold=settings.key_failure_threshold,
            cooldown=settings.key_cooldown_seconds,
        )

    def _client_for(self, api_key: str) -> Any:
        """取这个 key 对应的客户端。"""
        if self._injected_client is not None:
            return self._injected_client
        return _shared_client(api_key, self.base_url, self.timeout)

    # ------------------------------------------------------------------ 调用

    def _call_once(self, messages: Sequence[Message], temperature: float, api_key: str) -> str:
        """用指定 key 完整跑一次请求（含流式读取）。"""
        client = self._client_for(api_key)
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[dict(message) for message in messages],
                temperature=temperature,
                stream=self.stream,
            )
            if not self.stream:
                content = response.choices[0].message.content or ""
                self._last_usage = _usage_to_dict(getattr(response, "usage", None))
                return content

            chunks: list[str] = []
            usage: dict[str, Any] | None = None
            for chunk in response:
                chunk_usage = _usage_to_dict(getattr(chunk, "usage", None))
                if chunk_usage:
                    usage = chunk_usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                delta = getattr(choices[0], "delta", None)
                content = getattr(delta, "content", None) if delta is not None else None
                if content:
                    chunks.append(content)
            text = "".join(chunks)
            self._last_usage = usage or {
                "prompt_tokens": sum(len(str(m.get("content", ""))) for m in messages) // 2,
                "completion_tokens": len(text) // 2,
                "estimated": True,
            }
            return text
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转成框架异常
            message = f"调用大语言模型失败：{exc}"
            if is_retryable(exc):
                raise _RetryableLLMError(message) from exc
            raise LLMError(message) from exc

    def think(
        self,
        messages: Sequence[Message],
        temperature: float | None = None,
    ) -> str:
        """调用模型并返回完整回复文本；失败时自动换 key 重试。"""
        used_temperature = self.temperature if temperature is None else temperature
        attempts = len(self.pool)
        last_error: LLMError | None = None

        for _ in range(attempts):
            state = self.pool.acquire()
            if state is None:
                break
            try:
                text = self._call_once(messages, used_temperature, state.key)
            except _RetryableLLMError as exc:
                # 这个 key 有问题（限流/额度/停用/抖动）：记账后换下一个
                self.pool.report_failure(state)
                last_error = exc
                continue
            except LLMError:
                # 我们自己请求写错了，换 key 也没用，别污染熔断计数
                raise
            self.pool.report_success(state)
            self.last_key_index = state.index
            return text

        if last_error is not None:
            raise LLMError(f"{last_error}（已依次尝试 {attempts} 个 key，全部失败）")

        wait = self.pool.next_ready_in()
        hint = f"，最快约 {wait:.0f} 秒后自动恢复" if wait else ""
        raise LLMError(
            f"所有 API key 都在熔断冷却中（共 {len(self.pool)} 个）{hint}。"
            "请补充新的 key，或稍后重试。"
        )
