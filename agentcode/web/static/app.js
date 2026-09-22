/* AgentCode 页面逻辑：查询智能体 → 提交任务 → 解析 SSE → 只渲染最终结果 */

const PREFERRED_AGENT = "react";
const TIMER_INTERVAL_MS = 200;
const SESSION_KEY = "agentcode-session";

const els = {
  form: document.getElementById("run-form"),
  agentList: document.getElementById("agent-list"),
  task: document.getElementById("task"),
  runButton: document.getElementById("run-button"),
  runStatus: document.getElementById("run-status"),
  sessionHint: document.getElementById("session-hint"),
  newSessionButton: document.getElementById("new-session-button"),
  configList: document.getElementById("config-list"),
  empty: document.getElementById("empty"),
  answer: document.getElementById("answer"),
  answerText: document.getElementById("answer-text"),
  answerMeta: document.getElementById("answer-meta"),
  failure: document.getElementById("failure"),
  failureText: document.getElementById("failure-text"),
  exportButton: document.getElementById("export-button"),
  clearButton: document.getElementById("clear-button"),
};

const state = {
  config: null,
  running: false,
  lastResult: null,
  timer: null,
  memorySessionId: null,
};

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]
  );
}

function formatSeconds(ms) {
  return `${(Number(ms || 0) / 1000).toFixed(1)} 秒`;
}

/* ------------------------------------------------------- 会话标识（上下文） */

function newSessionId() {
  if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
  return `s-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
}

function sessionId() {
  try {
    let id = window.localStorage.getItem(SESSION_KEY);
    if (!id) {
      id = newSessionId();
      window.localStorage.setItem(SESSION_KEY, id);
    }
    return id;
  } catch (error) {
    // 隐私模式下 localStorage 可能不可用，退回内存里的标识
    if (!state.memorySessionId) state.memorySessionId = newSessionId();
    return state.memorySessionId;
  }
}

function storeSessionId(id) {
  try {
    window.localStorage.setItem(SESSION_KEY, id);
  } catch (error) {
    state.memorySessionId = id;
  }
}

function updateSessionHint(kept, limit) {
  if (kept > 0) {
    const cap = limit > 0 ? `（上限 ${limit} 轮）` : "";
    els.sessionHint.textContent = `当前会话已记住 ${kept} 轮上下文${cap}，追问时不必重复背景。`;
    return;
  }
  els.sessionHint.textContent = "当前会话还没有上下文，提问一次之后就会记住。";
}

/* ------------------------------------------------------------ 加载元信息 */

async function loadMeta() {
  updateSessionHint(0, 0);
  try {
    const [agentsResponse, configResponse] = await Promise.all([
      fetch("/api/agents"),
      fetch("/api/config"),
    ]);
    const agentsPayload = await agentsResponse.json();
    state.config = await configResponse.json();
    renderAgents(agentsPayload.agents);
    renderConfig(state.config);
  } catch (error) {
    els.runStatus.textContent = `无法连接本地服务：${error.message}`;
  }
}

function renderAgents(agents) {
  els.agentList.innerHTML = "";
  const hasPreferred = agents.some((agent) => agent.name === PREFERRED_AGENT);

  agents.forEach((agent, index) => {
    const checked =
      agent.name === PREFERRED_AGENT || (!hasPreferred && index === 0) ? "checked" : "";
    const option = document.createElement("label");
    option.className = "agent-option";
    option.innerHTML = `
      <input type="radio" name="agent" value="${escapeHtml(agent.name)}" ${checked} />
      <span class="agent-name">${escapeHtml(agent.name)}</span>
      <span class="agent-desc">${escapeHtml(agent.description)}</span>`;
    els.agentList.appendChild(option);
  });
}

function renderConfig(config) {
  const rows = [
    ["模型", config.model || "未配置"],
    ["接口", config.base_url || "未配置"],
    ["密钥", config.api_key || "未配置"],
    ["搜索工具", config.serpapi_configured ? "已配置" : "未配置"],
    ["步数上限", String(config.max_steps)],
    ["记忆轮数", String(config.memory_turns ?? "—")],
  ];
  els.configList.innerHTML = rows
    .map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`)
    .join("");

  if (!config.ready) {
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = `缺少 ${config.missing_keys.join("、")}，请先补齐 .env，否则运行会失败。`;
    els.configList.after(note);
  }
}

/* -------------------------------------------------------------- 运行流程 */

async function submitRun(event) {
  event.preventDefault();
  if (state.running) return;

  const task = els.task.value.trim();
  if (!task) {
    els.task.focus();
    els.runStatus.textContent = "先写下一个任务。";
    return;
  }

  const payload = {
    agent: new FormData(els.form).get("agent"),
    task,
    session_id: sessionId(),
  };

  clearResult();
  setRunning(true);
  startProgressTimer(performance.now());

  try {
    const response = await fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      showFailure(detail.error || `服务返回了 ${response.status}。`);
      return;
    }

    await readEventStream(response);
  } catch (error) {
    showFailure(`无法连接本地服务：${error.message}`);
  } finally {
    stopProgressTimer();
    setRunning(false);
  }
}

async function startNewSession() {
  if (state.running) return;
  const previous = sessionId();
  try {
    await fetch("/api/session/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: previous }),
    });
  } catch (error) {
    // 服务没起来也不影响本地换一个会话标识
  }
  storeSessionId(newSessionId());
  clearResult();
  updateSessionHint(0, 0);
  els.runStatus.textContent = "已开始新会话，上下文已清空。";
}

function startProgressTimer(startedAt) {
  stopProgressTimer();
  state.timer = setInterval(() => {
    const seconds = ((performance.now() - startedAt) / 1000).toFixed(1);
    els.runStatus.textContent = `运行中… ${seconds} 秒`;
  }, TIMER_INTERVAL_MS);
}

function stopProgressTimer() {
  if (state.timer) {
    clearInterval(state.timer);
    state.timer = null;
  }
}

async function readEventStream(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      handleEventBlock(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
  }
}

function handleEventBlock(block) {
  let type = "message";
  const dataLines = [];
  block.split("\n").forEach((line) => {
    if (line.startsWith("event:")) type = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  });
  if (!dataLines.length) return;

  let payload;
  try {
    payload = JSON.parse(dataLines.join("\n"));
  } catch (error) {
    return;
  }
  handleEvent(type, payload);
}

function handleEvent(type, payload) {
  // step 与 status 事件只会透露推理过程，页面上不做任何渲染
  if (type === "answer") {
    showAnswer(payload);
  } else if (type === "error") {
    showFailure(payload.message || "运行失败。");
  }
}

/* ---------------------------------------------------------------- 渲染 */

function showAnswer(result) {
  state.lastResult = result;
  els.empty.hidden = true;
  els.answer.hidden = false;
  els.exportButton.disabled = false;
  els.answerText.textContent = result.answer || "（这次没有得出结论）";
  const usage = result.usage || {};
  const turnIndex = Number(result.turn_index || 0);
  const kept = Number(result.memory_turns || 0);
  const limit = Number(result.memory_limit || 0);
  els.answerMeta.textContent = [
    result.success ? "已完成" : "未得出结论",
    turnIndex > 0 ? `本次会话第 ${turnIndex} 轮` : "",
    `模型调用 ${usage.calls || 0} 次`,
    `约 ${usage.total_tokens || 0} token${usage.estimated ? "（估算）" : ""}`,
    `耗时 ${formatSeconds(result.duration_ms)}`,
  ]
    .filter(Boolean)
    .join("，");
  updateSessionHint(kept, limit);
  els.runStatus.textContent = `已完成，用时 ${formatSeconds(result.duration_ms)}`;
}

function showFailure(message) {
  els.empty.hidden = true;
  els.failure.hidden = false;
  els.failureText.textContent = `${message} 请检查左侧面板里的模型、接口与密钥。`;
  els.runStatus.textContent = "失败";
}

function clearResult() {
  els.answer.hidden = true;
  els.failure.hidden = true;
  els.empty.hidden = false;
  els.exportButton.disabled = true;
  state.lastResult = null;
}

function setRunning(running) {
  state.running = running;
  els.runButton.disabled = running;
  els.runButton.textContent = running ? "运行中…" : "运行";
  if (running) els.runStatus.textContent = "正在启动…";
}

/* ---------------------------------------------------------------- 导出 */

function exportResult() {
  if (!state.lastResult) return;
  const blob = new Blob([JSON.stringify(state.lastResult, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${state.lastResult.agent}-result.json`;
  link.click();
  URL.revokeObjectURL(url);
}

els.form.addEventListener("submit", submitRun);
els.newSessionButton.addEventListener("click", startNewSession);
els.exportButton.addEventListener("click", exportResult);
els.clearButton.addEventListener("click", clearResult);

loadMeta();
