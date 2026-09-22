# AgentCode：可扩展的 Python 智能体框架

一个把「智能体范式」做成可插拔组件的教学向框架：LLM 后端、工具、记忆、中间件都能替换，
新增一个智能体只需要**一个文件**加一个注册装饰器；附带一个本地网页，默认只给结果，需要时可展开推理过程。

内置三种经典范式：

- **ReAct**：思考 → 行动 → 观察，边推理边调用工具，适合需要实时信息的任务。
- **Plan-and-Solve**：先拆解计划，再逐步执行，适合多步骤推理题。
- **Reflection**：生成 → 评审 → 优化，适合代码与写作类任务。

## 快速开始

```powershell
# 1. 安装依赖（本机已用 D:\Anaconda\python.exe 验证）
D:\Anaconda\python.exe -m pip install -r requirements.txt

# 2. 配置密钥：复制 .env.example 为 .env 并填写
#    LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_ID 为必填，SERPAPI_API_KEY 用于网页搜索

# 3. 打开可视化网页（默认离线演示，不消耗额度）
D:\Anaconda\python.exe -m agentcode web --open

# 4. 命令行离线演示（不需要任何密钥）
D:\Anaconda\python.exe -m agentcode run --agent react --llm mock --task "帮我看看北京今天适合去哪里"

# 5. 命令行使用真实模型
D:\Anaconda\python.exe -m agentcode run --agent react --task "帮我查一下北京天气并推荐景点" --trace traces/run.json

# 6. 其它子命令
D:\Anaconda\python.exe -m agentcode list
D:\Anaconda\python.exe -m agentcode config
```

## 可视化网页

```powershell
D:\Anaconda\python.exe -m agentcode web                 # 默认 http://127.0.0.1:8000
D:\Anaconda\python.exe -m agentcode web --port 8080 --open
D:\Anaconda\python.exe -m agentcode web --llm openai    # 页面默认选中真实模型
```

页面**默认只给结果**：左侧选智能体、填任务、选模型，运行期间只显示进度计时，
结束后给出荧光黄标注的最终答案。想看它是怎么想出来的，勾选左侧的「推理过程」，
右侧就会逐步出现每一步的思考、调用的工具和工具返回的观察。两种模式下都可以导出完整轨迹 JSON。

实现上分成两层：`agentcode/web/runner.py` 把一次运行变成事件流（`status` / `step` / `answer` / `error`），
`agentcode/web/server.py` 用标准库 `http.server` 把它以 SSE 推给浏览器。整个过程**不新增任何依赖**，
前端是一个不依赖框架和构建步骤的页面。服务只监听 `127.0.0.1`，不做鉴权，请勿暴露到公网。

实时推送靠 `RunContext` 上的一个可选观察者回调 `on_step`；无论页面是否显示推理过程，
事件流都会照常推送，所以将来要做更细的展示（比如只显示耗时或工具名）不需要改后端。

## 架构

```
CLI (agentcode.cli)  ──  agentcode web  ──▶  Web 层 (agentcode.web)
      │                                              │
      ▼                                              ▼
Agent 实现 (agentcode.agents.*)  ── 注册到 ──▶  AgentRegistry
      │
      ├──▶ BaseAgent (agentcode.core.agent) ──▶ Middleware 链（日志/重试/超时/用量）
      ├──▶ BaseLLM   (agentcode.llm.*)      ── openai 兼容实现 + 离线脚本模型
      ├──▶ ToolRegistry (agentcode.tools.*) ── 工具注册、描述、安全调用
      └──▶ Memory (agentcode.memory.*)      ── 短期记忆与轨迹落盘

配置：agentcode.config.Settings  ←  .env / JSON 配置文件
结果：AgentResult / Step（可导出为轨迹 JSON）
```

目录一览：

| 路径 | 职责 |
| --- | --- |
| `agentcode/core/` | 基类、注册表、解析器、运行上下文与结果结构 |
| `agentcode/llm/` | LLM 后端：OpenAI 兼容 + 离线脚本模型 |
| `agentcode/tools/` | 工具注册表与内置工具（搜索、计算器、时间） |
| `agentcode/memory/` | 短期记忆、JSON 轨迹读写 |
| `agentcode/middleware/` | 日志、重试、超时、token 统计 |
| `agentcode/agents/` | ReAct、Plan-and-Solve、Reflection、Echo 示例 |
| `agentcode/web/` | 本地网页：事件流运行器与零依赖 HTTP 服务 |
| `tests/` | 全离线单元测试 |

## 三种范式的示例输出

```
$ D:\Anaconda\python.exe -m agentcode run --agent react --llm mock --task "北京天气如何"
================================================
智能体：react
任务：北京天气如何
------------------------------------------------
[步骤 1]
  思考：先了解北京的天气，再推荐景点。
  行动：get_weather[北京]
  观察：北京当前天气：晴，气温 24 摄氏度（演示数据）
[步骤 2]
  思考：天气信息已拿到，可以给出最终建议。
  行动：Finish[北京当前晴、气温 24 摄氏度（演示数据），适合去故宫和颐和园。]
------------------------------------------------
最终答案：北京当前晴、气温 24 摄氏度（演示数据），适合去故宫和颐和园。
模型调用：2 次；token 合计约 215（估算值）；耗时 12 ms
================================================
```

命令行会打印完整轨迹（终端本身就是给开发者看的），网页默认只给结果——两边的差别只在这里。

```powershell
# 多步推理题：先列计划，再逐步算
D:\Anaconda\python.exe -m agentcode run --agent plan_and_solve --llm mock --task "三天共卖出多少苹果"

# 代码优化：自我评审并迭代
D:\Anaconda\python.exe -m agentcode run --agent reflection --llm mock --task "写一个找素数的函数"
```

## 扩展指南

### 新增一个智能体

```python
# agentcode/agents/my_agent.py
from agentcode.core.agent import BaseAgent
from agentcode.core.registry import register_agent
from agentcode.core.result import AgentResult


@register_agent("my_agent", "我的智能体：一句话说明。")
class MyAgent(BaseAgent):
    """自定义智能体。"""

    name = "my_agent"
    description = "我的智能体：一句话说明。"

    def run(self, task: str, context=None) -> AgentResult:
        ctx = context or self._new_context(task)
        reply = self._think([{"role": "user", "content": task}], ctx)
        return self._build_result(task, reply, ctx)
```

在 `agentcode/agents/__init__.py` 中导入一次即完成注册，随后
`python -m agentcode run --agent my_agent` 和网页上的智能体列表都会出现它。
`agentcode/agents/echo.py` 是可运行的最小示例。

### 新增一个工具

```python
@tools.tool(description="把两个数相加。", parameters={"a": "加数", "b": "被加数"})
def add(a: str, b: str) -> str:
    return str(int(a) + int(b))
```

工具内部抛出的异常会被统一转换成中文错误字符串，作为 Observation 回到循环里，
不会中断整个任务。

### 新增一个中间件

继承 `agentcode.middleware.base.Middleware`，覆盖 `wrap_llm` / `wrap_tool` 返回新的调用函数即可。
框架自带日志、指数退避重试、超时与 token 统计四个中间件；网页的进度提示也是这么实现的。

## 配置说明

| 环境变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `LLM_API_KEY` | 是 | 无 | 模型服务密钥 |
| `LLM_BASE_URL` | 是 | 无 | 兼容 OpenAI 的接口地址 |
| `LLM_MODEL_ID` | 是 | 无 | 模型名称 |
| `LLM_TIMEOUT` | 否 | `60` | 单次调用超时（秒） |
| `LLM_TEMPERATURE` | 否 | `0.0` | 采样温度 |
| `LLM_STREAM` | 否 | `true` | 是否流式输出 |
| `SERPAPI_API_KEY` | 否 | 无 | 网页搜索工具密钥 |
| `AGENT_MAX_STEPS` | 否 | `6` | 智能体最大步数 |
| `AGENT_TRACE_DIR` | 否 | `traces` | 轨迹默认输出目录 |

配置文件（`--config configs/example.json`）可以覆盖默认智能体、步数、温度等字段，
网页也可以用 `web_host`、`web_port`、`llm_mode` 三个字段设默认值。
`.env` 的查找顺序是：显式传入的路径 → 当前目录向上最多三层 → 进程环境变量。
密钥永不写入源码，`config` 子命令与网页配置面板输出时都自动脱敏。

## 测试

```powershell
D:\Anaconda\python.exe -m pytest -q
```

101 项用例全部离线运行，不产生任何网络请求，也不消耗 API 额度；
其中网页部分通过真实 HTTP 请求与 SSE 流解析做端到端验证。

## 设计文档与实现计划

- 设计文档：`docs/superpowers/specs/2026-09-22-agentcode-framework-design.md`
- 实现计划：`docs/superpowers/plans/2026-09-22-agentcode-framework.md`
