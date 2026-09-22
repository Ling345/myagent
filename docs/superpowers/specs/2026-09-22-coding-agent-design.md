# Coding 智能体与受限代码执行 设计文档

> 状态：已确认（方案 C）
> 日期：2026-09-22
> 位置：`D:\agent\agentcode-homework1`

## 1. 问题

现有框架能「写」代码，却不能「验」代码：

- 工具只有网页搜索、计算器、当前时间，没有任何代码执行能力；
- `agentcode/agents/reflection.py` 的评审意见完全来自模型自评，从未运行过代码，
  因此会自信地给出「已是常规最优解」这类结论，而实现可能有越界或逻辑错误。

结论：瓶颈不在模型，而在缺少「写 → 跑 → 读报错 → 改」的客观闭环。

## 2. 范围

本次交付三件事：

1. `agentcode/tools/code.py`：受限代码执行与文件读写工具；
2. `agentcode/agents/coding.py`：以「跑通测试」为终止条件的 coding 智能体；
3. 配置项与接线：代码工作目录、超时、输出上限、coding 步数。

不在范围内：容器级隔离、多文件项目管理、依赖自动安装、长期代码记忆。

## 3. 受限执行（不是真沙箱，边界说清楚）

`run_python` 用 `subprocess` 启动独立解释器进程，约束如下：

| 约束 | 做法 |
| --- | --- |
| 工作目录 | 固定在 `AGENT_CODE_ROOT`（默认 `traces/sandbox`），默认不碰项目源码 |
| 超时 | `AGENT_CODE_TIMEOUT`（默认 10 秒），超时即杀进程并返回中文提示 |
| 环境变量 | 只传最小集（PATH/SYSTEMROOT/TEMP/PYTHONIOENCODING/PYTHONPATH），**不传任何密钥** |
| 输出长度 | 截断到 `AGENT_CODE_OUTPUT_LIMIT`（默认 4000 字符），避免刷爆上下文 |
| 输入 | `stdin` 接 `DEVNULL`，避免子进程等待输入卡住 |

**必须明确的边界**：这**不是**安全沙箱。Python 子进程本身可以访问文件系统与网络，
本设计的目的是"防止误伤与跑飞"（改错目录、死循环、刷屏、密钥泄漏到子进程），
而不是防御恶意代码。真要跑不可信代码，需要容器或独立账号，那是另一个量级的工程。

## 4. 工具

| 工具 | 说明 |
| --- | --- |
| `run_python(code, timeout=None)` | 在代码根目录执行 `python -c code`，返回退出码、stdout、stderr |
| `read_file(path)` | 读取根目录内文件，超长截断 |
| `write_file(path, content)` | 写入根目录内文件（自动建目录），单文件大小受限 |
| `list_files(path=".")` | 列出根目录内文件，便于失败后重新定位 |

路径安全：所有路径先 `resolve()`，必须落在代码根目录之内，否则返回中文错误；
禁止 `..` 逃逸与根目录外的绝对路径。所有工具异常都转成中文错误字符串回到循环里，
不会中断智能体运行。

## 5. Coding 智能体

`CodingAgent` 复用 `ReActAgent` 的循环与解析，只替换提示词与默认步数：

1. 先写测试（`write_file`），再写实现（`write_file`）——不接受"只在回答里贴代码"；
2. 用 `run_python` 跑测试；
3. 失败就读 stderr 改代码再跑，直到全绿；
4. 全绿之后才 `Finish`，并报告通过的测试数与关键改动；
5. 同一错误连续两次无进展就如实报告卡点，不许谎报成功。

步数：写代码比闲聊长，coding 智能体默认 **20 步**（`AGENT_CODING_STEPS`），
其它智能体仍用 `AGENT_MAX_STEPS`（默认 6）。用户显式传 `--max-steps` 时对两者都生效。

## 6. 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AGENT_CODE_ROOT` | `traces/sandbox` | 代码工作目录 |
| `AGENT_CODE_TIMEOUT` | `10` | 单次执行超时（秒），非法值回退默认 |
| `AGENT_CODE_OUTPUT_LIMIT` | `4000` | 输出截断长度（字符） |
| `AGENT_CODING_STEPS` | `20` | coding 智能体默认最大步数 |

`Settings.max_steps_for(agent_name)` 负责按智能体选择步数，命令行与网页共用它。

## 7. 验收标准

单元测试（离线，不消耗额度）：

1. `run_python` 能执行代码并回传 stdout；语法错误能把 traceback 带回；
2. 死循环被超时终止，且不拖住调用方；
3. 子进程**看不到** `LLM_API_KEY` 等密钥（环境变量隔离）；
4. 子进程的工作目录就是代码根目录；
5. 超长输出被截断并标注；
6. 读写文件正常往返；`../` 逃逸、根目录外绝对路径、超大写入都被拒绝；
7. scripted 模型驱动完整闭环：初版故意写错 → 测试失败 → 读 stderr 改对 → 全绿收尾；
8. `Settings.max_steps_for("coding")` 与 CodingAgent 默认步数一致。

端到端验证（消耗少量真实额度）：用真实 DeepSeek 跑一个编码任务，导出完整轨迹，
确认它真的经历了"失败 → 修改 → 通过"的过程，而不是一次生成就对。
