# AgentCode 可扩展智能体框架 设计文档

> 状态：已确认（方案 B）
> 日期：2026-09-22
> 位置：`D:\agent\agentcode-homework1`

## 1. 目标

构建一个可扩展的 Python 智能体框架，而不是若干个一次性示例脚本。
核心验收标准是"加一个新智能体只需新增一个文件并在注册表登记"。

框架需要同时满足三类使用方式：

1. 作为课程作业：演示 ReAct、Plan-and-Solve、Reflection 三种经典范式。
2. 作为可复用框架：工具、LLM 后端、记忆、中间件均可替换与扩展。
3. 作为可验证工程：全部核心行为有离线单元测试，测试不消耗 API 额度。

## 2. 技术栈与全局约束

- Python 3.13（解释器 `D:\Anaconda\python.exe`）。
- 依赖：`openai`、`python-dotenv`、`requests`、`serpapi`、`rich`、`pytest`。
- 密钥只从环境变量 / `.env` 读取，源码中不得出现任何密钥字面量。
- 所有注释、提示词、终端输出、异常信息使用中文。
- 全部公开函数带类型注解，模块职责单一，单文件保持可一次读完的规模。
- 网络相关工具在无网络或缺少密钥时必须返回中文错误字符串，不得抛异常中断流程。

## 3. 架构

框架采用"核心 + 可插拔实现"的分层结构，依赖方向单向向下：

```
CLI (agentcode.cli)
      │
      ▼
Agent 实现 (agentcode.agents.*)  ── 使用 ──▶  AgentRegistry
      │
      ├──▶ BaseAgent (agentcode.core.agent)  ── 持有 ──▶ Middleware 链
      │                                                │
      ├──▶ BaseLLM (agentcode.llm.*)  ◀── 包装 ───────┘
      ├──▶ ToolRegistry (agentcode.tools.*)
      └──▶ Memory (agentcode.memory.*)

配置：agentcode.config.Settings  ← .env / JSON 配置文件
结果：AgentResult / Step（可序列化为轨迹 JSON）
```

### 3.1 中间件采用"包装器"而不是"观察者"

中间件的职责是包装一次调用，因此接口定义为：

```python
class Middleware:
    def wrap_llm(self, call, ctx): return call
    def wrap_tool(self, call, ctx): return call
```

- 日志中间件返回原调用，只在调用前后打印与记录。
- 重试中间件返回一个带指数退避的新调用。
- 超时中间件返回一个在独立线程中限时执行的新调用。

这样日志类中间件与行为类中间件共用同一个接口，无需两套扩展点。

### 3.2 依赖注入

`BaseAgent` 通过构造函数接收 `llm`、`tools`、`middlewares`、`memory`、`max_steps`。
测试时注入 `ScriptedLLM`（按脚本返回固定文本）即可完整跑通循环，不触碰网络。

## 4. 组件与接口

### 4.1 core

| 文件 | 职责 | 关键接口 |
|---|---|---|
| `core/errors.py` | 异常体系 | `AgentCodeError`、`LLMError`、`ToolError`、`ConfigError`、`AgentNotFoundError`、`ParseError` |
| `core/result.py` | 运行轨迹数据结构 | `Step`、`TokenUsage`、`AgentResult.to_dict()` |
| `core/context.py` | 单次运行上下文 | `RunContext`（`run_id`、`steps`、`usage`、`started_at`） |
| `core/parsing.py` | 输出解析 | `parse_react_output`、`parse_action`、`parse_finish`、`parse_plan` |
| `core/agent.py` | 智能体基类 | `BaseAgent.run(task) -> AgentResult`，内部 `_think`/`_call_tool` |
| `core/registry.py` | 智能体注册表 | `AgentRegistry.register/create/names/describe`，全局 `default_registry` 与 `@register_agent` |

### 4.2 llm

| 文件 | 职责 | 关键接口 |
|---|---|---|
| `llm/base.py` | 抽象后端 | `BaseLLM.think(messages, temperature=None) -> str`，`name`、`last_usage` |
| `llm/openai_compatible.py` | OpenAI 兼容实现 | 流式输出、超时、异常转 `LLMError`、记录 token 用量 |
| `llm/mock.py` | 离线脚本化实现 | `ScriptedLLM(responses)`，用尽后重复最后一条 |

### 4.3 tools

| 文件 | 职责 | 关键接口 |
|---|---|---|
| `tools/base.py` | 工具规格与注册表 | `ToolSpec`、`ToolRegistry.register_tool/tool/get/describe/invoke` |
| `tools/builtin.py` | 内置工具 | `web_search`、`calculator`、`current_time`、`register_builtin_tools` |

`ToolRegistry.invoke(name, raw_input)` 统一处理两种调用形态：
字符串输入按位置参数传入，`key=value` 形式解析为关键字参数；
工具内部异常被捕获并转换为中文错误字符串，保证智能体循环不中断。

### 4.4 memory

| 文件 | 职责 | 关键接口 |
|---|---|---|
| `memory/short_term.py` | 最近若干轮对话 | `ShortTermMemory.add/get_messages/clear` |
| `memory/json_store.py` | 轨迹落盘 | `JsonStore.save(payload, path)`、`load(path)` |

### 4.5 middleware

| 文件 | 职责 | 关键接口 |
|---|---|---|
| `middleware/base.py` | 中间件基类 | `Middleware.wrap_llm/wrap_tool` |
| `middleware/logging.py` | 步骤日志 | `LoggingMiddleware(stream)` |
| `middleware/retry.py` | 指数退避重试 | `RetryMiddleware(max_retries, base_delay)` |
| `middleware/timeout.py` | 单次调用限时 | `TimeoutMiddleware(timeout)` |
| `middleware/token_usage.py` | token 与调用次数统计 | `TokenUsageMiddleware`，暴露 `usage` |

### 4.6 agents

| 文件 | 职责 | 关键接口 |
|---|---|---|
| `agents/react.py` | Thought/Action/Observation 循环 | `ReActAgent` |
| `agents/plan_and_solve.py` | 先规划再逐步执行 | `Planner`、`Executor`、`PlanAndSolveAgent` |
| `agents/reflection.py` | 生成—评审—优化迭代 | `ReflectionAgent` |

三者都在 `agents/__init__.py` 中通过 `@register_agent` 登记，
CLI 与注册表据此发现可用智能体。

### 4.7 config 与 CLI

`config.Settings.from_env()` 解析顺序：显式传入的 `.env` → 当前目录向上最多三层查找 `.env` → 进程环境变量。
缺少 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL_ID` 时抛 `ConfigError` 并给出中文提示。
`Settings.masked()` 用于安全打印配置。

CLI 子命令：

```
agentcode run    --agent react --task "..." [--llm mock|openai] [--max-steps 6] [--trace out.json] [--json] [--config configs/example.json]
agentcode list   [--config ...]
agentcode config [--config ...]
```

`--llm mock` 使用内置脚本化模型，可在无网络环境下演示完整流程。

## 5. 数据流（以 ReAct 为例）

1. CLI 读取配置，构建 `ToolRegistry` 与 `BaseLLM`，选择智能体。
2. `BaseAgent.run` 创建 `RunContext`，把中间件链应用到 LLM 与工具调用上。
3. 每轮：渲染提示词 → 调用 LLM（经中间件）→ 解析 Thought/Action。
4. 若为 `Finish[...]`，写入 `Step` 并返回 `AgentResult`；否则执行工具，把 Observation 追加进历史。
5. 达到 `max_steps` 仍未结束时，返回 `success=False` 与中文说明。
6. 结果统一由 CLI 渲染，`--trace` 指定路径时写入轨迹 JSON。

## 6. 错误处理

- 配置缺失 → `ConfigError`，CLI 打印中文提示并以退出码 2 结束。
- LLM 调用失败 → `LLMError`；有重试中间件时先重试，最终失败写入 `AgentResult.error`。
- 工具不存在或执行异常 → 返回中文错误字符串作为 Observation，循环继续。
- 模型输出无法解析 → 记录 `ParseError` 信息，追加纠正提示后进入下一轮。
- 智能体名不存在 → `AgentNotFoundError`，CLI 列出全部可用名称。

## 7. 测试策略

全部测试离线可跑，`pytest` 不产生任何网络请求：

| 测试文件 | 覆盖点 |
|---|---|
| `tests/test_tools.py` | 注册、描述、字符串/关键字调用、未知工具、计算器、异常兜底 |
| `tests/test_parsing.py` | ReAct 输出解析、两种 Action 形态、Finish 提取、计划解析与兜底 |
| `tests/test_llm.py` | 配置校验、ScriptedLLM 行为、用量记录、缺密钥报错 |
| `tests/test_middleware.py` | 重试成功与耗尽、超时、日志记录、用量统计 |
| `tests/test_memory.py` | 短期记忆轮数上限、JSON 落盘与读取 |
| `tests/test_registry.py` | 注册、重复注册、创建、未知名称报错 |
| `tests/test_react.py` | 完整循环、工具调用、Finish 结束、步数上限 |
| `tests/test_plan_and_solve.py` | 规划解析、逐步执行、历史累积 |
| `tests/test_reflection.py` | 达到"无需改进"提前结束、否则迭代优化 |
| `tests/test_cli.py` | `list` / `run --llm mock` / `--trace` 落盘 / 配置缺失退出码 |
| `tests/test_extension.py` | 新增自定义智能体后通过注册表与 CLI 使用 |

## 8. 非目标（YAGNI）

- 不做多进程 / 分布式调度。
- 不做 Web 界面（后续可以基于现有 `BaseAgent` 单独加）。
- 不实现向量数据库与长期记忆检索。
- 不绑定任何特定厂商的 function calling 协议。
