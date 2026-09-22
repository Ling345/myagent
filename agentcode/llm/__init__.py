"""LLM 后端：抽象接口、OpenAI 兼容实现与离线脚本模型。"""

from agentcode.llm.base import BaseLLM
from agentcode.llm.mock import ScriptedLLM, demo_responses_llm
from agentcode.llm.openai_compatible import OpenAICompatibleLLM

__all__ = ["BaseLLM", "OpenAICompatibleLLM", "ScriptedLLM", "demo_responses_llm"]
