"""OpenAI 兼容后端：可对接 DeepSeek、通义、本地 vLLM 等任意兼容服务。"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from agentcode.core.errors import ConfigError, LLMError
from agentcode.llm.base import BaseLLM, Message


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


class OpenAICompatibleLLM(BaseLLM):
    """调用任何兼容 OpenAI 接口的服务。"""

    name = "openai"

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str,
        timeout: float = 60.0,
        temperature: float = 0.0,
        stream: bool = True,
        client: Any | None = None,
    ) -> None:
        super().__init__(temperature=temperature)
        if not model or not api_key or not base_url:
            raise ConfigError(
                "初始化模型客户端失败：LLM_MODEL_ID、LLM_API_KEY、LLM_BASE_URL 必须齐全。"
            )
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.stream = stream
        if client is not None:
            self.client = client
        else:
            from openai import OpenAI

            self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    @classmethod
    def from_settings(cls, settings: Any) -> "OpenAICompatibleLLM":
        """按 :class:`Settings` 构造后端。"""
        settings.validate()
        return cls(
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.timeout,
            temperature=settings.temperature,
            stream=settings.stream,
        )

    def _request(self, messages: Sequence[Message], temperature: float) -> Any:
        """发起一次请求并返回原始响应对象。"""
        return self.client.chat.completions.create(
            model=self.model,
            messages=[dict(message) for message in messages],
            temperature=temperature,
            stream=self.stream,
        )

    def think(
        self,
        messages: Sequence[Message],
        temperature: float | None = None,
    ) -> str:
        """调用模型并返回完整回复文本。"""
        used_temperature = self.temperature if temperature is None else temperature
        try:
            response = self._request(messages, used_temperature)
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
            raise LLMError(f"调用大语言模型失败：{exc}") from exc
