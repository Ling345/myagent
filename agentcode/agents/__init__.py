"""内置智能体集合。

导入本模块会触发各智能体的注册，因此 CLI 只需要 ``import agentcode.agents``
即可在 ``default_registry`` 中看到它们。
"""

from agentcode.agents.coding import CodingAgent
from agentcode.agents.echo import EchoAgent
from agentcode.agents.plan_and_solve import Executor, PlanAndSolveAgent, Planner
from agentcode.agents.react import ReActAgent
from agentcode.agents.reflection import ReflectionAgent

__all__ = [
    "CodingAgent",
    "EchoAgent",
    "Executor",
    "PlanAndSolveAgent",
    "Planner",
    "ReActAgent",
    "ReflectionAgent",
]
