# AgentCode：可扩展的 Python 智能体框架

一个把「智能体范式」做成可插拔组件的教学向框架：LLM 后端、工具、记忆、中间件都能替换，
新增一个智能体只需要**一个文件**加一个注册装饰器；附带一个本地网页，只给结果，但记住你们的对话。

内置四种智能体：

- **ReAct**：思考 → 行动 → 观察，边推理边调用工具，适合需要实时信息的任务。
- **Plan-and-Solve**：先拆解计划，再逐步执行，适合多步骤推理题。
- **Reflection**：生成 → 评审 → 优化，适合代码与写作类任务。
- **Coding**：写测试 → 写实现 → 跑测试 → 改到全绿，反馈来自真实运行结果。

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

# 5. 让 coding 智能体真的写代码并跑通测试
D:\Anaconda\python.exe -m agentcode run --agent coding --task "实现 is_prime(n)，并写 pytest 测试覆盖 2、3、4、9、13"

# 6. 其它子命令
D:\Anaconda\python.exe -m agentcode list
D:\Anaconda\python.exe -m agentcode config
```

## 代码能力

`coding` 智能体带四个工具：`run_python`、`read_file`、`write_file`、`list_files`。
它的终止条件不是"我觉得写对了"，而是"测试真的跑绿了"——与 `reflection` 的区别在于反馈来自
真实执行的 stdout/stderr，而不是模型自评。

工作纪律写进了提示词：先写测试再写实现、每次改动都要跑、失败就读报错改、全绿才 `Finish`、
连续两轮无进展就如实报告卡点。默认给 **20 步**（`AGENT_CODING_STEPS`），比闲聊类智能体的 6 步多。

实测一次真实任务（DeepSeek，实现 `is_prime` 并写 5 个用例）：4 步、4 次模型调用、约 5300 token、
30 秒完成，测试一次通过。文件真的落在磁盘上，不是贴在回答里。

### 运行产物可以直接点开看

coding 智能体每写完一轮，服务端会对比代码目录的前后快照，把这一轮**新增或改动**的文件记下来，
挂在对应的回答下面（形如 `📄 prime.py 354 B`）。点一下就能在页面里读内容，
Esc 关闭；记录会随会话落盘，刷新或切回旧会话都还在。

### 速度

用 `scripts/bench_latency.py` 量过（同一提示词，各测多次）：

| 模型 | 小请求 | 真实规模（约 800 token 输入） |
| --- | --- | --- |
| `deepseek-flash` | 1.0 秒 | **2.5 秒**（2.45 / 2.56，稳定） |
| `deepseek-v4-pro` | 1.8 秒 | **14.0 秒**（5.3 / 22.6，波动极大） |

所以默认用 `deepseek-flash`。同一个编码任务实测：**从 30.5 秒降到 7.9 秒**（4 步、约 4400 token）。
另外三处也影响体感速度，都已经处理：

- LLM 客户端按配置复用，不再每次请求重做 TLS 握手；
- 代码工具只注册给 `coding` 智能体，别的智能体提示词更短，也不会被"诱导"去跑代码；
- 解析器容忍模型的一次回复里写多组 Thought/Action、把提示词条目文字当动作前缀等常见走样，
  避免因为一次解析失败白跑好几步（这曾让同一任务从 4 步涨到 12 步）。

### 受限执行，但**不是安全沙箱**

工具默认把代码跑在 `AGENT_CODE_ROOT`（默认 `traces/sandbox`），**不碰项目源码**。约束：

| 约束 | 做法 |
| --- | --- |
| 工作目录 | 固定在代码根目录，文件工具也只能访问这里（`..` 逃逸与外部绝对路径被拒绝） |
| 超时 | 默认 10 秒（`AGENT_CODE_TIMEOUT`），超时杀进程并返回中文提示 |
| 环境变量 | 只传白名单（PATH/TEMP 等），**不传任何密钥**，子进程读不到 `LLM_API_KEY` |
| 输出长度 | 截断到 `AGENT_CODE_OUTPUT_LIMIT`（默认 4000 字符），避免刷爆上下文 |

必须说清楚：Python 子进程本身仍能访问文件系统与网络，这套约束防的是"误伤与跑飞"，
不是防御恶意代码。真要跑不可信代码，需要容器或独立账号。

## 上下文记忆

记忆在四个范式里都会真正参与推理：每轮结束后，把「用户提问 + 最终答案」整体写入短期记忆，
下一轮渲染进提示词，所以「那上海呢」这种指代型追问能听懂。失败的运行不写记忆。

记忆轮数可配，默认 **5 轮**（一轮 = 一次提问 + 一次回答）：

| 位置 | 说明 |
| --- | --- |
| `.env` 里的 `AGENT_MEMORY_TURNS` | 全局默认，命令行与网页都读它 |
| `--memory-turns 3` | 只对这一次 `run` 生效，优先级高于 `.env` |
| 配置文件里的 `memory_turns` | 用 `--config configs/example.json` 时生效 |

轮数上限只决定**带进提示词的上下文长度**，不影响你能聊多少轮：网页头部会写「已记住 2/5 轮上下文」，
而消息区标的是真实轮次，两者不会因为裁剪而对不上。

## 会话管理

网页端把每次对话当成一个**会话**，左侧栏就是会话列表：

- 点「＋ 新会话」开一段新对话，**旧会话不会消失**，继续留在列表里；
- 会话名默认取第一条提问（截断到 16 字），也可以悬停某一行点 ✎ 改名，回车保存、Esc 取消；
- 点任意一行就能切回那个会话，完整对话记录会一起还原，上下文记忆也会跟着恢复，
  所以切回去继续追问「那上海呢」依然能听懂；
- 悬停某一行点 ✕ 可以删除该会话（会连同记录一起删掉）；
- 列表按最近使用排序，服务端最多保留 `AGENT_MAX_SESSIONS`（默认 20）个会话，超出淘汰最久没用的。

会话不只是内存里的东西：名字、创建/更新时间、**完整对话记录**都会落盘到
`AGENT_WEB_SESSION_DIR`（默认 `traces/web-sessions/`），所以重启服务之后列表和记录都还在。
智能体的上下文记忆仍是最近 `AGENT_MEMORY_TURNS` 轮，进程重启后由对话记录重建。
页面还会把当前会话记在 `localStorage`，刷新后自动回到同一个会话。

命令行用 `--session 名字` 显式开启会话，记忆会落盘到 `traces/sessions/<名字>.json`：

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

界面参考 DeepSeek 网页端的结构：左侧会话栏（新会话 + 智能体列表 + 模型信息）、
中间居中的消息流、底部圆角输入条。提问以浅蓝气泡靠右，回答以纯文本靠左，回车发送、Shift+回车换行。

视觉上有一层**缓慢流动的浅蓝色背景**（三个模糊光斑，40/55/68 秒各自漂移），侧栏、顶栏、输入条是
`backdrop-filter` 做的半透明面板；品牌蓝 `#3f5cf0` 比 DeepSeek 原色略深一档，为了白字与浅底都过对比度要求。
系统开启"减少动态效果"时背景会自动静止。

页面**只给结果**，并且**只走真实模型**：没有推理过程、思考内容或工具调用记录的任何入口，
也没有「离线演示 / 真实模型」这类选择；左侧栏除了会话列表，还有智能体列表与模型信息。
需要离线跑（没有密钥、或不想消耗额度）时用命令行或显式参数：

```powershell
D:\Anaconda\python.exe -m agentcode run --agent react --llm mock --task "北京天气如何"
D:\Anaconda\python.exe -m agentcode web --llm mock
```

实现上分成两层：`agentcode/web/runner.py` 把一次运行变成事件流（`status` / `step` / `answer` / `error`），
`agentcode/web/server.py` 用标准库 `http.server` 把它以 SSE 推给浏览器。整个过程**不新增任何依赖**。
服务只监听 `127.0.0.1`，不做鉴权，请勿暴露到公网。

## 架构

```
CLI (agentcode.cli)  ──  agentcode web / open  ──▶  Web 层 (agentcode.web)
      │                                                    │
      ▼                                                    ▼
Agent 实现 (agentcode.agents.*)  ── 注册到 ──▶  AgentRegistry
      │
      ├──▶ BaseAgent (agentcode.core.agent) ──▶ 上下文记忆 + Middleware 链（日志/重试/超时/用量）
      ├──▶ BaseLLM   (agentcode.llm.*)      ── openai 兼容实现 + 离线脚本模型
      ├──▶ ToolRegistry (agentcode.tools.*) ── 内置工具 + 受限代码执行/文件工具
      └──▶ Memory (agentcode.memory.*)      ── 短期记忆、磁盘会话、结果落盘

配置：agentcode.config.Settings  ←  .env / JSON 配置文件
结果：AgentResult / Step（可导出为完整结果 JSON）
```

目录一览：

| 路径 | 职责 |
| --- | --- |
| `agentcode/core/` | 基类、注册表、解析器、运行上下文与结果结构 |
| `agentcode/llm/` | LLM 后端：OpenAI 兼容 + 离线脚本模型 |
| `agentcode/tools/` | 工具注册表、内置工具、受限代码执行与文件工具 |
| `agentcode/memory/` | 短期记忆、磁盘会话读写、结果落盘 |
| `agentcode/middleware/` | 日志、重试、超时、token 统计 |
| `agentcode/agents/` | ReAct、Plan-and-Solve、Reflection、Coding、Echo |
| `agentcode/web/` | 本地网页：事件流运行器、会话表、零依赖 HTTP 服务 |
| `启动网页.cmd` | 双击启动服务并打开浏览器 |
| `tests/` | 全离线单元测试 |

## 三种范式的示例输出

命令行会打印完整轨迹（终端本来就是给开发者看的），网页只给结果。

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

coding 智能体的真实运行（节选）：

```
[步骤 1] 行动：write_file[path="test_prime.py", content="..."]   → 已写入 test_prime.py（306 字符）
[步骤 2] 行动：write_file[path="prime.py", content="..."]        → 已写入 prime.py（163 字符）
[步骤 3] 行动：run_python[code="import pytest, sys; sys.exit(pytest.main(['-q']))"]
         观察：退出码：0 --- stdout --- ..... [100%]
[步骤 4] 行动：Finish[已通过 5 个 pytest 用例；改动文件：test_prime.py、prime.py]
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
`self._new_context()` 建运行上下文；需要更多步数就声明 `default_max_steps`。
在 `agentcode/agents/__init__.py` 里导入一次即完成注册。

### 新增一个工具

```python
@tools.tool(description="把两个数相加。", parameters={"a": "加数", "b": "被加数"})
def add(a: str, b: str) -> str:
    return str(int(a) + int(b))
```

工具内部抛出的异常会被统一转换成中文错误字符串，作为 Observation 回到循环里。
引号包裹的参数会还原 `\n`、`\t` 等转义，所以模型写多行文件内容是可靠的。

### 新增一个中间件

继承 `agentcode.middleware.base.Middleware`，覆盖 `wrap_llm` / `wrap_tool` 返回新的调用函数即可。

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
| `AGENT_MAX_STEPS` | 否 | `6` | 闲聊类智能体的最大步数 |
| `AGENT_CODING_STEPS` | 否 | `20` | coding 智能体的最大步数 |
| `AGENT_MEMORY_TURNS` | 否 | `5` | 每个会话记住几轮上下文 |
| `AGENT_MAX_SESSIONS` | 否 | `20` | 网页同时保留多少个会话 |
| `AGENT_WEB_SESSION_DIR` | 否 | `traces/web-sessions` | 网页会话（名字 + 完整记录）落盘目录 |
| `AGENT_CODE_ROOT` | 否 | `traces/sandbox` | 代码执行的工作目录 |
| `AGENT_CODE_TIMEOUT` | 否 | `10` | 单次代码执行超时（秒） |
| `AGENT_CODE_OUTPUT_LIMIT` | 否 | `4000` | 执行输出截断长度（字符） |
| `AGENT_TRACE_DIR` | 否 | `traces` | 轨迹默认输出目录 |

配置文件（`--config configs/example.json`）可以覆盖上面的数值型字段，
网页也可以用 `web_host`、`web_port`、`llm_mode` 设默认值（`llm_mode` 默认 `openai`）。
`.env` 的查找顺序是：显式传入的路径 → 当前目录向上最多三层 → 进程环境变量。

## 测试

```powershell
D:\Anaconda\python.exe -m pytest -q
```

223 项用例全部离线运行，不产生任何网络请求，也不消耗 API 额度。覆盖重点：

- 代码执行：stdout/stderr 回传、超时终止、**子进程看不到密钥**、输出截断、路径逃逸被拒；
- coding 闭环：scripted 模型驱动「初版写错 → 测试失败 → 读报错 → 改对 → 全绿」；
- 行动解析：引号内 `\n`/`\t` 转义还原、模型顺手编的 Observation 后缀被切掉；
- 行动解析（续）：一次回复里重复的 Thought/Action 块、提示词条目文字当前缀、
  值里未转义的引号与逗号，都要能正确还原；
- 运行产物：快照对比能认出新增/改动文件、产物随消息落盘、`/api/file` 只能在代码目录内读取；
- 上下文记忆：第二轮提示词能读到第一轮答案、超限裁剪、真实轮次不回退、会话隔离与磁盘会话；
- 多会话：保留旧会话、按首条提问自动命名、手动改名不被覆盖、切回旧会话能还原记录与记忆、删除、落盘后重开仍在；
- 网页：真实 HTTP + SSE 端到端、视觉规范（流动背景 / 半透明面板 / 品牌蓝）、页面不出现推理过程与离线选项。

## 设计文档与实现计划

- 框架设计：`docs/superpowers/specs/2026-09-22-agentcode-framework-design.md`
- 实现计划：`docs/superpowers/plans/2026-09-22-agentcode-framework.md`
- Coding 智能体设计：`docs/superpowers/specs/2026-09-22-coding-agent-design.md`
