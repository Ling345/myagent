"""多模型档位：按用途挑模型，整个档位挂了自动降级。

为什么需要它：不同活儿对模型的要求完全不同——写代码要强模型，回显、汇总这种
用便宜的就够；而且单一供应商抖一下，整个服务就跟着抖。所以把模型抽象成**档位**
（profile）：

- 一个档位 = 一个模型 + 一个 base_url + 一组 key（key 只从环境变量 /.env 读，
  配置文件里写的是**环境变量名**）；
- 档位可以声明 ``fallback``：这一档整体不可用时按顺序换下一档；
- 选档顺序：显式指定 > 按智能体名（``profile_by_agent``）> ``default_profile``；
- **没配任何档位就完全走老路**（单模型 + key 池），行为与以前一模一样。

降级策略：可重试的错（限流、超时、5xx）先退避一下再换档；其它错误立刻换档。
一旦降级，**本次运行不再回头**（一个任务里在两个模型之间来回横跳，上下文风格
会飘，也不好排查）。每次运行结果里会写清"这次是谁答的"。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from agentcode.core.errors import ConfigError, LLMError
from agentcode.llm.base import BaseLLM, LLMReply, Message
from agentcode.llm.keypool import parse_keys
from agentcode.metrics import MODEL_FALLBACK, MODEL_USED

#: 可重试的错换档前先退避多久（秒）
DEFAULT_SWITCH_DELAY = 1.0

#: 「档位 → 后端」的构造器；测试里换成假的，就不必碰网络
ProviderFactory = Callable[["ModelProfile", Any], BaseLLM]


@dataclass(frozen=True)
class ModelProfile:
    """一个模型档位。``keys`` 是真密钥，只活在内存里，不打印、不落盘。"""

    name: str
    model: str
    base_url: str
    keys: tuple[str, ...] = ()
    fallback: tuple[str, ...] = ()
    timeout: float | None = None
    temperature: float | None = None
    max_steps: int | None = None
    #: 密钥来自哪个环境变量（展示用）
    api_key_env: str = ""
    api_keys_env: str = ""

    def to_dict(self) -> dict[str, Any]:
        """展示用摘要——绝不包含密钥本身。"""
        return {
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env or self.api_keys_env or "（未配置）",
            "keys": len(self.keys),
            "fallback": list(self.fallback),
        }


def parse_profiles(
    raw: Mapping[str, Any] | None, *, env: Mapping[str, str] | None = None
) -> dict[str, ModelProfile]:
    """把配置里的档位解析成 :class:`ModelProfile`，配置有问题就报中文错。"""
    source = dict(raw or {})
    if not source:
        return {}
    environment: Mapping[str, str] = os.environ if env is None else env
    profiles: dict[str, ModelProfile] = {}
    for name, item in source.items():
        key = str(name or "").strip()
        if not key:
            raise ConfigError("模型档位的名字不能为空。")
        if not isinstance(item, Mapping):
            raise ConfigError(f"模型档位 {key} 必须是对象，例如 {{'model': ..., 'base_url': ...}}。")
        model = str(item.get("model") or "").strip()
        base_url = str(item.get("base_url") or "").strip()
        if not model:
            raise ConfigError(f"模型档位 {key} 缺少 model。")
        if not base_url:
            raise ConfigError(f"模型档位 {key} 缺少 base_url。")
        single_env = str(item.get("api_key_env") or "").strip()
        multi_env = str(item.get("api_keys_env") or "").strip()
        if not single_env and not multi_env:
            raise ConfigError(
                f"模型档位 {key} 要指定 api_key_env（单把 key）或 api_keys_env（多把 key）"
                "——密钥只从环境变量 / .env 读，不写进配置文件。"
            )
        keys: list[str] = []
        for env_name in (single_env, multi_env):
            if not env_name:
                continue
            value = str(environment.get(env_name) or "").strip()
            if not value:
                raise ConfigError(
                    f"模型档位 {key} 用的环境变量 {env_name} 没有配（值不能为空）。"
                )
            keys.extend(parse_keys(value))
        if not keys:
            raise ConfigError(f"模型档位 {key} 没有可用的 key。")
        profiles[key] = ModelProfile(
            name=key,
            model=model,
            base_url=base_url,
            keys=tuple(keys),
            fallback=tuple(str(item_name).strip() for item_name in (item.get("fallback") or ())),
            timeout=_optional_float(item.get("timeout")),
            temperature=_optional_float(item.get("temperature")),
            max_steps=_optional_int(item.get("max_steps")),
            api_key_env=single_env,
            api_keys_env=multi_env,
        )

    for profile in profiles.values():
        for target in profile.fallback:
            if target not in profiles:
                raise ConfigError(
                    f"模型档位 {profile.name} 的 fallback 指向不存在的档位：{target}。"
                )
    _check_no_cycle(profiles)
    return profiles


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _check_no_cycle(profiles: Mapping[str, ModelProfile]) -> None:
    """fallback 链不能成环——成环意味着"降级也降不出去"。"""
    for name in profiles:
        seen: list[str] = []
        cursor = name
        while cursor:
            if cursor in seen:
                raise ConfigError(
                    "模型档位的 fallback 成环：" + " -> ".join(seen + [cursor]) + "。"
                )
            seen.append(cursor)
            chain = profiles[cursor].fallback
            cursor = chain[0] if chain else ""


def chain_for(name: str, profiles: Mapping[str, ModelProfile]) -> list[str]:
    """按 fallback 展开成一条降级链（去重，遇到不存在的档位就停）。"""
    chain: list[str] = []
    cursor = name
    while cursor and cursor in profiles and cursor not in chain:
        chain.append(cursor)
        fallback = profiles[cursor].fallback
        cursor = fallback[0] if fallback else ""
    return chain


def resolve_profile(
    settings: Any,
    *,
    agent: str | None = None,
    explicit: str | None = None,
) -> str:
    """这次用哪个档位；没有配置档位时返回空串（走老的单模型路径）。"""
    profiles = dict(getattr(settings, "model_profiles", {}) or {})
    requested = str(explicit or "").strip()
    if not profiles:
        if requested:
            raise ConfigError(f"没有配置模型档位，不能用 --model-profile {requested}。")
        return ""
    if requested:
        if requested not in profiles:
            raise ConfigError(
                f"没有这个模型档位：{requested}。已配置：{'、'.join(sorted(profiles))}。"
            )
        return requested
    by_agent = dict(getattr(settings, "profile_by_agent", {}) or {})
    if agent and str(by_agent.get(agent) or "").strip():
        chosen = str(by_agent[agent]).strip()
        if chosen not in profiles:
            raise ConfigError(
                f"profile_by_agent 里给 {agent} 指定的档位不存在：{chosen}。"
            )
        return chosen
    default = str(getattr(settings, "default_profile", "") or "").strip()
    if default:
        if default not in profiles:
            raise ConfigError(f"default_profile 指定的档位不存在：{default}。")
        return default
    # 没写默认档位时，用配置文件里**第一个写的**（顺序即优先级），而不是按名字排序——
    # 不然 strong/cheap 这种命名会让"便宜的那个"意外变成默认
    return next(iter(profiles))


class RoutedLLM(BaseLLM):
    """按降级链依次尝试的模型后端。"""

    name = "routed"

    def __init__(
        self,
        chain: Sequence[tuple[str, BaseLLM]],
        *,
        sleep: Callable[[float], None] = time.sleep,
        switch_delay: float = DEFAULT_SWITCH_DELAY,
    ) -> None:
        if not chain:
            raise ConfigError("模型档位链不能为空。")
        super().__init__(temperature=0.0)
        self._chain = list(chain)
        self._sleep = sleep
        self.switch_delay = max(0.0, float(switch_delay))
        self._index = 0
        #: 这次运行最后是谁答的（没有降级时就是首选档位）
        self.used_profile = ""
        #: 依次试过哪些档位（排错用，失败信息里也带上）
        self.attempted: list[str] = []

    @property
    def supports_tools(self) -> bool:  # type: ignore[override]
        """只要链里有档位支持工具调用，就值得先按原生工具调用试。"""
        return any(getattr(llm, "supports_tools", False) for _, llm in self._chain)

    @supports_tools.setter
    def supports_tools(self, value: bool) -> None:
        # 智能体发现"这家不支持工具调用"时会把开关关掉，这里同步给链上每一档
        for _, llm in self._chain:
            llm.supports_tools = value

    def think(self, messages: Sequence[Message], temperature: float | None = None) -> str:
        """老接口：拿文本。"""
        return self.complete(messages, temperature=temperature).text

    def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> LLMReply:
        """从当前档位往下试，第一个成功的就是本次运行的档位。"""
        errors: list[str] = []
        start = self._index
        for index in range(start, len(self._chain)):
            name, llm = self._chain[index]
            self.attempted.append(name)
            try:
                reply = llm.complete(messages, tools=tools, temperature=temperature)
            except Exception as exc:  # noqa: BLE001 - 这一档不行就换下一档
                errors.append(f"{name}：{exc}")
                if index + 1 < len(self._chain) and self._should_wait(exc) and self.switch_delay > 0:
                    self._sleep(self.switch_delay)
                continue
            self._last_usage = llm.last_usage
            if index != start:
                previous = self._chain[start][0]
                MODEL_FALLBACK.inc(**{"from": previous, "to": name})
                print(f"提示：模型档位 {previous} 不可用，本次运行改用 {name}。")
                self._index = index  # 本次运行不再回头
            self.used_profile = name
            MODEL_USED.inc(profile=name)
            return reply
        raise LLMError("模型档位全部失败：" + "；".join(errors))

    @staticmethod
    def _should_wait(exc: BaseException) -> bool:
        """可重试的错（限流/超时/5xx）先退避一下再换档。"""
        from agentcode.llm.openai_compatible import RetryableLLMError

        return isinstance(exc, RetryableLLMError)

    def snapshot(self) -> list[dict[str, Any]]:
        """档位链摘要（不回显密钥）。"""
        return [
            {
                "name": name,
                "active": index >= self._index,
                "supports_tools": bool(getattr(llm, "supports_tools", False)),
            }
            for index, (name, llm) in enumerate(self._chain)
        ]


def _profile_from_settings(settings: Any) -> ModelProfile:
    """没有配置档位时，把老的单模型配置当成一个匿名档位。"""
    return ModelProfile(
        name="",
        model=str(getattr(settings, "model", "") or ""),
        base_url=str(getattr(settings, "base_url", "") or ""),
        keys=tuple(settings.api_key_list()),
    )


def _default_provider_factory(profile: ModelProfile, settings: Any) -> BaseLLM:
    """真实构造：走 OpenAI 兼容后端（含该档位自己的 key 池）。"""
    from agentcode.llm.openai_compatible import OpenAICompatibleLLM

    return OpenAICompatibleLLM(
        model=profile.model,
        base_url=profile.base_url,
        timeout=profile.timeout or getattr(settings, "timeout", 60.0),
        temperature=(
            profile.temperature
            if profile.temperature is not None
            else getattr(settings, "temperature", 0.0)
        ),
        stream=bool(getattr(settings, "stream", True)),
        api_keys=profile.keys,
        failure_threshold=int(getattr(settings, "key_failure_threshold", 3)),
        cooldown=float(getattr(settings, "key_cooldown_seconds", 60.0)),
    )


def build_llm_for_agent(
    settings: Any,
    *,
    agent: str | None = None,
    explicit: str | None = None,
    provider_factory: ProviderFactory | None = None,
    env: Mapping[str, str] | None = None,
) -> BaseLLM:
    """按配置构造这次运行要用的模型后端。

    - 没配档位：返回普通的单模型后端（与以前完全一致）；
    - 配了档位：返回 :class:`RoutedLLM`，链上第一档是选中的档位，往后是 fallback。
    """
    factory = provider_factory or _default_provider_factory
    raw = dict(getattr(settings, "model_profiles", {}) or {})
    if not raw:
        name = resolve_profile(settings, explicit=explicit)
        if not name:  # 老路：单模型
            return factory(_profile_from_settings(settings), settings)
    if env is None:
        # 默认从 Settings 那边取环境（.env + 进程环境），别只认 os.environ
        provider_env = getattr(settings, "profile_env", None)
        env = provider_env() if callable(provider_env) else None
    profiles = parse_profiles(raw, env=env)
    name = resolve_profile(settings, agent=agent, explicit=explicit)
    chain = [profiles[item] for item in chain_for(name, profiles)]
    # 配了档位就一律走路由层：哪怕只有一档，也能记下"这次是谁答的"
    # （单档时的开销只是多一层函数调用）
    return RoutedLLM([(profile.name, factory(profile, settings)) for profile in chain])
