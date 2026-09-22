/* AgentCode 页面逻辑：多会话（保留 / 重命名 / 切换）+ SSE 结果呈现 */

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
  sessionList: document.getElementById("session-list"),
  agentList: document.getElementById("agent-list"),
  newSessionButton: document.getElementById("new-session-button"),
  chatSub: document.getElementById("chat-sub"),
};

const state = {
  config: null,
  sessions: [],
  currentSessionId: null,
  running: false,
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

function selectedAgent() {
  const checked = document.querySelector('input[name="agent"]:checked');
  return checked ? checked.value : PREFERRED_AGENT;
}

async function postJSON(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
  if (!response.ok) throw new Error(`服务返回了 ${response.status}`);
  return response.json();
}

/* ------------------------------------------------------------ 会话标识 */

function readStoredSessionId() {
  try {
    return window.localStorage.getItem(SESSION_KEY);
  } catch (error) {
    return state.memorySessionId;
  }
}

function rememberSessionId(id) {
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
    option.querySelector("input").addEventListener("change", () => updateHeader());
    els.agentList.appendChild(option);
  });
}

/* -------------------------------------------------------------- 会话列表 */

async function loadSessions() {
  try {
    const payload = await (await fetch("/api/sessions")).json();
    state.sessions = payload.sessions || [];
  } catch (error) {
    state.sessions = [];
  }
  renderSessionList();
  return state.sessions;
}

function renderSessionList() {
  els.sessionList.innerHTML = "";
  if (!state.sessions.length) {
    const empty = document.createElement("p");
    empty.className = "session-empty";
    empty.textContent = "还没有会话，点上面的「＋ 新会话」开始。";
    els.sessionList.appendChild(empty);
    return;
  }

  state.sessions.forEach((session) => {
    const row = document.createElement("div");
    row.className = "session-row" + (session.id === state.currentSessionId ? " is-active" : "");
    row.dataset.id = session.id;
    const count = Number(session.message_count || 0);
    row.innerHTML = `
      <button type="button" class="session-name" title="${escapeHtml(session.name)}">${escapeHtml(
        session.name
      )}</button>
      ${count > 0 ? `<span class="session-count" title="共 ${count} 条消息">${count} 条</span>` : ""}
      <span class="icon-group">
        <button type="button" class="icon" data-action="rename" title="重命名">✎</button>
        <button type="button" class="icon" data-action="delete" title="删除">✕</button>
      </span>`;
    row.querySelector(".session-name").addEventListener("click", () => selectSession(session.id));
    row.querySelector('[data-action="rename"]').addEventListener("click", (event) => {
      event.stopPropagation();
      startRename(row, session);
    });
    row.querySelector('[data-action="delete"]').addEventListener("click", (event) => {
      event.stopPropagation();
      removeSession(session);
    });
    els.sessionList.appendChild(row);
  });
}

async function bootSession() {
  const sessions = await loadSessions();
  const stored = readStoredSessionId();
  const target = sessions.find((item) => item.id === stored) || sessions[0];
  if (target) {
    await selectSession(target.id);
  } else {
    await createSession();
  }
}

async function createSession() {
  if (state.running) return;
  try {
    const payload = await postJSON("/api/sessions/create", {});
    rememberSessionId(payload.session.id);
    await loadSessions();
    await selectSession(payload.session.id);
    els.runStatus.textContent = "已开始新会话。";
  } catch (error) {
    els.runStatus.textContent = `新建会话失败：${error.message}`;
  }
  els.task.focus();
}

async function selectSession(sessionId) {
  if (state.running || !sessionId) return;
  let payload;
  try {
    payload = await (await fetch(`/api/session?id=${encodeURIComponent(sessionId)}`)).json();
  } catch (error) {
    els.runStatus.textContent = `读取会话失败：${error.message}`;
    return;
  }
  if (!payload.session) {
    await createSession();
    return;
  }
  state.currentSessionId = payload.session.id;
  rememberSessionId(payload.session.id);
  renderSessionList();
  renderTranscript(payload.messages || []);
  const turns = countTurns(payload.messages || []);
  updateHeader(turns, payload.session.turns || turns, payload.session.name);
  els.runStatus.textContent = "";
}

function startRename(row, session) {
  if (state.running) return;
  const nameButton = row.querySelector(".session-name");
  const input = document.createElement("input");
  input.className = "session-input";
  input.value = session.name;
  nameButton.replaceWith(input);
  input.focus();
  input.select();

  let finished = false;
  const finish = async (save) => {
    if (finished) return;
    finished = true;
    const nextName = input.value.trim();
    let finalName = session.name;
    if (save && nextName && nextName !== session.name) {
      try {
        await postJSON("/api/sessions/rename", { session_id: session.id, name: nextName });
        finalName = nextName;
      } catch (error) {
        els.runStatus.textContent = `重命名失败：${error.message}`;
      }
    }
    await loadSessions();
    if (state.currentSessionId === session.id) {
      const turns = countTurns(currentTurnMessages);
      updateHeader(turns, turns, finalName);
    }
  };

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      finish(true);
    } else if (event.key === "Escape") {
      event.preventDefault();
      finish(false);
    }
  });
  input.addEventListener("blur", () => finish(true));
}

async function removeSession(session) {
  if (state.running) return;
  if (!window.confirm(`删除会话「${session.name}」？对话记录会一起删掉。`)) return;
  try {
    await postJSON("/api/sessions/delete", { session_id: session.id });
  } catch (error) {
    els.runStatus.textContent = `删除失败：${error.message}`;
    return;
  }
  const wasCurrent = state.currentSessionId === session.id;
  await loadSessions();
  if (wasCurrent) {
    if (state.sessions.length) {
      await selectSession(state.sessions[0].id);
    } else {
      await createSession();
    }
  }
  els.runStatus.textContent = "会话已删除。";
}

/* ---------------------------------------------------------------- 渲染 */

let currentTurnMessages = [];

function countTurns(messages) {
  return (messages || []).filter((message) => message.role === "assistant").length;
}

function updateHeader(turns, kept, name) {
  const limit = state.config ? state.config.memory_turns : 0;
  const total = Number.isFinite(turns) ? turns : countTurns(currentTurnMessages);
  const remembered = Number.isFinite(kept) ? kept : total;
  const parts = [name ? `「${name}」` : "", `${selectedAgent()} 智能体`, `共 ${total} 轮对话`];
  if (remembered > 0 && limit) parts.push(`已记住 ${remembered}/${limit} 轮上下文`);
  if (state.config && !state.config.ready) {
    parts.push(`缺少 ${state.config.missing_keys.join("、")}，运行会失败`);
  }
  els.chatSub.textContent = parts.filter(Boolean).join(" · ");
}

function renderTranscript(messages) {
  currentTurnMessages = messages || [];
  els.messages.querySelectorAll(".msg").forEach((node) => node.remove());
  els.empty.hidden = currentTurnMessages.length > 0;
  currentTurnMessages.forEach((message) => {
    appendMessage(
      message.role,
      message.content,
      undefined,
      message.success === false,
      message.artifacts || []
    );
  });
  scrollToEnd();
}

function appendMessage(role, text, metaText, isError = false, artifacts = []) {
  const wrapper = document.createElement("div");
  wrapper.className = `msg msg-${role}` + (isError ? " msg-error" : "");
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
  const chips = buildArtifacts(artifacts);
  if (chips) wrapper.appendChild(chips);
  els.messages.appendChild(wrapper);
  scrollToEnd();
  return wrapper;
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  return `${(value / 1024).toFixed(1)} KB`;
}

function buildArtifacts(artifacts) {
  if (!artifacts || !artifacts.length) return null;
  const box = document.createElement("div");
  box.className = "artifacts";
  artifacts.forEach((item) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "artifact";
    chip.title = `点击查看 ${item.path}`;
    chip.innerHTML = `<span class="artifact-icon">📄</span><span class="artifact-path">${escapeHtml(
      item.path
    )}</span><span class="artifact-size">${formatBytes(item.bytes)}</span>`;
    chip.addEventListener("click", () => openViewer(item.path, item.bytes));
    box.appendChild(chip);
  });
  return box;
}

async function openViewer(path, bytes) {
  let payload;
  try {
    payload = await (await fetch(`/api/file?path=${encodeURIComponent(path)}`)).json();
  } catch (error) {
    els.runStatus.textContent = `读取文件失败：${error.message}`;
    return;
  }
  if (payload.error) {
    els.runStatus.textContent = payload.error;
    return;
  }

  const overlay = document.createElement("div");
  overlay.className = "viewer";
  overlay.innerHTML = `
    <div class="viewer-panel" role="dialog" aria-modal="true" aria-label="文件内容">
      <header class="viewer-head">
        <span class="viewer-title"></span>
        <button type="button" class="viewer-close" aria-label="关闭">✕</button>
      </header>
      <pre class="viewer-body"></pre>
      <p class="viewer-meta"></p>
    </div>`;
  overlay.querySelector(".viewer-title").textContent = path;
  overlay.querySelector(".viewer-body").textContent = payload.content;
  overlay.querySelector(".viewer-meta").textContent = [
    `共 ${payload.bytes} 字节`,
    payload.truncated ? "内容过长，仅显示前 20000 字符" : "",
  ]
    .filter(Boolean)
    .join("，");

  const close = () => {
    document.removeEventListener("keydown", onKey);
    overlay.remove();
  };
  const onKey = (event) => {
    if (event.key === "Escape") close();
  };
  overlay.querySelector(".viewer-close").addEventListener("click", close);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close();
  });
  document.addEventListener("keydown", onKey);
  document.body.appendChild(overlay);
  void bytes;
}

function appendPending() {
  const wrapper = document.createElement("div");
  wrapper.className = "msg msg-assistant";
  wrapper.innerHTML = '<div class="msg-body"><span class="dots"><i></i><i></i><i></i></span></div>';
  els.messages.appendChild(wrapper);
  scrollToEnd();
  return wrapper;
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
  const chips = buildArtifacts(options.artifacts);
  if (chips) node.appendChild(chips);
  scrollToEnd();
}

function scrollToEnd() {
  els.messages.scrollTop = els.messages.scrollHeight;
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
  if (!state.currentSessionId) {
    await createSession();
    if (!state.currentSessionId) return;
  }

  const sessionId = state.currentSessionId;
  const payload = { agent: selectedAgent(), task, session_id: sessionId };

  appendMessage("user", task);
  currentTurnMessages.push({ role: "user", content: task, success: true });
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

    await readEventStream(response, pending, sessionId);
  } catch (error) {
    failPending(pending, `无法连接本地服务：${error.message}`);
  } finally {
    stopProgressTimer();
    setRunning(false);
    els.task.focus();
  }
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

async function readEventStream(response, pending, sessionId) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      handleEventBlock(buffer.slice(0, boundary), pending, sessionId);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
  }
}

function handleEventBlock(block, pending, sessionId) {
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

async function resolvePending(pending, result) {
  const usage = result.usage || {};
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
    artifacts: result.artifacts || [],
  });
  currentTurnMessages.push({
    role: "assistant",
    content: answer,
    success: result.success,
    artifacts: result.artifacts || [],
  });

  await loadSessions();
  updateHeader(result.turn_index, result.memory_turns);
  els.runStatus.textContent = `已完成，用时 ${formatSeconds(result.duration_ms)}`;
}

function failPending(pending, message) {
  const text = `${message} 请检查 .env 里的模型配置。`;
  renderMessage(pending, {
    role: "assistant",
    text,
    className: "msg-error",
  });
  currentTurnMessages.push({ role: "assistant", content: text, success: false });
  els.runStatus.textContent = "失败";
  loadSessions();
}

function setRunning(running) {
  state.running = running;
  els.sendButton.disabled = running;
  els.newSessionButton.disabled = running;
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
      updateHeader();
    }
  }
  autoGrow();
  els.task.focus();
}

els.form.addEventListener("submit", submitRun);
els.newSessionButton.addEventListener("click", createSession);
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

async function boot() {
  await loadMeta();
  await bootSession();
}

boot();
