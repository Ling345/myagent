# AgentCode：可扩展的 Python 智能体框架

[![CI](https://github.com/Ling345/myagent/actions/workflows/ci.yml/badge.svg)](https://github.com/Ling345/myagent/actions/workflows/ci.yml)

> **软件工程 · Homework 1（Code Agent）**：本项目的作业方向是**测试生成 Agent**——
> 为指定源码生成 pytest 用例，并**真的跑通**（生成 → 运行 → 读报错 → 修正 → 全绿）。
> 设计说明与评分点对照见 [Design.md](Design.md)。

一个把「智能体范式」做成可插拔组件的教学向框架：LLM 后端、工具、记忆、中间件都能替换，
新增一个智能体只需要**一个文件**加一个注册装饰器；附带一个本地网页，只给结果，但记住你们的对话。

> **接手这个项目**（新开对话、换人、过一阵再回来）先读 [`docs/state.md`](docs/state.md)：
> 里面有当前 schema 版本、已完成与未完成清单、命令速查和踩过的坑。

> 已按「能收费的产品」完成阶段一：**账号体系 + 每用户会话隔离 + 每日 token 配额 + 默认关闭代码执行**。
> 详见下面「账号与配额」一节。
>
> 阶段二进行中：**容器化代码执行 + 频率/并发限流 + 单次 token 预算 + 多 key 轮换熔断**。
> 详见「执行后端二：docker」与「多 key 轮换与熔断」两节。
>
> 阶段三进行中：**运行状态独立于浏览器连接（刷新/断网都能续上）**。
> 详见「运行状态与断线续传」一节。
>
> 阶段四进行中：**套餐、用量账本与订单——能收费了**。
> 详见「套餐与计费」一节。
>
> 阶段五进行中：**指标与阈值告警——出问题看得见了**。
> 详见「指标与告警」一节。
>
> 阶段六进行中：**文件上传 + 代码目录按用户隔离**。
> 详见「上传文件」一节。
>
> 阶段七进行中：**隐私政策、数据导出与注销**。
> 详见「数据与隐私」一节。
>
> 阶段八进行中：**把测试生成收窄成能放进工作流的场景**（`agentcode test-gen` + GitHub Action）。
> 详见「批量补测试」一节。
>
> 阶段九进行中：**算清楚成本账**（账本分开记输入输出 token + `billing costs` 毛利表）。
> 详见「算清楚这笔账」一节。
>
> 阶段十进行中：**备份与恢复演练**（`agentcode backup`：打包、真打开库校验、恢复留底）。
> 详见「备份与恢复」一节。
>
> 阶段十一进行中：**误删能撤销 + 操作有记录**（回收站，以及「谁在什么时候删了什么」的
> 审计日志）。详见「回收站与审计日志」一节。
>
> 阶段十二进行中：**出了事找得到人**（账号通知邮箱 + webhook/SMTP 两个渠道，
> 套餐到期、额度用尽、账号被猜密码、注销完成都会通知本人）。
> 详见「出了事找得到人：通知」一节。
>
> 阶段十三进行中：**对外开放 API**（API 令牌 + `POST /v1/run` / `GET /v1/me`，
> 让脚本和 CI 也能用，用量记进同一本账）。详见 [`docs/api.md`](docs/api.md)。

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

### 批量补测试：`agentcode test-gen`

上面那条是"给它一个文件、看它怎么做"。真要用起来，是这个子命令：

```powershell
# 给一个文件补测试（写到源文件旁边）
D:\Anaconda\python.exe -m agentcode test-gen src\utils.py

# 给整个目录补测试（递归找 .py，自动跳过已有测试与 conftest）
D:\Anaconda\python.exe -m agentcode test-gen src\

# 只看看会处理哪些文件，不真的跑
D:\Anaconda\python.exe -m agentcode test-gen src\ --dry-run

# 写到 tests/ 目录；已有的测试文件要覆盖得显式加 --force
D:\Anaconda\python.exe -m agentcode test-gen src\ --out tests --force
```

**退出码是一道门禁**：`0` = 全成（写好了或者本来就有测试所以跳过）；`1` = 有文件没搞定。
放进 CI 就能拦住"生成了但跑不通"。

三个刻意的设计：

| 决定 | 为什么 |
| --- | --- |
| 测试写到**源文件旁边**（或 `--out` 指定的目录） | agent 的工作目录是内部实现，用户不该去那儿找产物 |
| 已有测试文件**默认跳过**，要覆盖必须 `--force` | 静默覆盖别人写好的测试，是最容易让人拉黑一个工具的行为 |
| **"模型说成功了"不算成功** | 工作目录里必须真的有测试文件落下来才算，否则报失败——不然用户看到一片绿，实际什么都没生成 |

### 放进 CI：`.github/workflows/test-gen.yml`

仓库里带了一个现成的 workflow：PR 有 Python 改动时，自动为改动过的源码文件补测试，
把生成的测试作为 artifact 传上来。

设计取向是**助手而不是裁判**——默认不拦 PR（写不出测试不该红叉拦人），
结果放在 step summary 里，想变成门禁把最后那步的 `|| true` 去掉即可。

要让它真的跑起来，需要在仓库的 **Settings → Secrets and variables → Actions** 里配：

| 类型 | 名字 | 值 |
| --- | --- | --- |
| Secret | `LLM_API_KEY` | 你的模型密钥 |
| Variable | `LLM_BASE_URL` | 例如 `https://api.deepseek.com` |
| Variable | `LLM_MODEL_ID` | 例如 `deepseek-chat` |

**没配就整条跳过，不报错**——免得每个 PR 都挂一个红叉。

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

## 多 key 轮换与熔断

单把 key 是**单点故障**：额度打满、被限流、被风控，整个服务立刻不可用。所以支持一次配多把：

```dotenv
# 只有一把 key 时填这个（老配置不用改）
LLM_API_KEY=sk-aaa
# 有多把 key 时填这个，逗号或换行分隔；配了它就以它为准
LLM_API_KEYS=sk-aaa,sk-bbb,sk-ccc
```

行为：

| 机制 | 说明 |
| --- | --- |
| 轮换 | 请求在多把 key 之间轮流用，不会把某一把打爆 |
| 熔断 | 某把 key 连续失败 `AGENT_KEY_FAILURE_THRESHOLD`（默认 3）次就临时摘掉 |
| 恢复 | 冷却 `AGENT_KEY_COOLDOWN_SECONDS`（默认 60）秒后自动放回池子，成功一次就彻底复位 |
| 智能重试 | 限流/额度/失效/5xx 会换下一把 key 接着试；400/422 这种"我们自己请求写错了"不换 |
| 兜底提示 | 全部 key 都在冷却时，报错里会带上"最快多少秒后恢复" |

想看当前状态，跑 `D:\Anaconda\python.exe -m agentcode config`——
它只打印脱敏后的 key 摘要，不会泄露明文。

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

## 运行状态与断线续传

一次任务要调用模型十几二十次、跑几十秒。这期间用户刷新页面、切走标签页、
网络抖一下，连接就断了。如果事件只活在连接里，用户就再也看不到结果——
更要命的是额度照样在扣，而他什么都不知道。

所以运行的状态放在服务端（`agentcode/web/runs.py` 的运行注册表），**连接只是订阅者**：

| 场景 | 行为 |
| --- | --- |
| 刷新页面 / 关标签页 | 任务在服务端继续跑完，结果照常落进会话记录 |
| 重新打开这个会话 | 页面自动查 `/api/runs`，发现有没跑完的就连上去继续看 |
| 连接中途断了 | 前端带着 `from=<已收到的事件数>` 重连 `/api/run/stream`，漏掉的部分补回来 |
| 用量与并发闸门 | 挂在运行结束的收尾钩子上——没人看着也照扣、照释放，不会把自己锁死 |
| 事件缓冲 | 每个运行最多留 5000 条，老事件滚动丢弃并显式告知 `truncated`，下标不会串位 |
| 记录回收 | 跑完的运行默认留 30 分钟、最多 100 条，避免内存无限涨 |

想自己验一把：跑起网页后让智能体干个活，中途按 F5 刷新——任务不会白跑，
页面会自己接上去。命令行同理，可以看 `GET /api/runs` 里那次运行的状态。

## 指标与告警

服务跑起来之后，「模型失败率多少、容器是不是变慢了、哪把 key 被熔断、
谁在刷额度」这些问题得答得上来。日志是一行行的，统计只能人肉 grep，所以有了指标。

### `GET /metrics`

Prometheus 文本格式，纯文本、肉眼可读：

```
# TYPE agentcode_http_requests_total counter
agentcode_http_requests_total{path="/api/run",status="200"} 12.0
# TYPE agentcode_llm_calls_total counter
agentcode_llm_calls_total{key="1",result="ok"} 8.0
agentcode_llm_calls_total{key="0",result="failed"} 3.0
# TYPE agentcode_llm_call_seconds histogram
agentcode_llm_call_seconds_bucket{key="1",le="1"} 6
agentcode_llm_call_seconds_sum{key="1"} 7.4
agentcode_llm_call_seconds_count{key="1"} 8
# TYPE agentcode_sandbox_runs_total counter
agentcode_sandbox_runs_total{backend="docker",result="ok"} 5.0
# TYPE agentcode_keypool_trips_total counter
agentcode_keypool_trips_total{key="0"} 1.0
```

| 指标 | 说明 |
| --- | --- |
| `agentcode_http_requests_total{path,status}` | 请求数。路径收敛成有限集合，未知路径归 `other`、静态资源归 `/static`——否则被人拿随机 URL 扫站就能把标签基数撑爆 |
| `agentcode_http_request_seconds` | 请求耗时分布 |
| `agentcode_runs_total{agent,result}` | 任务数。`result` 是 `ok` / `error` / `empty`（流结束了却没有答案，通常是跑到步数上限） |
| `agentcode_run_seconds` | 任务耗时分布 |
| `agentcode_llm_calls_total{key,result}` | 模型调用。`key` 是**下标**而不是脱敏明文——指标要往监控系统送，少暴露一点是一点；下标顺序与 `agentcode config` 里那串脱敏 key 一致 |
| `agentcode_keypool_trips_total{key}` | 某把 key 被熔断了几次 |
| `agentcode_sandbox_runs_total{backend,result}` | 沙箱执行。`result` 是 `ok` / `failed` / `timeout` / `error`，`backend` 区分 `local` 与 `docker` |
| `agentcode_rejections_total{reason}` | 被拒的请求：`quota` / `rate_limit` / `concurrency` / `unauthorized` / `bad_credentials` |
| `agentcode_alerts_total{rule}` | 触发过几次告警 |

**访问控制**：绑在 `127.0.0.1` 上时不要求登录，方便直接 `curl http://127.0.0.1:8000/metrics`；
一旦监听到别的地址就**必须登录**——指标里有请求路径、账号名和 key 指纹，不该对公网敞开。

### 阈值告警

告警不另起一套埋点：**指标就是唯一的事实来源**。`agentcode/alerts.py` 在固定时间点
给指标拍快照，用两次快照的差算窗口内的量——调用方一行都不用多写，
也就不会出现「指标和告警各记各的、对不上」这种经典问题。

| 规则 | 触发条件 | 为什么 |
| --- | --- | --- |
| `http_error_rate` | 5xx 占比超过阈值，且样本数够 | 「服务挂了」最直接的信号 |
| `llm_failure_rate` | 模型调用失败占比超过阈值，且样本数够 | 上游挂了 / key 全废了 |
| `sandbox_timeout` | 窗口内出现过超时 | 通常是模型写了个死循环 |
| `keypool_trip` | 窗口内有 key 被熔断 | key 额度用完或失效 |

两个刻意的设计：

- **4xx 不参与错误率**。没登录、参数写错是用户自己的问题，为它们半夜报警，
  只会让人学会无视告警。
- **样本太少不做比例判断**（默认 <20）。刚起来两个请求里有一个 5xx，
  不代表服务挂了。

同一规则有冷却（默认 15 分钟），不会刷屏。

```dotenv
# 不配 webhook 就只在日志里报，不往外推——本地自己用没必要接
AGENT_ALERT_WEBHOOK=https://hooks.example.com/你的密钥
AGENT_ALERT_WINDOW_SECONDS=300
AGENT_ALERT_COOLDOWN_SECONDS=900
AGENT_ALERT_ERROR_RATE=0.5
```

推送的载荷长这样（飞书 / 钉钉 / Slack 的机器人 webhook 都能直接收）：

```json
{
  "rule": "llm_failure_rate",
  "severity": "critical",
  "message": "最近窗口内模型调用失败率 100%（6/6），检查上游或 API key。",
  "details": { "ratio": 1.0, "failed": 6, "total": 6 }
}
```

推送失败只记一行日志，不影响服务本身。

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
| 配额 | 按（账号, 日期）累计 token，超额返回 402 并说明原因，次日 UTC 零点重置；**上限由套餐决定** |
| 限流 | 每分钟请求数**由套餐决定**，超出返回 429 并带 `Retry-After` |
| 并发 | 同时最多几个任务在跑**由套餐决定**，多开的请求同样 429 |
| 单次预算 | 单次任务超出 `AGENT_RUN_TOKEN_BUDGET`（默认 30000）token 会**中途中断**并说明原因 |
| 运维 | `/healthz` 免登录探活；每请求一行 JSON 日志（请求 id、用户、路径、状态、耗时） |

> 公网部署务必固定 `AGENT_SECRET_KEY`（否则每次重启都要求重新登录），
> 并保持 `AGENT_ALLOW_CODE_TOOLS=false`——没有容器隔离时，网页端执行代码是危险的。
> 本地自用想免登录：`agentcode web --no-auth`（会打印醒目警告）。
>
> 登录接口额外限流（每 IP 每分钟 10 次），防止有人拿脚本暴力试探密码。

### 套餐与计费

额度由**套餐**决定，不再由账号上的数字决定。套餐目录在 `agentcode/plans.py`：

| 套餐 | 日 token | 每分钟 | 并发 | 代码执行 | 会话数 | 月价 |
| --- | --- | --- | --- | --- | --- | --- |
| `free` 免费 | 2 万 | 10 | 1 | 否 | 5 | 免费 |
| `basic` 基础 | 20 万 | 30 | 2 | 是 | 20 | ¥29 |
| `pro` 专业 | 100 万 | 60 | 4 | 是 | 50 | ¥99 |
| `team` 团队 | 500 万 | 120 | 8 | 是 | 200 | ¥399 |
| `owner` 内部 | 不限 | 不限 | 不限 | 是 | 不限 | 不可售 |

> **行为变化**：升级到本版本后，账号上原先手工设的每日额度不再生效，额度以套餐为准。
> 需要给某个人单独提额时用 `agentcode user limit <账号> <token数>`，
> 它会写成**账号级覆盖**，优先级高于套餐。

**用量账本**：每次运行往 `usage_ledger` 记一条，带运行编号（能追到具体是哪次运行）。
今日用量、区间用量、历史曲线都从账本汇总，可导出 CSV。老库里的日计数器会在升级时自动搬进账本。

### 上传文件

输入框下面有「上传文件」，也可以把文件**直接拖进去**（拖上去会高亮）。传完直接说
「给这个文件生成测试」就行——文件落在 agent 的代码工作目录里，`read_file` 本来就在那儿。
传成功后会出现一个文件名标签，点开就能看内容（复用已有的文件查看器）。

| 约定 | 值 |
| --- | --- |
| 类型 | 只收 **UTF-8 文本**；含 NUL 字节的当二进制拒掉（`read_file` 也只读文本，二进制对 agent 没用） |
| 单个文件 | 2 MB（`AGENT_UPLOAD_MAX_BYTES`） |
| 每人总量 | 50 MB（`AGENT_UPLOAD_QUOTA_BYTES`） |
| 同名文件 | 覆盖；覆盖**不计入**新增配额，否则用户改不了自己那个文件 |
| 文件名 | 只取最后一段，`\` 与 `/` 都当分隔符（Windows 与 Linux 都要挡），`.` 与 `..` 直接拒 |

> **每个账号一个代码目录**。会话目录一直是按账号分的，代码目录**曾经是全局共享的**——
> 也就是说 A 让 agent 写进去的文件，B 换个账号就能读到。这个洞在上传功能之前先堵上了：
> 现在登录用户的代码目录是 `AGENT_CODE_ROOT/<账号id>/`，免登录本地自用模式仍用共享目录。

**买套餐**：网页左侧账号栏点「套餐与用量」，可以看到当前套餐、到期时间、额度进度条、近 7 天用量，
以及套餐卡片。点「升级」会生成一张**待支付**订单——现在只有手动开通这一条渠道。

**管理动作只在命令行**（网页不开放，避免给自己留一条自助提权的路）：

```powershell
D:\Anaconda\python.exe -m agentcode user plan alice pro --months 3      # 换套餐 / 续期
D:\Anaconda\python.exe -m agentcode billing orders                      # 看订单（加 --json 给脚本用）
D:\Anaconda\python.exe -m agentcode billing grant alice basic --months 1  # 收到钱后直接开通
D:\Anaconda\python.exe -m agentcode billing confirm <订单号> --reference "微信转账 20260929"
```

续期规则：同一个套餐且没过期，就从原来的到期日往后加；换套餐、或者已经过期，就从今天重新起算。
`owner` 是内部套餐，永不过期、不可购买。

**支付渠道是可替换的**：实现 `agentcode/billing.py` 里的 `PaymentProvider` 协议
（`create_order` + `confirm`），就能接微信 / 支付宝 / Stripe；上层业务代码一行都不用改。
金额一律用「分」存整数，不用浮点。

**数据库迁移**：schema 现在有版本号（`schema_meta`），打开老库会自动升级。
迁移**只增不改**（加列不删列）、逐条事务、可重复执行。升级时自动备份好 `traces/agentcode.db`
就不会有风险——真失败了它会整体回滚并把版本停在上一条。

### 算清楚这笔账：`agentcode billing costs`

「基础版 ¥29/月，每天 20 万 token」——这单生意是赚是亏？先配单价：

```dotenv
# .env：分 / 百万 token。例：DeepSeek 输入 ¥2/M、输出 ¥8/M
AGENT_PRICE_INPUT_PER_MILLION=200
AGENT_PRICE_OUTPUT_PER_MILLION=800
```

然后：

```powershell
D:\Anaconda\python.exe -m agentcode billing costs            # 最近 30 天
D:\Anaconda\python.exe -m agentcode billing costs --days 7   # 最近 7 天
D:\Anaconda\python.exe -m agentcode billing costs --json     # 给脚本用
```

它会给出三样东西：**这个窗口花了多少钱**、**每个账号花了多少**、
以及**每个套餐在"用户天天跑满"时的成本与毛利**。

毛利用两列给，因为只给一个数会误导：

| 列 | 含义 |
| --- | --- |
| 估算 | 按**实测**的输入/输出比例算。还没有拆分数据时会标明是假设值（默认输出占 20%，可用 `--output-ratio` 改） |
| 最坏 | 假设所有 token 都是输出（最贵的那一侧）——不现实，但那是"最坏能亏到哪"的答案 |

两个实现上的讲究：

- 账本从 v3 起**分开记输入与输出 token**。LLM 的输出单价通常是输入的 2–4 倍，
  只记总数换算出来的是假账。
- 早期数据只有总数、拆不出来，计价时会**单独标出来**按比例估，而不是假装它不存在。

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
| `LLM_API_KEYS` | 否 | 无 | 多把模型密钥（逗号/换行分隔），配了就覆盖 `LLM_API_KEY` |
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
| `AGENT_KEY_FAILURE_THRESHOLD` | 否 | `3` | 某把 key 连续失败几次就被熔断 |
| `AGENT_KEY_COOLDOWN_SECONDS` | 否 | `60` | 熔断冷却多少秒后自动放回 |
| `AGENT_ALERT_WEBHOOK` | 否 | 无 | 告警推送地址；不配则只在日志里报 |
| `AGENT_ALERT_WINDOW_SECONDS` | 否 | `300` | 告警观察窗口（秒） |
| `AGENT_ALERT_COOLDOWN_SECONDS` | 否 | `900` | 同一规则多久不重复推（秒） |
| `AGENT_ALERT_ERROR_RATE` | 否 | `0.5` | 错误率阈值 |
| `AGENT_UPLOAD_MAX_BYTES` | 否 | `2097152` | 单个上传文件的上限（2 MB） |
| `AGENT_UPLOAD_QUOTA_BYTES` | 否 | `52428800` | 每个用户代码目录的总量上限（50 MB） |
| `AGENT_METRICS_TOKEN` | 否 | 无 | 抓指标用的令牌；容器里必须绑 0.0.0.0，靠它免登录抓取 |
| `AGENT_RETENTION_DAYS` | 否 | `0` | 会话与代码文件留多少天；`0` = 不自动清理 |
| `AGENT_API_ENABLED` | 否 | `true` | 对外 API 开关；`false` 时 `/v1/*` 一律 404 |
| `AGENT_API_RUNS_DAYS` | 否 | `7` | 对外 API 任务台账保留多少天；`0` = 永久保留 |
| `AGENT_CALLBACK_SECRET` | 否 | 无 | 任务完成回调的签名密钥；留空回落 `AGENT_SECRET_KEY`，两个都没有则拒收 `callback_url` |
| `AGENT_CALLBACK_MAX_ATTEMPTS` | 否 | `4` | 一个回调最多投递几次（含首次） |
| `AGENT_CALLBACK_TIMEOUT` | 否 | `10` | 单次回调投递超时（秒） |
| `AGENT_CALLBACK_PORTS` | 否 | `80,443` | 允许回调的端口白名单 |
| `AGENT_CALLBACK_ALLOW_PRIVATE` | 否 | `false` | 允许回调内网/本机地址；只给本地开发与测试 |
| `AGENT_PRICE_INPUT_PER_MILLION` | 否 | `0` | 模型输入单价（分/百万 token）；0 = 算不了钱 |
| `AGENT_PRICE_OUTPUT_PER_MILLION` | 否 | `0` | 模型输出单价（分/百万 token）；0 = 算不了钱 |
| `AGENT_TRACE_DIR` | 否 | `traces` | 轨迹默认输出目录 |

配置文件（`--config configs/example.json`）可以覆盖上面的数值型字段，
网页也可以用 `web_host`、`web_port`、`llm_mode` 设默认值（`llm_mode` 默认 `openai`）。
`.env` 的查找顺序是：显式传入的路径 → 当前目录向上最多三层 → 进程环境变量。

## 部署到服务器

整个服务用 Docker 部署，服务器上不用装 Python：

```bash
docker build -t agentcode-sandbox:1.0 docker/sandbox   # 沙箱镜像（被调用的那个）
cp .env.example deploy/.env && nano deploy/.env        # 填密钥
export AGENTCODE_DATA=/srv/agentcode/data              # 必须是绝对路径
docker compose up -d
```

完整步骤（含 HTTPS、备份、升级回滚、以及那个绕不开的安全取舍）见
[`docs/deploy.md`](docs/deploy.md)。两件事值得先知道：

- 应用容器挂了宿主机的 `docker.sock`——因为它要靠宿主 Docker 起沙箱的**兄弟容器**。
  代价与更安全的替代方案都写在部署文档里。
- 数据（账号库、会话、每个用户的代码目录）全部落在 `$AGENTCODE_DATA` 一个目录下，
  备份就是备份它。

## 数据与隐私

网页左下角有「我的数据」：**导出**（把全部会话和代码目录打包成 zip）、
**清空代码目录**、**注销账号**（输入用户名二次确认，删账号 + 全部会话 + 全部代码）。
导出不是可选项——只能删不能导出的话，"删除"就成了单向门，用户不敢按。

留存策略默认是「**保留到你主动删除**」：不配置 `AGENT_RETENTION_DAYS` 就永不自动清理。
需要自动清理时配一个天数，服务会在启动时扫一遍、之后每天扫一遍，只删过期的会话与代码
文件，**不动账号与交易记录**。

网页上还有两份文档：`/privacy`（隐私政策）与 `/terms`（用户协议），
**免登录可读**——没登录的人也有权知道你如何处理他的数据。

### 删错了能找回来：回收站

会话和代码目录的删除都是**软删除**：先进回收站，网页「我的数据」面板里能一条条
恢复或彻底删掉。回收站放在用户工作目录**之外**（`<会话目录>/.trash/`、
`<代码根目录>/.trash/`），所以 agent 在代码目录里看不到它、也不会误读。

- 保留期 `AGENT_TRASH_DAYS`（默认 30 天，`0` = 不自动清理）；
- 运营方也能查（用户来投诉"我误删了"的时候用）：
  `agentcode trash list <账号>`、`agentcode trash restore <账号> session <条目>`；
- **注销账号不进回收站**：隐私政策承诺的是真删除，回收站会让那句话变成谎话，
  所以注销会把回收站一起删干净。

### 谁在什么时候做了什么：审计日志

登录（成功与失败）、删会话、清空代码目录、导出、回收站的恢复与彻底删、注销，
以及运营方的开户/停用/改额度/改密码/换套餐/确认订单，都会写一条审计记录进库里
（表 `audit_log`，迁移 v4）。两条硬约束：

- **绝不记密钥**：密码这类字段进门一律抹成 `***`——审计日志自己变成泄露源是经典事故；
- **绝不阻断主流程**：审计写失败只打印警告，用户的操作照常完成。

```powershell
D:\Anaconda\python.exe -m agentcode audit list                          # 最近 50 条
D:\Anaconda\python.exe -m agentcode audit list --action-prefix login.   # 所有登录尝试
D:\Anaconda\python.exe -m agentcode audit list --actor alice --json
D:\Anaconda\python.exe -m agentcode audit prune --days 180              # 清过期
```

保留期 `AGENT_AUDIT_DAYS`（默认 180 天，`0` = 永久保留）。审计只在命令行看——
和 `billing costs` 一个道理，运营数据不该摆在用户面前。

### 出了事找得到人：通知

账号里可以填一个**接收通知的邮箱**（网页「我的数据」里自己填，运营方也能
`agentcode user email <账号> --set x@y` 补录）。填了之后这四件事会通知本人：

| 什么时候 | 为什么要发 |
| --- | --- |
| 套餐还有 3 天到期 | 别让用户那天突然发现额度掉回免费档 |
| 当天额度用尽 | 他多半正卡在那儿纳闷为什么跑不动 |
| 短时间内连续登录失败 | 有人在猜密码，得让账号主人知道 |
| 注销完成 | 最后一条消息：如果这不是你干的，立刻找我们 |

渠道二选一，都是标准库、零新依赖：`AGENT_NOTIFY_WEBHOOK`（POST 一段 JSON，最省事）
或 SMTP（`AGENT_SMTP_HOST` 等，465 走隐式 TLS、其它端口走 STARTTLS）。

```powershell
D:\Anaconda\python.exe -m agentcode notify list        # 台账：发成功 / 未发（没配渠道）/ 失败
D:\Anaconda\python.exe -m agentcode notify test alice  # 真发一封试试渠道通不通
```

三条刻意的设计：

- **没配渠道不算发过**：通知会记成 `queued` 并打日志，绝不明说"已发送"——
  假的成功比不发更糟，你会以为通知过用户；
- **同一件事不反复轰**：每条通知带去重键（日期 / 本次到期时间 / 失败批次），
  发过的不重发；**失败的除外**，服务端抖一下不该让用户永远收不到；
- **通知是旁路**：发送失败只记结果、不抛异常，绝不影响用户自己的操作。

运营方需要知道的事——数据清单、你的义务、**第三方披露**（用户的提问和代码会发给模型
服务商，这条必须写在政策里）、以及还没做的部分——见
[`docs/data-and-privacy.md`](docs/data-and-privacy.md)。

### 给程序用的门：对外开放 API

网页是给人用的；脚本、CI、内部工具需要另一扇门。带一把令牌就能调：

```bash
curl -sS -X POST "$AGENTCODE_URL/v1/run" \
  -H "Authorization: Bearer $AGENTCODE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"agent": "coding", "task": "给 utils.py 补 pytest 测试", "session_id": "ci-42"}'
```

```json
{"run_id": "9f2c…", "success": true, "answer": "已生成 tests/test_utils.py，5 项测试全过。",
 "usage": {"total_tokens": 1834, "prompt_tokens": 1502, "completion_tokens": 332}}
```

令牌在网页「我的数据」里自己建（运营方也能 `agentcode token create <账号> --name CI`）：

- **只存哈希**，明文只在创建时显示一次；能吊销、能设过期、能按用途建多把；
- 调用走的是**同一套**配额、并发闸门、单次预算和账本——所以按量计费天然成立；
- 每次调用都进审计（哪个令牌调了什么），审计里**没有**令牌明文；
- 后端由服务端决定，调用方不能在请求里挑 `mock` 去拿假答案；
- 不想对外开放就设 `AGENT_API_ENABLED=false`，`/v1/*` 一律 404。

完整的字段表、错误码、Python / GitHub Actions 示例、以及"现在还没有的"（流式、
细分权限）都写在 **[`docs/api.md`](docs/api.md)** 里。

长任务和重试都有对应手段：

- **异步**：`{"async": true}` 立刻拿 `run_id`，之后 `GET /v1/runs/<id>` 查结果
  （跑完的结果**存在数据库里**，服务重启也取得到；卡死的老任务会被标成失败）；
- **幂等**：带上 `Idempotency-Key`，同一个键重发只会跑一次、只扣一次费
  （还在跑就返回同一个 `run_id` 让你轮询）；
- **回调**：异步任务多给一个 `callback_url`，跑完主动 POST 结果过去，不用轮询。
  签名防伪造（HMAC-SHA256）、失败按退避重试且跨重启接着发、地址逐条查内网防 SSRF；
  投递状态在 `GET /v1/runs/<id>` 的 `callback` 块里，收不到时看
  `agentcode api callbacks <账号>`（细节见 [`docs/api.md`](docs/api.md) 第 5 节）；
- 运维侧查"用户说任务一直没结果"：`agentcode api list <账号>`（那一行会标出回调状态）。

## 备份与恢复

用户的数据全在一台机器的一块盘上：账号、账本、订单、会话、代码工作区。
没有第二份的那天，就是硬盘坏掉的那天——所以这件事做成了命令，
而不是"记得自己 tar 一下"：

```powershell
D:\Anaconda\python.exe -m agentcode backup create                        # 备一份
D:\Anaconda\python.exe -m agentcode backup list                          # 手上有哪些包
D:\Anaconda\python.exe -m agentcode backup verify backups\xxx.tar.gz     # 恢复演练
D:\Anaconda\python.exe -m agentcode backup restore backups\xxx.tar.gz --to D:\恢复出来
```

四件事值得说明：

- **备份不是拷文件**。SQLite 正在写入时，直接 `tar` 出来的库可能是"事务做了一半"的，
  平时看着没事、真要恢复才发现坏了。这里走 SQLite 的在线备份 API 拿一致性快照。
- **备份必须演练**。`verify` 会把包解开、真的把库打开读一遍（完整性检查 + 账号 +
  schema 版本），再拿清单和实际内容对账。没打开过的备份等于没有备份。
- **恢复是往新目录里放**，不是原地覆盖。目标非空默认拒绝；`--force` 才继续，
  而且旧数据会**改名留一份**，不删除——"以为恢复了，其实盖错了"是这里唯一不能犯的错。
- `verify` / `restore` / `list` **不读 .env**：配置坏掉、库打不开的那天，
  正是你最需要它们的时候。

让它自动备：

```dotenv
AGENT_BACKUP_DIR=backups          # 留空（默认）= 不自动备份
AGENT_BACKUP_INTERVAL_HOURS=24
AGENT_BACKUP_KEEP=7               # 只留最近 7 份，且只删自己生成的那种文件名
```

配了之后，服务启动时在后台查一次、之后每小时查一次，**距上一份超过间隔才备**。
读不出来的坏包不算"已经备过"，不会挡住新的一份；清理老备份时也只认
`agentcode-*.tar.gz` 这种自己生成的名字，你手工放进去的东西一律不碰。

**演练多久做一次**：每次发版前一次，之后每月一次——真的恢复到临时目录，
再用 `agentcode user list` 看一眼账号读不读得出来。备份的失败方式是静默的：
它平时完全不报错，只在你要用它的时候才暴露。

## 测试

```powershell
D:\Anaconda\python.exe -m pytest -q
```

全部用例都是**离线**的：不打真实模型、不联网搜索，所以随便跑。

推上去之后 GitHub Actions 会自动跑（见 `.github/workflows/ci.yml`）：

| 任务 | 内容 |
| --- | --- |
| 测试 | `ubuntu-latest` × Python 3.10/3.13，外加 `windows-latest` × Python 3.13 |
| 沙箱镜像 | 真的构建 `docker/sandbox/Dockerfile`，并断言容器里非 root、断网、只读根、pytest 可用 |

之所以要跑 3.10：`pyproject.toml` 里声明了 `requires-python = ">=3.10"`，
声明了就得有人真的替你测，不然迟早变成一句谎话。

824 项用例全部离线运行，不产生任何网络请求，也不消耗 API 额度——连 SMTP 都是用
本地 stub 服务真的走一遍协议，不往任何真实邮箱发信。覆盖重点：

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
- 备份与恢复：包里四样东西齐全、清单计数与实际内容对账、非 tar.gz / 缺清单 / 库损坏
  分别报错、恢复到新目录后**库真的能打开读账号**、目标非空拒绝、force 时旧数据改名留底；
  自动备份只在配了目录时生效、坏包不算"已备过"、只清自己生成的老包（别人的文件一律不碰）。
- 回收站与审计：删掉的会话连对话内容一起恢复、清空的代码目录连文件一起恢复、
  条目名不能顺着路径爬出去、恢复时不覆盖新写的东西、注销时回收站一起真删；
  审计记下登录/删除/导出/开户等动作，**密码永远不进审计表**，写审计失败也不影响操作。
- 通知：邮箱格式与读写、五类事件各自的触发与去重、没配渠道时记台账而不是假装发出、
  渠道失败只记结果不抛异常、SMTP 真投递（本地 stub 验中文主题编码与收件人）。
- 对外 API：令牌只存哈希、明文只在创建时出现、吊销/过期/停用立刻失效、
  一个账号的令牌碰不到别人的数据；`POST /v1/run` 走真实运行链路并记账、
  额度用尽 402、限流 429、服务端决定后端（调用方挑不了 mock）、每次调用进审计。

## 设计文档与实现计划

- 框架设计：`docs/superpowers/specs/2026-09-22-agentcode-framework-design.md`
- 实现计划：`docs/superpowers/plans/2026-09-22-agentcode-framework.md`
- Coding 智能体设计：`docs/superpowers/specs/2026-09-22-coding-agent-design.md`
