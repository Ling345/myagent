# AgentCode：可扩展的 Python 智能体框架

一个把「智能体范式」做成可插拔组件的教学向框架：LLM 后端、工具、记忆、中间件都能替换，
新增一个智能体只需要**一个文件**加一个注册装饰器；附带一个本地网页，只给结果，但记住你们的对话。

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

# 3. 打开网页（推荐：直接双击项目里的「启动网页.cmd」）
D:\Anaconda\python.exe -m agentcode open

# 4. 命令行离线演示（不需要任何密钥）
D:\Anaconda\python.exe -m agentcode run --agent react --llm mock --task "帮我看看北京今天适合去哪里"

# 5. 命令行使用真实模型，并带上下文会话
D:\Anaconda\python.exe -m agentcode run --agent react --session 北京游 --task "帮我查一下北京天气并推荐景点"

# 6. 其它子命令
D:\Anaconda\python.exe -m agentcode list
D:\Anaconda\python.exe -m agentcode config
```

## 上下文记忆

记忆在三个范式里都会真正参与推理：每轮结束后，把「用户提问 + 最终答案」整体写入短期记忆，
下一轮渲染进提示词，所以「那上海呢」这种指代型追问能听懂。失败的运行不写记忆，
上下文里不会留下"只问没答"的半截记录。

记忆轮数可配，默认 **5 轮**（一轮 = 一次提问 + 一次回答）：

| 位置 | 说明 |
| --- | --- |
| `.env` 里的 `AGENT_MEMORY_TURNS` | 全局默认，命令行与网页都读它 |
| `--memory-turns 3` | 只对这一次 `run` 生效，优先级高于 `.env` |
| 配置文件里的 `memory_turns` | 用 `--config configs/example.json` 时生效 |

轮数上限只决定**带进提示词的上下文长度**，不影响你能聊多少轮：页面侧栏会写「已记住 2 轮上下文（上限 2 轮）」，
而答案下面标的是真实轮次「本次会话第 3 轮」，两者不会因为裁剪而对不上。

网页端按标签页维度自动延续上下文：

- 页面在 `localStorage` 里放一个会话标识，每次运行带上它，服务端复用同一个智能体实例（记忆随之延续）。
- 点侧栏的「新会话」即可清空上下文重新开始；服务端同时保留的会话数由 `AGENT_MAX_SESSIONS` 控制（默认 20），超出按最近使用淘汰。
- 服务端只保存内存里的会话，重启服务即全部清空。

命令行用 `--session 名字` 显式开启会话，记忆会落盘到 `traces/sessions/<名字>.json`，
下次同名会话自动接着聊（`--session-dir` 可改目录）：

```powershell
D:\Anaconda\python.exe -m agentcode run --task "请记住：我最喜欢的城市是杭州" --session 备忘
D:\Anaconda\python.exe -m agentcode run --task "我最喜欢的城市是哪个？" --session 备忘
# 第二次会先打印「已载入会话「备忘」的 1 轮上下文。」，再直接答出杭州
```

## 网站入口与网页

三种打开方式，效果一样：

| 方式 | 做法 |
| --- | --- |
| 双击 | 打开项目目录，双击 `启动网页.cmd` |
| 一条命令 | `D:\Anaconda\python.exe -m agentcode open` |
| 自己管端口 | `D:\Anaconda\python.exe -m agentcode web --port 8080 --open` |

`open` 会先探测端口：已经在运行就直接打开浏览器，没运行就以当前窗口启动服务并打开浏览器。
服务地址默认 `http://127.0.0.1:8000/`。**这个窗口就是服务本身**，关掉窗口（或按 Ctrl+C）服务就停了。

页面**只给结果**，并且**只走真实模型**：左侧选智能体、写下任务、点运行，屏幕上先显示进度计时，
结束后直接给出荧光黄标注的最终答案，没有推理过程、思考内容或工具调用记录的任何入口，
也没有「离线演示 / 真实模型」这类选择——它调用的就是 `.env` 里配置的那个模型。
想拿完整执行记录时，点「导出完整结果 JSON」可以得到每一步的原始数据。

需要离线跑（没有密钥、或者不想消耗额度）时用命令行或显式参数：

```powershell
# 命令行离线演示
D:\Anaconda\python.exe -m agentcode run --agent react --llm mock --task "北京天气如何"

# 用离线模型启动网页（主要用于自动化测试与演示，页面上不做区分）
D:\Anaconda\python.exe -m agentcode web --llm mock
```

实现上分成两层：`agentcode/web/runner.py` 把一次运行变成事件流（`status` / `step` / `answer` / `error`），
`agentcode/web/server.py` 用标准库 `http.server` 把它以 SSE 推给浏览器。整个过程**不新增任何依赖**，
前端是一个不依赖框架和构建步骤的页面。服务只监听 `127.0.0.1`，不做鉴权，请勿暴露到公网。

## 架构

```
CLI (agentcode.cli)  ──  agentcode web / open  ──▶  Web 层 (agentcode.web)
      │                                                    │
      ▼                                                    ▼
Agent 实现 (agentcode.agents.*)  ── 注册到 ──▶  AgentRegistry
      │
      ├──▶ BaseAgent (agentcode.core.agent) ──▶ 上下文记忆 + Middleware 链（日志/重试/超时/用量）
      ├──▶ BaseLLM   (agentcode.llm.*)      ── openai 兼容实现 + 离线脚本模型
      ├──▶ ToolRegistry (agentcode.tools.*) ── 工具注册、描述、安全调用
      └──▶ Memory (agentcode.memory.*)      ── 短期记忆、磁盘会话、结果落盘

配置：agentcode.config.Settings  ←  .env / JSON 配置文件
结果：AgentResult / Step（可导出为完整结果 JSON）
```

目录一览：

| 路径 | 职责 |
| --- | --- |
| `agentcode/core/` | 基类、注册表、解析器、运行上下文与结果结构 |
| `agentcode/llm/` | LLM 后端：OpenAI 兼容 + 离线脚本模型 |
| `agentcode/tools/` | 工具注册表与内置工具（搜索、计算器、时间） |
| `agentcode/memory/` | 短期记忆、磁盘会话读写、结果落盘 |
| `agentcode/middleware/` | 日志、重试、超时、token 统计 |
| `agentcode/agents/` | ReAct、Plan-and-Solve、Reflection、Echo 示例 |
| `agentcode/web/` | 本地网页：事件流运行器、会话表、零依赖 HTTP 服务 |
| `启动网页.cmd` | 双击启动服务并打开浏览器 |
| `tests/` | 全离线单元测试 |

## 三种范式的示例输出

命令行会打印完整轨迹（终端本来就是给开发者看的），网页只给结果——两边的差别只在这里。

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
        prompt = f"更早的对话：\n{self._history_text()}\n\n当前问题：{task}"
        reply = self._think([{"role": "user", "content": prompt}], ctx)
        self._remember(task, reply)
        return self._build_result(task, reply, ctx)
```

三个钩子就是上下文记忆的全部成本：`self._history_text()` 读，`self._remember()` 写，
`self._new_context()` 建本次运行上下文。在 `agentcode/agents/__init__.py` 里导入一次即完成注册，
随后命令行和网页的智能体列表都会出现它。`agentcode/agents/echo.py` 是可运行的最小示例。

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
| `AGENT_MAX_STEPS` | 否 | `6` | 智能体单轮最大步数 |
| `AGENT_MEMORY_TURNS` | 否 | `5` | 每个会话记住几轮上下文 |
| `AGENT_MAX_SESSIONS` | 否 | `20` | 网页同时保留多少个会话 |
| `AGENT_TRACE_DIR` | 否 | `traces` | 轨迹默认输出目录 |

配置文件（`--config configs/example.json`）可以覆盖默认智能体、步数、温度、记忆轮数等字段，
网页也可以用 `web_host`、`web_port`、`llm_mode` 三个字段设默认值（`llm_mode` 默认 `openai`）。
`.env` 的查找顺序是：显式传入的路径 → 当前目录向上最多三层 → 进程环境变量。
密钥永不写入源码，`config` 子命令与网页配置面板输出时都自动脱敏。

## 测试

```powershell
D:\Anaconda\python.exe -m pytest -q
```

136 项用例全部离线运行，不产生任何网络请求，也不消耗 API 额度；
其中网页部分通过真实 HTTP 请求与 SSE 流解析做端到端验证，上下文记忆覆盖了
「第二轮提示词里能看到第一轮的答案」「超限裁剪」「真实轮次不回退」「会话隔离」
「新会话清空」「磁盘会话读写」「`AGENT_MEMORY_TURNS` 与 `--memory-turns` 生效」，
另外有一组测试守住"页面不再出现离线演示选项"。

## 设计文档与实现计划

- 设计文档：`docs/superpowers/specs/2026-09-22-agentcode-framework-design.md`
- 实现计划：`docs/superpowers/plans/2026-09-22-agentcode-framework.md`
