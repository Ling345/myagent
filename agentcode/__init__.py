"""AgentCode：一个可扩展的 Python 智能体框架。

框架由四层组成：

- ``agentcode.core``：智能体基类、注册表、解析与结果数据结构。
- ``agentcode.llm`` / ``agentcode.tools`` / ``agentcode.memory`` / ``agentcode.middleware``：可插拔实现。
- ``agentcode.agents``：ReAct、Plan-and-Solve、Reflection 等具体智能体。
- ``agentcode.cli``：统一的命令行入口。
"""

from agentcode.config import Settings
from agentcode.core.errors import (
    AgentCodeError,
    AgentNotFoundError,
    ConfigError,
    LLMError,
    ParseError,
    ToolError,
)
from agentcode.core.registry import default_registry, register_agent
from agentcode.core.result import AgentResult, Step, TokenUsage

__version__ = "0.1.0"

__all__ = [
    "AgentCodeError",
    "AgentNotFoundError",
    "AgentResult",
    "ConfigError",
    "LLMError",
    "ParseError",
    "Settings",
    "Step",
    "TokenUsage",
    "ToolError",
    "__version__",
    "default_registry",
    "register_agent",
]
