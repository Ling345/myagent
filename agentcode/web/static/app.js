/* AgentCode 页面逻辑：选智能体 → 提交任务 → 解析 SSE → 以对话形式呈现结果 */

const PREFERRED_AGENT = "react";
const TIMER_INTERVAL_MS = 200;
const SESSION_KEY = "agentcode-session";
const MAX_TEXTAREA_HEIGHT = 200;

const els = {
  form: document.getElementById("run-form"),
  task: document.getElementById("task"),
  sendButton: document.getElementById("send-button"),
  runStatus: document.getElementById("run-status"),
  messages: document.getElementById("messages"),
  empty: document.getElementById("empty"),
  agentList: document.getElementById("agent-list"),
  newSessionButton: document.getElementById("new-session-button"),
  configList: document.getElementById("config-list"),
  sessionHint: document.getElementById("session-hint"),
  chatSub: document.getElementById("chat-sub"),
  exportButton: document.getElementById("export-button"),
};

const state = {
  config: null,
  running: false,
  lastResult: null,
  timer: null,
  pending: null,
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

function selectedAgent() {
  const checked = document.querySelector('input[name="agent"]:checked');
  return checked ? checked.value : PREFERRED_AGENT;
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

/* ------------------------------------------------------------ 加载元信息 */

async function loadMeta() {
  try {
    const [agentsResponse, configResponse] = await Promise.all([
      fetch("/api/agents"),
      fetch("/api/config"),
    ]);
    const agentsPayload = await agentsResponse.json();
    state.config = await configResponse.json();
    renderAgents(agentsPayload.agents);
    renderConfig(state.config);
    updateHeader(0, 0);
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
    option.querySelector("input").addEventListener("change", () => updateHeader(0, 0));
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

function updateHeader(turnIndex, kept) {
  const limit = state.config ? state.config.memory_turns : 0;
  const parts = [`${selectedAgent()} 智能体`];
  if (turnIndex > 0) parts.push(`本次会话第 ${turnIndex} 轮`);
  if (kept > 0 && limit) parts.push(`已记住 ${kept}/${limit} 轮上下文`);
  els.chatSub.textContent = parts.join(" · ");
  els.sessionHint.textContent =
    kept > 0
      ? `当前会话已记住 ${kept} 轮上下文${limit ? `（上限 ${limit} 轮）` : ""}。`
      : "当前会话还没有上下文，提问一次之后就会记住。";
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
    agent: selectedAgent(),
    task,
    session_id: sessionId(),
  };

  appendMessage("user", task);
  els.empty.hidden = true;
  els.task.value = "";
  autoGrow();
  const pending = appendPending();
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
      failPending(pending, detail.error || `服务返回了 ${response.status}。`);
      return;
    }

    await readEventStream(response, pending);
  } catch (error) {
    failPending(pending, `无法连接本地服务：${error.message}`);
  } finally {
    stopProgressTimer();
    setRunning(false);
    els.task.focus();
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
  clearTranscript();
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

async function readEventStream(response, pending) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      handleEventBlock(buffer.slice(0, boundary), pending);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
  }
}

function handleEventBlock(block, pending) {
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

  // step 与 status 事件只会透露推理过程，页面上不做任何渲染
  if (type === "answer") {
    resolvePending(pending, payload);
  } else if (type === "error") {
    failPending(pending, payload.message || "运行失败。");
  }
}

/* ---------------------------------------------------------------- 渲染 */

function appendMessage(role, text, metaText) {
  const wrapper = document.createElement("div");
  wrapper.className = `msg msg-${role}`;
  const body = document.createElement("div");
  body.className = "msg-body";
  body.textContent = text;
  wrapper.appendChild(body);
  if (metaText) {
    const meta = document.createElement("p");
    meta.className = "msg-meta";
    meta.textContent = metaText;
    wrapper.appendChild(meta);
  }
  els.messages.appendChild(wrapper);
  scrollToEnd();
  return wrapper;
}

function appendPending() {
  const wrapper = document.createElement("div");
  wrapper.className = "msg msg-assistant";
  wrapper.innerHTML = '<div class="msg-body"><span class="dots"><i></i><i></i><i></i></span></div>';
  els.messages.appendChild(wrapper);
  scrollToEnd();
  return wrapper;
}

function resolvePending(pending, result) {
  state.lastResult = result;
  els.exportButton.disabled = false;
  state.pending = null;

  const usage = result.usage || {};
  const turnIndex = Number(result.turn_index || 0);
  const kept = Number(result.memory_turns || 0);
  const answer = result.answer || "（这次没有得出结论）";
  const meta = [
    result.success ? "已完成" : "未得出结论",
    `模型调用 ${usage.calls || 0} 次`,
    `约 ${usage.total_tokens || 0} token${usage.estimated ? "（估算）" : ""}`,
    `耗时 ${formatSeconds(result.duration_ms)}`,
  ].join("，");

  renderMessage(pending, {
    role: "assistant",
    text: answer,
    meta,
    className: result.success ? "" : "msg-error",
  });
  updateHeader(turnIndex, kept);
  els.runStatus.textContent = `已完成，用时 ${formatSeconds(result.duration_ms)}`;
}

function failPending(pending, message) {
  renderMessage(pending, {
    role: "assistant",
    text: `${message} 请检查左侧面板里的模型、接口与密钥。`,
    className: "msg-error",
  });
  els.runStatus.textContent = "失败";
}

function renderMessage(node, options) {
  node.className = `msg msg-${options.role} ${options.className || ""}`.trim();
  node.innerHTML = "";
  const body = document.createElement("div");
  body.className = "msg-body";
  body.textContent = options.text;
  node.appendChild(body);
  if (options.meta) {
    const meta = document.createElement("p");
    meta.className = "msg-meta";
    meta.textContent = options.meta;
    node.appendChild(meta);
  }
  scrollToEnd();
}

function clearTranscript() {
  els.messages.querySelectorAll(".msg").forEach((node) => node.remove());
  els.empty.hidden = false;
  els.exportButton.disabled = true;
  state.lastResult = null;
  state.pending = null;
  updateHeader(0, 0);
  els.runStatus.textContent = "";
}

function scrollToEnd() {
  els.messages.scrollTop = els.messages.scrollHeight;
}

function setRunning(running) {
  state.running = running;
  els.sendButton.disabled = running;
  if (running) els.runStatus.textContent = "正在启动…";
}

/* ------------------------------------------------------------ 输入体验 */

function autoGrow() {
  els.task.style.height = "auto";
  els.task.style.height = `${Math.min(els.task.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
}

function fillSample(event) {
  const button = event.currentTarget;
  els.task.value = button.dataset.task || "";
  const agent = button.dataset.agent;
  if (agent) {
    const radio = document.querySelector(`input[name="agent"][value="${agent}"]`);
    if (radio) {
      radio.checked = true;
      updateHeader(0, 0);
    }
  }
  autoGrow();
  els.task.focus();
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
document.querySelectorAll(".sample").forEach((button) => {
  button.addEventListener("click", fillSample);
});
els.task.addEventListener("input", autoGrow);
els.task.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!els.sendButton.disabled) els.form.requestSubmit();
  }
});

loadMeta();
