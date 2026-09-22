"""框架的统一异常体系。

约定：

- 面向用户的错误信息一律使用中文。
- 智能体循环中可恢复的错误（例如工具执行失败）不会抛异常，
  而是以错误字符串形式作为 Observation 返回。
"""


class AgentCodeError(Exception):
    """框架内所有异常的基类。"""


class ConfigError(AgentCodeError):
    """配置缺失或非法。"""


class LLMError(AgentCodeError):
    """调用大语言模型失败。"""


class ToolError(AgentCodeError):
    """工具注册或调用出现无法就地恢复的问题。"""


class ParseError(AgentCodeError):
    """模型输出无法按约定格式解析。"""


class AgentNotFoundError(AgentCodeError):
    """注册表中不存在指定名称的智能体。"""
