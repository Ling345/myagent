# AgentCode：可扩展的 Python 智能体框架

> **软件工程 · Homework 1（Code Agent）**：本项目的作业方向是**测试生成 Agent**——
> 为指定源码生成 pytest 用例，并**真的跑通**（生成 → 运行 → 读报错 → 修正 → 全绿）。
> 设计说明与评分点对照见 [Design.md](Design.md)。

一个把「智能体范式」做成可插拔组件的教学向框架：LLM 后端、工具、记忆、中间件都能替换，
新增一个智能体只需要**一个文件**加一个注册装饰器；附带一个本地网页，只给结果，但记住你们的对话。

> 已按「能收费的产品」完成阶段一：**账号体系 + 每用户会话隔离 + 每日 token 配额 + 默认关闭代码执行**。
> 详见下面「账号与配额」一节。

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

### 测试生成（本次作业方向）

```powershell
# 真实模型：为指定源码生成 pytest 用例，生成后真的跑一遍，不通过就改到通过
D:\Anaconda\python.exe -m agentcode run --agent test_gen --file examples\sample_code\calculator.py

# 离线演示（不消耗额度，内置脚本模型，配套上面那个示例文件）
D:\Anaconda\python.exe -m agentcode run --agent test_gen --llm mock --file examples\sample_code\calculator.py
```

`test_gen` 有五条纪律写进提示词并有测试守着：先读源码再写测试、**不修改被测源文件**、
只跑自己写的测试文件、必须覆盖边界（空输入/零/负数/非法类型/`pytest.raises`）、
结论只说"为哪个文件生成了测试、覆盖了什么、测试文件叫什么"。
实测：为 `calculator.py` 生成 8 个用例，独立复跑 `pytest -q test_calculator.py` 全部通过。

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

### 联网搜索

内置工具 `web_search`（基于 SerpApi），用于查时事、天气、价格、最新版本这类模型知识库之外
或可能过期的信息。工具返回标题与摘要（**不带链接**——模型会把链接照抄进答案，而答案要保持干净）：

```
[1] DeepSeek V4.1 Flash：更强、更快、更普惠
北京时间 2026 年 9 月 14 日 12:00 之后 …… 将全部路由到 V4.1 Flash
```

答案风格有两条硬约束（写在提示词里，并有测试守着）：**不列网址或"来源："**；
coding 智能体的答案**不提测试、用例、通过与否、命令或 stdout**——
测试只是它自己的验证手段，产物文件在页面下方点开就能看。

需要在 `.env` 里配 `SERPAPI_API_KEY`（免费额度每月 100 次搜索）。三种失败都有明确提示：
没配密钥、密钥无效或过期、搜索额度用完。工具描述里写明了"不确定或需要最新信息时应当使用它，
不要凭记忆回答"，所以只要问题涉及实时信息，ReAct 智能体会主动去搜。

> 修过的坑：这个工具一度**完全不可用**——它从 `os.environ` 读密钥，而框架是直接解析 `.env`
> 成 `Settings` 的，不会污染进程环境变量，于是每次都返回"未配置"。现在密钥由框架从配置注入，
> 并有回归测试守着（`tests/test_web_search.py`）。

### 执行后端一：`local`（默认，本机受限）

工具默认把代码跑在 `AGENT_CODE_ROOT`（默认 `traces/sandbox`），**不碰项目源码**。约束：

| 约束 | 做法 |
| --- | --- |
| 工作目录 | 固定在代码根目录，文件工具也只能访问这里（`..` 逃逸与外部绝对路径被拒绝） |
| 超时 | 默认 10 秒（`AGENT_CODE_TIMEOUT`），超时杀进程并返回中文提示 |
| 环境变量 | 只传白名单（PATH/TEMP 等），**不传任何密钥**，子进程读不到 `LLM_API_KEY` |
| 输出长度 | 截断到 `AGENT_CODE_OUTPUT_LIMIT`（默认 4000 字符），避免刷爆上下文 |

必须说清楚：Python 子进程本身仍能访问文件系统与网络，这套约束防的是"误伤与跑飞"，
不是防御恶意代码。所以它只是**开发期默认值**。

### 执行后端二：`docker`（面向公网时用这个）

把 `AGENT_EXECUTION_BACKEND` 设成 `docker`，代码就改在一次性容器里执行：

| 隔离项 | 做法 |
| --- | --- |
| 网络 | `--network none`，容器内**完全不能联网** |
| 文件系统 | `--read-only` 根文件系统只读，只有 `AGENT_CODE_ROOT` 挂成可写，`/tmp` 是 32MB 内存盘 |
| 权限 | `--user 65534:65534`（nobody），非 root |
| 资源 | `--memory 256m`、`--cpus 0.5`、`--pids-limit 64`（防内存爆炸与 fork 炸弹） |
| 生命周期 | `--rm` 用完即删；超时先杀容器再回报，不留残骸 |
| 密钥 | 容器里只注入 `PYTHONPATH`/`PYTHONIOENCODING`，宿主机的 `.env` 与密钥一律看不见 |

关键设计：**选了 docker 但环境不可用时直接报错，绝不悄悄退回本机执行**——
"以为隔离了其实没隔离"比不隔离更危险，这条有回归测试守着（`tests/test_sandbox.py`）。

#### 为什么需要一个专用沙箱镜像

官方的 `python:3.13-slim` 里**没有 pytest**，而容器运行期是断网的，装不了包。
所以测试依赖必须在**构建期**烤进镜像：

```powershell
# 构建（默认走 PyPI；国内建议走清华镜像，快很多）
docker build -t agentcode-sandbox:1.0 `
  --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple docker/sandbox
```

嫌命令长就直接跑打包好的脚本，它顺带会自检镜像（确认非 root、pytest 可用）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_sandbox_image.ps1
```

`docker/sandbox/Dockerfile` 只做三件事：基于 `python:3.13-slim`、装 pytest、设好工作目录。

#### 三种装 Docker 的方式

| 方式 | `AGENT_DOCKER_BINARY` | 说明 |
| --- | --- | --- |
| Docker Desktop（Windows） | `docker` | 图形界面，装机最省事；默认占 C 盘，且数据目录要另外指到 D 盘 |
| Docker 引擎装在 WSL 里 | `wsl -d Ubuntu -- docker` | **本项目当前采用**。不用装桌面端、不用图形界面，整块磁盘跟着 WSL 发行版走 |
| Linux 服务器 | `docker` | 生产环境的标准做法 |

当前这台机器的实际配置（Docker 引擎在 WSL 的 Ubuntu 24.04 里，发行版整个放在 D 盘）：

```dotenv
AGENT_EXECUTION_BACKEND=docker
AGENT_DOCKER_BINARY=wsl -d Ubuntu -- docker
AGENT_DOCKER_IMAGE=agentcode-sandbox:1.0
```

自检命令：

```powershell
# 看守护进程在不在（正常应输出一个版本号）
wsl -d Ubuntu -- docker info --format "{{.ServerVersion}}"

# 直接手工跑一个隔离容器，确认断网 + 非 root
wsl -d Ubuntu -- docker run --rm --network none --read-only --user 65534:65534 `
  agentcode-sandbox:1.0 python -c "import os; print('uid', os.getuid())"
```

> Docker 引擎在 WSL 里是 `systemd` 托管的开机自启服务，随发行版一起启动，
> 平时不需要单独去开它；第一次调用如果 WSL 恰好处于停止状态，会多花几秒唤醒。

配置项一览：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AGENT_EXECUTION_BACKEND` | `local` | `local` 或 `docker` |
| `AGENT_DOCKER_IMAGE` | `python:3.13-slim` | 执行镜像 |
| `AGENT_DOCKER_BINARY` | `docker` | docker 命令本身，可写成 `wsl -d Ubuntu -- docker` |
| `AGENT_DOCKER_MEMORY` | `256m` | 容器内存上限 |
| `AGENT_DOCKER_CPUS` | `0.5` | 容器 CPU 上限 |
| `AGENT_DOCKER_PIDS_LIMIT` | `64` | 容器内进程数上限 |
| `AGENT_DOCKER_USER` | `65534:65534` | 容器内运行用户；留空则用镜像默认用户 |

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

### 账号与配额

网页端默认**必须登录**。第一次启动如果账号库是空的，会自动创建一个管理员账号并把密码**只打印一次**：

```
========================================================
已创建初始账号　用户名：admin　密码：xxxxxxxxxxx
请立刻记下这个密码，它只显示这一次。
========================================================
```

账号管理走命令行（邀请制，避免公网被刷注册）：

```powershell
D:\Anaconda\python.exe -m agentcode user add alice           # 开户，密码自动生成并打印一次
D:\Anaconda\python.exe -m agentcode user add bob --password 自定义 --plan pro --daily-limit 200000
D:\Anaconda\python.exe -m agentcode user list                # 账号 + 今日用量
D:\Anaconda\python.exe -m agentcode user usage alice         # 最近 7 天用量
D:\Anaconda\python.exe -m agentcode user limit alice 500000  # 卖套餐时调额度
D:\Anaconda\python.exe -m agentcode user passwd alice         # 改密码
D:\Anaconda\python.exe -m agentcode user disable bob         # 停用（欠费/违规）
```

| 能力 | 实现 |
| --- | --- |
| 身份 | SQLite 账号表（`AGENT_DB_PATH`），PBKDF2-SHA256 加盐哈希，不存明文 |
| 登录态 | HMAC 签名 HttpOnly Cookie（`AGENT_SECRET_KEY`），改一个字符即失效，7 天过期 |
| 会话隔离 | 每个账号独立目录 `traces/web-sessions/<账号id>/`，看不到也猜不到别人的会话 |
| 配额 | 按（账号, 日期）累计 token，超额返回 402 并说明原因，次日自动重置 |
| 限流 | 每人每分钟 `AGENT_RATE_LIMIT_PER_MINUTE`（默认 30）次请求，超出返回 429 并带 `Retry-After` |
| 并发 | 每人同时最多 `AGENT_MAX_CONCURRENT_RUNS`（默认 2）个任务在跑，多开的请求同样 429 |
| 单次预算 | 单次任务超出 `AGENT_RUN_TOKEN_BUDGET`（默认 30000）token 会**中途中断**并说明原因 |
| 运维 | `/healthz` 免登录探活；每请求一行 JSON 日志（请求 id、用户、路径、状态、耗时） |

> 公网部署务必固定 `AGENT_SECRET_KEY`（否则每次重启都要求重新登录），
> 并保持 `AGENT_ALLOW_CODE_TOOLS=false`——没有容器隔离时，网页端执行代码是危险的。
> 本地自用想免登录：`agentcode web --no-auth`（会打印醒目警告）。
>
> 登录接口额外限流（每 IP 每分钟 10 次），防止有人拿脚本暴力试探密码。

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
| `AGENT_EXECUTION_BACKEND` | 否 | `local` | 代码执行后端：`local` 或 `docker` |
| `AGENT_DOCKER_IMAGE` | 否 | `python:3.13-slim` | 容器执行镜像 |
| `AGENT_DOCKER_BINARY` | 否 | `docker` | docker 命令，可写成 `wsl -d Ubuntu -- docker` |
| `AGENT_DOCKER_MEMORY` | 否 | `256m` | 容器内存上限 |
| `AGENT_DOCKER_CPUS` | 否 | `0.5` | 容器 CPU 上限 |
| `AGENT_DOCKER_PIDS_LIMIT` | 否 | `64` | 容器内进程数上限 |
| `AGENT_DOCKER_USER` | 否 | `65534:65534` | 容器内运行用户，留空则不传 `--user` |
| `AGENT_TRACE_DIR` | 否 | `traces` | 轨迹默认输出目录 |

配置文件（`--config configs/example.json`）可以覆盖上面的数值型字段，
网页也可以用 `web_host`、`web_port`、`llm_mode` 设默认值（`llm_mode` 默认 `openai`）。
`.env` 的查找顺序是：显式传入的路径 → 当前目录向上最多三层 → 进程环境变量。

## 测试

```powershell
D:\Anaconda\python.exe -m pytest -q
```

285 项用例全部离线运行，不产生任何网络请求，也不消耗 API 额度。覆盖重点：

- 代码执行：stdout/stderr 回传、超时终止、**子进程看不到密钥**、输出截断、路径逃逸被拒；
- coding 闭环：scripted 模型驱动「初版写错 → 测试失败 → 读报错 → 改对 → 全绿」；
- 行动解析：引号内 `\n`/`\t` 转义还原、模型顺手编的 Observation 后缀被切掉；
- 行动解析（续）：一次回复里重复的 Thought/Action 块、提示词条目文字当前缀、
  值里未转义的引号与逗号，都要能正确还原；
- 运行产物：快照对比能认出新增/改动文件、产物随消息落盘、`/api/file` 只能在代码目录内读取；
- 联网搜索：密钥从配置注入（不依赖环境变量）、优先取直接答案、有机结果带来源链接、
  密钥无效/额度用完/一般错误各有对应提示；
- 账号与配额：密码哈希校验、重复账号/停用/改密、登录态签名与防篡改、未登录一律 401、
  **两个用户互相看不到对方的会话**、额度耗尽返回 402、跑完把用量记到账号名下；
- 限流与预算：按 key 的滑动窗口计数、窗口过期后恢复、并发名额占用与释放、
  超出单次 token 预算时**中途中断**、登录接口暴力试探防护（第 11 次返回 429）；
- 上下文记忆：第二轮提示词能读到第一轮答案、超限裁剪、真实轮次不回退、会话隔离与磁盘会话；
- 多会话：保留旧会话、按首条提问自动命名、手动改名不被覆盖、切回旧会话能还原记录与记忆、删除、落盘后重开仍在；
- 网页：真实 HTTP + SSE 端到端、视觉规范（流动背景 / 半透明面板 / 品牌蓝）、页面不出现推理过程与离线选项。

## 设计文档与实现计划

- 框架设计：`docs/superpowers/specs/2026-09-22-agentcode-framework-design.md`
- 实现计划：`docs/superpowers/plans/2026-09-22-agentcode-framework.md`
- Coding 智能体设计：`docs/superpowers/specs/2026-09-22-coding-agent-design.md`
