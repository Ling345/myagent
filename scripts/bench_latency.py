"""测一次调用的真实延迟：对比模型与流式开关，帮助决定速度优化方向。

用法：
    D:\\Anaconda\\python.exe scripts\\bench_latency.py [--runs 2]

只发很小的请求（每个约几十 token），结果打印成表格。
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentcode.config import Settings  # noqa: E402
from agentcode.llm.openai_compatible import OpenAICompatibleLLM  # noqa: E402

PROMPT_TINY = "只回复两个字：收到"

#: 接近真实 ReAct 调用规模的提示词（工具清单 + 历史 + 步骤）
PROMPT_REAL = """你是一个可以调用外部工具的智能助手。

# 可用工具
- calculator: 计算一个算术表达式，例如 (15*2-5)/3。（参数：expression: 需要计算的算术表达式）
- current_time: 查询指定时区的当前时间。（参数：timezone: 时区名，例如 Asia/Shanghai）
- web_search: 网页搜索引擎，用于查询时事、事实以及模型知识库之外的信息。（参数：query: 搜索关键词）
- run_python: 在代码工作目录执行一段 Python 代码，返回退出码与 stdout/stderr。写完代码必须用它跑一遍。（工作目录：D:\\agent\\agentcode-homework1\\traces\\sandbox）（参数：code: 要执行的 Python 代码，timeout_seconds: 可选，超时秒数（上限 60））
- read_file: 读取代码工作目录里的文件，内容过长会截断。（工作目录：D:\\agent\\agentcode-homework1\\traces\\sandbox）（参数：path: 相对代码工作目录的文件路径）
- write_file: 把内容写入代码工作目录里的文件，父目录会自动创建。（工作目录：D:\\agent\\agentcode-homework1\\traces\\sandbox）（参数：path: 相对代码工作目录的文件路径，content: 文件内容）
- list_files: 列出代码工作目录里的文件，用于确认当前有哪些文件。（工作目录：D:\\agent\\agentcode-homework1\\traces\\sandbox）（参数：path: 可选，相对路径的目录，默认根目录）

# 行动格式
你的回答必须严格遵循下面的两行格式，不要输出其它内容：
Thought: 你的思考过程，用于分析问题与规划下一步
Action: 需要执行的动作，取值为以下两种之一
- 调用工具：工具名[输入]
- 给出最终答案：Finish[最终答案]

# 更早的对话
用户：帮我查一下北京今天的天气，再推荐一个景点
助手：北京当前晴、气温 24 摄氏度（演示数据），适合去故宫和颐和园。

# 本轮已完成的步骤
Action: get_weather[北京]
Observation: 北京当前晴，气温 24 摄氏度

# 当前问题
帮我查一下上海今天的天气，再推荐一个景点

如果当前问题是对更早对话的追问（例如"那另一个城市呢"），请结合更早的对话理解它。
请开始作答。"""


def list_models(settings: Settings) -> list[str]:
    """问一下服务端有哪些模型可用（部分服务不提供该接口）。"""
    try:
        from openai import OpenAI

        client = OpenAI(api_key=settings.api_key, base_url=settings.base_url, timeout=20)
        return [item.id for item in client.models.list().data]
    except Exception as exc:  # noqa: BLE001 - 列不出来不影响测速
        print(f"（无法列出模型：{exc}）")
        return []


def time_once(settings: Settings, model: str, prompt: str, stream: bool) -> float:
    """发一次请求并返回耗时（秒）。"""
    llm = OpenAICompatibleLLM(
        model=model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        timeout=settings.timeout,
        stream=stream,
    )
    started = time.perf_counter()
    llm.think([{"role": "user", "content": prompt}])
    return time.perf_counter() - started


def main() -> int:
    parser = argparse.ArgumentParser(description="模型调用延迟基准")
    parser.add_argument("--runs", type=int, default=2, help="每种组合测几次")
    parser.add_argument("--models", default="", help="逗号分隔的模型名，默认测全部可用模型")
    parser.add_argument("--stream", default="false", help="true/false，是否使用流式")
    args = parser.parse_args()

    settings = Settings.from_env().validate()
    available = list_models(settings)
    if available:
        print(f"账号可用模型：{', '.join(available)}")

    models = [item.strip() for item in args.models.split(",") if item.strip()]
    if not models:
        models = available or [settings.model]
    stream = args.stream.strip().lower() in {"1", "true", "yes"}

    prompts = [("小请求", PROMPT_TINY), ("真实规模", PROMPT_REAL)]
    print(f"\n流式：{'开' if stream else '关'}")
    print(f"{'模型':<20}{'场景':<10}{'耗时':<10}{'明细'}")
    print("-" * 66)
    for model in models:
        for label, prompt in prompts:
            samples: list[float] = []
            for _ in range(max(1, args.runs)):
                try:
                    samples.append(time_once(settings, model, prompt, stream))
                except Exception as exc:  # noqa: BLE001 - 模型名不存在时跳过
                    print(f"{model:<20}{label:<10}失败：{exc}")
                    break
            if samples:
                detail = "、".join(f"{value:.2f}s" for value in samples)
                print(
                    f"{model:<20}{label:<10}{statistics.mean(samples):<10.2f}{detail}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
