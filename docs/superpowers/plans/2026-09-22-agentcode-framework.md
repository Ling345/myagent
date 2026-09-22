# AgentCode 可扩展智能体框架 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `D:\agent\agentcode-homework1` 交付一个可扩展的 Python 智能体框架，含三种经典智能体、可插拔工具/LLM/中间件与离线测试。

**Architecture:** 核心层（`core/`）定义智能体基类、注册表、解析与结果数据结构；实现层（`llm/`、`tools/`、`memory/`、`middleware/`）以依赖注入方式接入；`agents/` 提供 ReAct、Plan-and-Solve、Reflection 三个实现并通过注册表被发现；`cli.py` 统一入口。

**Tech Stack:** Python 3.13、openai、python-dotenv、requests、serpapi、rich、pytest。

**Spec:** `docs/superpowers/specs/2026-09-22-agentcode-framework-design.md`

## Global Constraints

- 解释器固定为 `D:\Anaconda\python.exe`，测试命令为 `D:\Anaconda\python.exe -m pytest -q`。
- 工作目录固定为仓库根目录 `D:\agent\agentcode-homework1`。
- 密钥仅从环境变量 / `.env` 读取，源码中不得出现密钥字面量。
- 注释、提示词、终端输出、异常信息一律使用中文。
- 所有公开函数带类型注解。
- 无网络环境下测试必须全部通过。

---

### Task 1: 项目骨架与配置模块

**Files:**
- Create: `.gitignore`、`requirements.txt`、`pyproject.toml`、`.env.example`
- Create: `agentcode/__init__.py`、`agentcode/core/__init__.py`、`agentcode/core/errors.py`、`agentcode/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `ConfigError`；`Settings.from_env(env_file=None, search_parents=False) -> Settings`；`Settings.validate() -> Settings`；`Settings.masked() -> dict[str, str]`；字段 `model`、`api_key`、`base_url`、`timeout`、`serpapi_key`、`max_steps`、`temperature`、`trace_dir`。

- [ ] **Step 1: 写失败测试**

```python
def test_missing_llm_key_raises_config_error(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    settings = Settings.from_env(env_file=str(tmp_path / "missing.env"), search_parents=False)
    with pytest.raises(ConfigError):
        settings.validate()
```

- [ ] **Step 2: 运行测试确认失败** — 预期 `ModuleNotFoundError: No module named 'agentcode'`。
- [ ] **Step 3: 实现骨架与 `Settings`**，`masked()` 只保留密钥首尾 4 位。
- [ ] **Step 4: 运行测试确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat: 项目骨架与配置模块"`。

### Task 2: 工具系统

**Files:**
- Create: `agentcode/tools/__init__.py`、`agentcode/tools/base.py`、`agentcode/tools/builtin.py`
- Test: `tests/test_tools.py`

**Interfaces:**
- Produces: `ToolSpec(name, description, parameters, func)`；`ToolRegistry.register_tool(name, description, func, parameters=None) -> ToolSpec`；`ToolRegistry.tool(name=None, description="", parameters=None)` 装饰器；`get(name)`；`describe() -> str`；`invoke(name, raw_input) -> str`；`register_builtin_tools(registry) -> ToolRegistry`。

- [ ] **Step 1: 写失败测试**

```python
def test_invoke_unknown_tool_returns_chinese_error():
    tools = ToolRegistry()
    assert "未注册" in tools.invoke("nope", "x")


def test_invoke_passes_keyword_arguments():
    tools = ToolRegistry()
    tools.register_tool("add", "求和", lambda a, b: str(int(a) + int(b)))
    assert tools.invoke("add", "a=2, b=3") == "5"


def test_tool_exception_becomes_error_string():
    tools = ToolRegistry()
    tools.register_tool("boom", "抛错", lambda: 1 / 0)
    assert "执行失败" in tools.invoke("boom", "")
```

- [ ] **Step 2: 运行测试确认失败** — 预期 `ModuleNotFoundError: No module named 'agentcode.tools'`。
- [ ] **Step 3: 实现 `ToolRegistry` 与内置工具**：`calculator` 用 `ast` 白名单求值，禁用 `eval`；`web_search` 走 SerpApi，缺密钥返回中文错误；`current_time` 返回 ISO 时间。
- [ ] **Step 4: 运行测试确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat: 工具注册表与内置工具"`。

### Task 3: LLM 后端

**Files:**
- Create: `agentcode/llm/__init__.py`、`agentcode/llm/base.py`、`agentcode/llm/mock.py`、`agentcode/llm/openai_compatible.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `Settings`、`LLMError`、`ConfigError`。
- Produces: `BaseLLM.think(messages, temperature=None) -> str`，属性 `name`、`last_usage`；`ScriptedLLM(responses, fallback=None)`；`OpenAICompatibleLLM(model, api_key, base_url, timeout=60, temperature=0.0, stream=True)`。

- [ ] **Step 1: 写失败测试**

```python
def test_scripted_llm_returns_responses_in_order():
    llm = ScriptedLLM(["第一步", "第二步"])
    assert llm.think([{"role": "user", "content": "x"}]) == "第一步"
    assert llm.think([{"role": "user", "content": "x"}]) == "第二步"
    assert llm.think([{"role": "user", "content": "x"}]) == "第二步"


def test_openai_llm_without_key_raises_config_error():
    with pytest.raises(ConfigError):
        OpenAICompatibleLLM(model="m", api_key="", base_url="https://example.com")
```

- [ ] **Step 2: 运行测试确认失败**。
- [ ] **Step 3: 实现三个后端**：OpenAI 兼容后端支持流式与非流式，异常统一转 `LLMError`，并把 `response.usage` 记入 `last_usage`。
- [ ] **Step 4: 运行测试确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat: LLM 后端与离线脚本模型"`。

### Task 4: 结果、上下文与解析

**Files:**
- Create: `agentcode/core/result.py`、`agentcode/core/context.py`、`agentcode/core/parsing.py`
- Test: `tests/test_parsing.py`

**Interfaces:**
- Produces: `TokenUsage(prompt_tokens, completion_tokens, calls)`；`Step`；`AgentResult.to_dict()`；`RunContext`；`parse_react_output(text) -> tuple[str | None, str | None]`；`parse_action(action) -> tuple[str | None, str | None]`；`parse_finish(action) -> str | None`；`parse_plan(text) -> list[str]`。

- [ ] **Step 1: 写失败测试**

```python
def test_parse_react_output_extracts_thought_and_action():
    text = "Thought: 需要查天气\nAction: Search[北京天气]"
    assert parse_react_output(text) == ("需要查天气", "Search[北京天气]")


def test_parse_action_supports_parentheses_form():
    assert parse_action('Search(query="北京天气")') == ("Search", 'query="北京天气"')


def test_parse_plan_extracts_python_list_from_fence():
    assert parse_plan("说明\n```python\n[\"第一步\", \"第二步\"]\n```") == ["第一步", "第二步"]
```

- [ ] **Step 2: 运行测试确认失败**。
- [ ] **Step 3: 实现数据结构与解析函数**：`parse_finish` 兼容 `Finish[...]` 与 `finish(answer="...")`；`parse_plan` 在代码围栏缺失时按行兜底并剥离序号前缀。
- [ ] **Step 4: 运行测试确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat: 运行结果结构与输出解析"`。

### Task 5: 记忆与中间件

**Files:**
- Create: `agentcode/memory/__init__.py`、`agentcode/memory/short_term.py`、`agentcode/memory/json_store.py`
- Create: `agentcode/middleware/__init__.py`、`agentcode/middleware/base.py`、`agentcode/middleware/logging.py`、`agentcode/middleware/retry.py`、`agentcode/middleware/timeout.py`、`agentcode/middleware/token_usage.py`
- Test: `tests/test_memory.py`、`tests/test_middleware.py`

**Interfaces:**
- Produces: `ShortTermMemory(max_turns=10)`；`JsonStore.save(payload, path)` / `load(path)`；`Middleware.wrap_llm(call, ctx)` / `wrap_tool(call, ctx)`；`RetryMiddleware(max_retries=2, base_delay=0.0)`；`TimeoutMiddleware(timeout=30)`；`LoggingMiddleware(stream=None)`；`TokenUsageMiddleware.usage`。

- [ ] **Step 1: 写失败测试**

```python
def test_retry_middleware_retries_until_success():
    attempts = {"n": 0}

    def call(messages):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise LLMError("暂时失败")
        return "成功"

    wrapped = RetryMiddleware(max_retries=3, base_delay=0.0).wrap_llm(call, RunContext())
    assert wrapped([]) == "成功"
    assert attempts["n"] == 3


def test_timeout_middleware_raises_llm_error():
    wrapped = TimeoutMiddleware(timeout=0.05).wrap_llm(lambda messages: time.sleep(0.5), RunContext())
    with pytest.raises(LLMError):
        wrapped([])
```

- [ ] **Step 2: 运行测试确认失败**。
- [ ] **Step 3: 实现记忆与中间件**：超时用 `ThreadPoolExecutor` 实现，保证 Windows 可用。
- [ ] **Step 4: 运行测试确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat: 记忆模块与中间件链"`。

### Task 6: 智能体基类与注册表

**Files:**
- Create: `agentcode/core/agent.py`、`agentcode/core/registry.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: `RunContext`、`AgentResult`、`ToolRegistry`、`BaseLLM`、`Middleware`。
- Produces: `BaseAgent`，构造函数参数 `name`、`description`、`llm`、`tools`、`middlewares`、`memory`、`max_steps`；`run(task) -> AgentResult`（抽象）；`_think(messages, ctx) -> str`；`_call_tool(name, raw_input, ctx) -> str`；`AgentRegistry.register(name, factory, description)`、`create(name, **kwargs)`、`names()`、`describe()`、`get(name)`；全局 `default_registry` 与 `register_agent(name, description)`。

- [ ] **Step 1: 写失败测试**

```python
def test_registry_create_unknown_agent_raises():
    registry = AgentRegistry()
    with pytest.raises(AgentNotFoundError):
        registry.create("不存在", llm=ScriptedLLM([]), tools=ToolRegistry())


def test_base_agent_wraps_llm_with_middleware():
    class EchoAgent(BaseAgent):
        def run(self, task, context=None):
            ctx = context or RunContext()
            answer = self._think([{"role": "user", "content": task}], ctx)
            return AgentResult(agent=self.name, task=task, answer=answer, success=True)

    agent = EchoAgent(name="echo", description="回显", llm=ScriptedLLM(["好的"]), tools=ToolRegistry())
    assert agent.run("你好").answer == "好的"
```

- [ ] **Step 2: 运行测试确认失败**。
- [ ] **Step 3: 实现基类与注册表**：`_call_tool` 记录 `Step` 并累加耗时；中间件链按注册顺序自外向内包装。
- [ ] **Step 4: 运行测试确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat: 智能体基类与注册表"`。

### Task 7: ReAct 智能体

**Files:**
- Create: `agentcode/agents/__init__.py`、`agentcode/agents/prompts.py`、`agentcode/agents/react.py`
- Test: `tests/test_react.py`

**Interfaces:**
- Consumes: `BaseAgent`、`parse_react_output`、`parse_action`、`parse_finish`。
- Produces: `ReActAgent`（`name="react"`），注册进 `default_registry`。

- [ ] **Step 1: 写失败测试**

```python
def test_react_agent_calls_tool_then_finishes():
    llm = ScriptedLLM([
        "Thought: 先查天气\nAction: get_weather[北京]",
        "Thought: 信息够了\nAction: Finish[北京晴，适合去故宫]",
    ])
    tools = ToolRegistry()
    tools.register_tool("get_weather", "查天气", lambda city: f"{city}晴")

    result = ReActAgent(llm=llm, tools=tools).run("北京天气如何")

    assert result.success is True
    assert result.answer == "北京晴，适合去故宫"
    assert result.steps[0].tool == "get_weather"
    assert result.steps[0].observation == "北京晴"


def test_react_agent_stops_at_max_steps():
    llm = ScriptedLLM(["Thought: 继续\nAction: get_weather[北京]"])
    tools = ToolRegistry()
    tools.register_tool("get_weather", "查天气", lambda city: "晴")

    result = ReActAgent(llm=llm, tools=tools, max_steps=2).run("北京天气如何")

    assert result.success is False
    assert "最大步数" in result.error
```

- [ ] **Step 2: 运行测试确认失败**。
- [ ] **Step 3: 实现 `ReActAgent`**：提示词含工具清单与历史；解析失败时追加纠正提示并继续；`Finish` 结束循环。
- [ ] **Step 4: 运行测试确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat: ReAct 智能体"`。

### Task 8: Plan-and-Solve 与 Reflection 智能体

**Files:**
- Create: `agentcode/agents/plan_and_solve.py`、`agentcode/agents/reflection.py`
- Test: `tests/test_plan_and_solve.py`、`tests/test_reflection.py`

**Interfaces:**
- Produces: `Planner.plan(question) -> list[str]`、`Executor.execute(question, plan, ctx) -> str`、`PlanAndSolveAgent`（`name="plan_and_solve"`）、`ReflectionAgent`（`name="reflection"`，参数 `max_iterations=3`）。

- [ ] **Step 1: 写失败测试**

```python
def test_plan_and_solve_runs_each_step():
    llm = ScriptedLLM(["```python\n[\"算苹果数\", \"求和\"]\n```", "周一15个", "总共45个"])
    result = PlanAndSolveAgent(llm=llm, tools=ToolRegistry()).run("三天共卖出多少苹果")
    assert result.answer == "总共45个"
    assert len(result.steps) == 2


def test_reflection_stops_when_no_improvement_needed():
    llm = ScriptedLLM(["def f(): return 1", "该实现已经是最优，无需改进"])
    result = ReflectionAgent(llm=llm, tools=ToolRegistry(), max_iterations=3).run("写一个函数")
    assert "无需改进" in result.answer
    assert result.success is True
```

- [ ] **Step 2: 运行测试确认失败**。
- [ ] **Step 3: 实现两个智能体**：Reflection 在反馈含"无需改进"时提前结束，否则进入下一轮优化；每轮记录 `Step`。
- [ ] **Step 4: 运行测试确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat: Plan-and-Solve 与 Reflection 智能体"`。

### Task 9: CLI 与轨迹导出

**Files:**
- Create: `agentcode/cli.py`、`agentcode/__main__.py`、`configs/example.json`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `default_registry`、`Settings`、`JsonStore`。
- Produces: `main(argv: list[str] | None = None) -> int`；子命令 `run`、`list`、`config`。

- [ ] **Step 1: 写失败测试**

```python
def test_cli_run_with_mock_llm_prints_answer(capsys):
    code = main(["run", "--agent", "react", "--llm", "mock", "--task", "你好"])
    assert code == 0
    assert "最终答案" in capsys.readouterr().out


def test_cli_run_writes_trace_file(tmp_path):
    trace = tmp_path / "trace.json"
    code = main(["run", "--agent", "react", "--llm", "mock", "--task", "你好", "--trace", str(trace)])
    assert code == 0
    assert JsonStore.load(trace)["agent"] == "react"
```

- [ ] **Step 2: 运行测试确认失败**。
- [ ] **Step 3: 实现 CLI**：缺失配置退出码 2，未知智能体列出候选，`--json` 输出机器可读结果，`--config` 读取 JSON 覆盖默认值。
- [ ] **Step 4: 运行测试确认通过**。
- [ ] **Step 5: 提交** — `git commit -m "feat: 命令行入口与轨迹导出"`。

### Task 10: 扩展示例、README 与全量验收

**Files:**
- Create: `agentcode/agents/echo.py`
- Create: `README.md`
- Test: `tests/test_extension.py`

**Interfaces:**
- Produces: `EchoAgent`（`name="echo"`）作为"新增一个智能体只需一个文件"的示范。

- [ ] **Step 1: 写失败测试**：自定义智能体注册后可从注册表创建并运行。
- [ ] **Step 2: 运行测试确认失败**。
- [ ] **Step 3: 实现扩展示例与 README**：含架构图、快速开始、三种范式示例输出、扩展指南、配置说明。
- [ ] **Step 4: 全量验收**：`D:\Anaconda\python.exe -m pytest -q` 全绿；`D:\Anaconda\python.exe -m agentcode list` 与 `... run --agent react --llm mock --task "你好"` 正常输出。
- [ ] **Step 5: 提交** — `git commit -m "docs: README 与扩展示例"`。
