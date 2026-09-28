# Code Agent 设计文档（Homework 1）

> 课程：软件工程　作业：Homework 1 · Code Agent
> 项目：AgentCode　—— 可扩展的 Python 智能体框架 + 测试生成智能体
> 提交物：源码仓库（本目录）、本文档、README.md，以及可选的演示视频

## 1. 方向选择：测试生成 Agent

作业给出五个可选方向，本项目选 **测试生成 Agent**，理由是它与本项目已有的能力最契合：

| 方向 | 核心技术要点 | 本项目匹配情况 |
| --- | --- | --- |
| **测试生成（选用）** | **测试框架集成、断言生成** | 已具备真实的 pytest 执行闭环与失败自愈，正是"测试框架集成" |
| 代码生成 | Prompt Engineering、Few-shot | 已有 `coding` 智能体，但主要考提示词，无法体现工具与验证能力 |
| 代码审查 | 代码解析 | 需要另建静态分析层，工作量与风险都更高 |
| 代码解释 | 代码理解、自然语言生成 | 只需读取文件，现有工具与验证机制几乎用不上 |
| 重构建议 | 代码分析、设计模式识别 | 同样需要新造分析层，且结论难以客观验证 |

选测试生成还有一个决定性优势：**它的结果可以被客观验证**。生成的测试能跑就是能跑，
跑不通就必须改到跑通，不存在"看起来对"的空间——这让功能完整性（占评分 40%）有据可依。

## 2. 需求对照

### 2.1 实现要求

| 作业要求 | 本项目实现 |
| --- | --- |
| 基本的 Agent 循环：输入 → 推理 → 工具调用 → 输出 | `agentcode/core/agent.py` 定义 `BaseAgent`；`agents/react.py` 实现 Thought → Action → Observation 循环 |
| 支持至少一种工具 | 共 8 个工具：`read_file`、`write_file`、`run_python`、`list_files`、`web_search`、`calculator`、`current_time`，外加离线演示工具 |
| 可通过命令行或简单 Web 界面交互 | 命令行 `agentcode run ...`，网页 `agentcode web`（含登录、会话管理） |
| 支持上下文记忆 | 短期记忆按轮保留（默认 5 轮，可配），网页端每用户独立会话并可切换 |
| 错误处理与重试机制 | 中间件链：日志、指数退避重试、超时、用量统计；工具异常统一转中文提示；模型输出解析带容错 |
| 技术栈自由选择 | Python 3.13 + OpenAI 兼容接口（DeepSeek），未绑定任何 Agent 框架 |

### 2.2 评审标准对照

| 维度（权重） | 本项目对应证据 |
| --- | --- |
| 功能完整性 40% | 五种方向中选定的测试生成闭环**真的可跑**；边界情况见 §6；264+ 项离线测试 |
| Agent 架构 30% | 分层架构（core / llm / tools / memory / middleware / agents / web），智能体注册表，中间件可插拔，新增智能体只需一个文件 |
| 代码质量 20% | 全量类型注解与中文注释、模块职责单一、`pytest` 264+ 项全绿、无重复逻辑（工具与网页共用同一份路径校验） |
| 文档 10% | 本 `Design.md` + `README.md`（快速开始、架构、扩展指南、配置说明） |

## 3. 架构

```
┌─────────────────────────── 入口层 ───────────────────────────┐
│ 命令行 agentcode.cli        网页 agentcode.web（登录 + SSE）  │
└───────────────┬──────────────────────────────┬───────────────┘
                │                              │
        ┌───────▼────────┐            ┌────────▼─────────┐
        │  Agent 注册表   │            │  会话仓库（按用户）│
        └───────┬────────┘            └────────┬─────────┘
                │                              │
        ┌───────▼──────────────────────────────▼─────────┐
        │              BaseAgent（core/agent.py）         │
        │  运行上下文 · 轨迹记录 · 上下文记忆 · 结果构造    │
        └───┬────────────┬───────────────┬───────────────┘
            │            │               │
      ┌─────▼────┐ ┌─────▼─────┐ ┌───────▼───────┐
      │ LLM 后端  │ │ 工具注册表 │ │  中间件链      │
      │ llm/*    │ │ tools/*   │ │ logging/retry │
      │          │ │           │ │ timeout/usage │
      └──────────┘ └───────────┘ └───────────────┘
```

已实现的智能体：`test_gen`（本次作业方向）、`coding`、`react`、`plan_and_solve`、`reflection`、`echo`。

## 4. 测试生成智能体的一次运行

以 `examples/sample_code/calculator.py` 为例：

```
1. read_file[calculator.py]                     读清有哪些函数、抛什么异常
2. 分析：add/subtract/divide/average/safe_sqrt  识别正常路径与边界（除零、空列表、负数）
3. write_file[test_calculator.py, ...]          生成 8 个 pytest 用例
4. run_python[pytest -q test_calculator.py]     真的执行
5. 退出码非 0 → 读报错 → 改测试 → 再跑 …       失败自愈（最多 default_max_steps 步）
6. Finish[为哪个文件生成了测试、覆盖了哪些场景、测试文件叫什么]
```

纪律（写在提示词里，并有测试守着）：

- **只写测试，不修改被测源文件**——即使发现源码有 bug，也只在结论里指出；
- 只跑自己写的那个测试文件，避免被工作目录里的无关文件干扰；
- 必须覆盖边界：空输入、零、负数、极值、非法类型、显式抛出的异常（用 `pytest.raises`）；
- 最终答案不粘贴命令、退出码或 stdout。

## 5. 关键设计决策

**工具执行放在独立工作目录。** `AGENT_CODE_ROOT`（默认 `traces/sandbox`）是唯一可读写的位置，
路径越界一律拒绝。这既防止智能体误伤项目源码，也让"生成测试"这件事可复现。

**验证优先于自评。** `reflection` 智能体靠模型自评，容易自信地错；`test_gen` 的结论来自真实运行结果。

**解析层容错。** 模型偶尔会一次输出多组 Thought/Action、把提示词条目当动作前缀、或在内容里用未转义的引号。
解析器逐项容错，避免一次解析失败白跑好几步（实测曾让同一任务从 4 步涨到 12 步）。

**没有代码工具时立刻报错。** 网页端默认禁止执行代码（`AGENT_ALLOW_CODE_TOOLS=false`），
此时 `test_gen` 会直接说明"需要代码工具以及如何开启"，而不是空转十几步。

## 6. 边界情况处理

| 边界情况 | 处理方式 |
| --- | --- |
| 源码有 bug | 不修改源码，在测试里用 `pytest.raises` 或补注释说明，并在结论中指出 |
| 工作目录里有无关文件 | 只对目标测试文件执行 pytest |
| 除零、空列表、负数等边界 | 提示词要求必须覆盖，示例演示里已体现 |
| 工具执行超时 / 死循环 | `run_python` 默认 10 秒超时并杀进程 |
| 模型输出格式走样 | 解析容错 + 缺失 Action 时给出纠正提示继续循环 |
| 达到最大步数 | 如实返回失败与原因，不谎报成功 |
| 缺少代码工具 | 立刻返回中文说明，提示开启方式 |
| 网页端多用户 | 账号隔离：会话按用户分目录，配额按账号计算，超额返回 402 |

## 7. 使用方法

```powershell
# 1. 安装依赖
D:\Anaconda\python.exe -m pip install -r requirements.txt

# 2. 配置 .env（复制 .env.example）：LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_ID

# 3. 核心演示：为指定源码生成测试（真实模型）
D:\Anaconda\python.exe -m agentcode run --agent test_gen --file examples\sample_code\calculator.py

# 4. 离线演示（不消耗额度，用内置脚本模型）
D:\Anaconda\python.exe -m agentcode run --agent test_gen --llm mock --file examples\sample_code\calculator.py

# 5. 网页界面
D:\Anaconda\python.exe -m agentcode web --open      # 首次启动会打印管理员密码

# 6. 运行测试
D:\Anaconda\python.exe -m pytest -q
```

演示视频（1 分钟内）建议顺序：`agentcode list`（展示六个智能体）→ 第 4 步离线演示（展示
读文件 → 写测试 → 跑测试 → 收尾的完整轨迹）→ 打开生成的 `test_calculator.py` 并单独跑一次 pytest →
第 3 步真实模型演示（展示真实理解与边界覆盖）。

## 8. 验证情况

- `pytest` 全绿（含测试生成智能体的 7 项专属用例：注册、缺工具报错、提示词纪律、
  **端到端生成并用 pytest 独立复跑确认通过**、源码未被修改、CLI `--file` 行为）。
- 演示路径实测：`--file examples/sample_code/calculator.py` 生成 8 个用例，退出码 0；
  独立复跑 `pytest -q test_calculator.py` 同样 8 项全过。

## 9. 已知限制

- 网页端默认禁止执行代码（安全默认），要看网页版测试生成需设 `AGENT_ALLOW_CODE_TOOLS=true`；
- 代码执行尚未容器化，约束是"限制目录 + 超时 + 剥离密钥环境变量"，不是安全沙箱；
- 会话与账号数据保存在本地 SQLite/JSON 文件，多实例部署需要换成 Redis/Postgres。
