"""把 AgentCode 当作库使用的示例（默认离线运行，不消耗 API 额度）。

用法：
    D:\\Anaconda\\python.exe examples\\quickstart.py            # 离线脚本模型
    D:\\Anaconda\\python.exe examples\\quickstart.py --real     # 调用 .env 中配置的真实模型
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 允许直接以脚本方式运行（把仓库根目录加入模块搜索路径）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentcode import agents  # noqa: E402,F401  导入即注册内置智能体
from agentcode.config import Settings
from agentcode.core.registry import default_registry
from agentcode.llm.mock import demo_responses_llm
from agentcode.llm.openai_compatible import OpenAICompatibleLLM
from agentcode.memory import JsonStore
from agentcode.middleware import LoggingMiddleware, RetryMiddleware, TimeoutMiddleware
from agentcode.tools import ToolRegistry, register_builtin_tools, register_demo_tools


def build_tools(mock: bool) -> ToolRegistry:
    """准备工具集：mock 模式下用离线假工具，否则用真实搜索。"""
    tools = ToolRegistry()
    register_builtin_tools(tools, include_search=not mock)
    if mock:
        register_demo_tools(tools)
    return tools


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentCode 库用法示例")
    parser.add_argument("--real", action="store_true", help="使用 .env 中配置的真实模型")
    parser.add_argument("--agent", default="react", help="智能体名称")
    parser.add_argument("--task", default="帮我看看北京今天适合去哪里", help="任务描述")
    args = parser.parse_args()

    settings = Settings.from_env()

    if args.real:
        settings.validate()  # 缺少密钥会抛出 ConfigError 并给出中文提示
        llm = OpenAICompatibleLLM.from_settings(settings)
    else:
        llm = demo_responses_llm(args.agent)

    # 中间件顺序 = 包装顺序：日志在最外层，重试次之，超时最靠近真实调用
    agent = default_registry.create(
        args.agent,
        llm=llm,
        tools=build_tools(mock=not args.real),
        middlewares=[
            LoggingMiddleware(enabled=True),
            RetryMiddleware(max_retries=2, base_delay=0.2),
            TimeoutMiddleware(timeout=settings.timeout),
        ],
        max_steps=settings.max_steps,
    )

    result = agent.run(args.task)

    print("\n===== 运行结果 =====")
    print(f"是否成功：{result.success}")
    print(f"最终答案：{result.answer}")
    print(f"步骤数量：{len(result.steps)}；模型调用：{result.usage.calls} 次")

    trace_path = Path("traces") / f"{args.agent}-quickstart.json"
    JsonStore.save(result.to_dict(), trace_path)
    print(f"轨迹已写入：{trace_path}")
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
