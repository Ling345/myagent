/* AgentCode 页面逻辑：多会话（保留 / 重命名 / 切换）+ SSE 结果呈现 */

const PREFERRED_AGENT = "react";
const TIMER_INTERVAL_MS = 200;
const SESSION_KEY = "agentcode-session";
const MAX_TEXTAREA_HEIGHT = 200;
const SIDEBAR_WIDTH_KEY = "agentcode-sidebar-width";
const AGENTS_COLLAPSED_KEY = "agentcode-agents-collapsed";
const MIN_SIDEBAR_WIDTH = 240;
const MAX_SIDEBAR_WIDTH = 420;

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
  app: document.querySelector(".app"),
  login: document.getElementById("login"),
  loginForm: document.getElementById("login-form"),
  loginName: document.getElementById("login-name"),
  loginPassword: document.getElementById("login-password"),
  loginButton: document.getElementById("login-button"),
  loginError: document.getElementById("login-error"),
  account: document.getElementById("account"),
  accountName: document.getElementById("account-name"),
  accountQuota: document.getElementById("account-quota"),
  sidebar: document.getElementById("sidebar"),
  agentToggle: document.getElementById("agent-toggle"),
  agentToggleLabel: document.getElementById("agent-toggle-label"),
  sidebarResizer: document.getElementById("sidebar-resizer"),
  billingToggle: document.getElementById("billing-toggle"),
  billingPanel: document.getElementById("billing-panel"),
  billingClose: document.getElementById("billing-close"),
  billingPlan: document.getElementById("billing-plan"),
  billingExpiry: document.getElementById("billing-expiry"),
  billingBarFill: document.getElementById("billing-bar-fill"),
  billingUsageText: document.getElementById("billing-usage-text"),
  billingPlans: document.getElementById("billing-plans"),
  billingOrders: document.getElementById("billing-orders"),
  dataToggle: document.getElementById("data-toggle"),
  dataPanel: document.getElementById("data-panel"),
  dataClose: document.getElementById("data-close"),
  dataExport: document.getElementById("data-export"),
  dataPurge: document.getElementById("data-purge"),
  dataDelete: document.getElementById("data-delete"),
  dataConfirm: document.getElementById("data-confirm"),
  dataConfirmName: document.getElementById("data-confirm-name"),
  dataConfirmInput: document.getElementById("data-confirm-input"),
  dataConfirmCancel: document.getElementById("data-confirm-cancel"),
  dataConfirmOk: document.getElementById("data-confirm-ok"),
  trashHint: document.getElementById("trash-hint"),
  trashList: document.getElementById("trash-list"),
  trashEmpty: document.getElementById("trash-empty"),
  notifyEmail: document.getElementById("notify-email"),
  notifySave: document.getElementById("notify-save"),
  tokenName: document.getElementById("token-name"),
  tokenCreate: document.getElementById("token-create"),
  tokenFresh: document.getElementById("token-fresh"),
  tokenList: document.getElementById("token-list"),
  tokenToggle: document.getElementById("token-toggle"),
  tokenPanel: document.getElementById("token-panel"),
  tokenClose: document.getElementById("token-close"),
  uploadButton: document.getElementById("upload-button"),
  uploadInput: document.getElementById("upload-input"),
  uploadChips: document.getElementById("upload-chips"),
  composer: document.getElementById("run-form"),
  logoutButton: document.getElementById("logout-button"),
};

const state = {
  config: null,
  account: null,
  sessions: [],
  currentSessionId: null,
  running: false,
  timer: null,
  memorySessionId: null,
  //: 当前这次运行的服务端编号，断线重连时凭它续上
  runId: null,
  //: 已经收到多少条运行事件（等于服务端的绝对事件下标）
  eventCount: 0,
  //: 本次运行是否已经拿到最终结果（answer/error）
  settled: false,
  //: 「智能体」列表是否收起（收起后把空间让给套餐面板）
  agentsCollapsed: false,
  //: 是否跟随最新消息（用户往上翻看历史时置为 false，翻回底部再恢复）
  followTail: true,
};

//: 断线后最多重连几次、每次等多久（毫秒，按次数递增）
const MAX_RESUME_ATTEMPTS = 5;
const RESUME_BACKOFF_MS = 700;

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
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    // 把服务端写好的中文原因带出来。不这样做的话，用户只看到"服务返回了 400"，
    // 而真正的原因（"请输入你自己的用户名以确认注销"）被丢在半路。
    throw new Error(data.error || `服务返回了 ${response.status}`);
  }
  return data;
}

async function getJSON(path) {
  const response = await fetch(path);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `服务返回了 ${response.status}`);
  }
  return data;
}

/* ------------------------------------------------------------ 登录与账号 */

function formatTokens(value) {
  const number = Number(value || 0);
  if (number >= 1000) return `${(number / 1000).toFixed(1)}k`;
  return String(number);
}

async function fetchAccount() {
  try {
    const response = await fetch("/api/me");
    if (!response.ok) return null;
    const payload = await response.json();
    return payload.account || null;
  } catch (error) {
    return null;
  }
}

function renderAccount(account) {
  state.account = account;
  if (!account) {
    els.account.hidden = true;
    return;
  }
  els.account.hidden = false;
  els.accountName.textContent = account.name;
  if (account.unlimited) {
    els.accountQuota.textContent = `今日 ${formatTokens(account.used_today)}`;
    els.accountQuota.title = `不限量套餐，今日已用 ${account.used_today} token`;
  } else {
    els.accountQuota.textContent = `今日 ${formatTokens(account.used_today)}/${formatTokens(
      account.daily_token_limit
    )}`;
    els.accountQuota.title = `今日已用 ${account.used_today} token，共 ${account.calls_today} 次调用`;
  }
}

function showLogin(message = "") {
  els.app.hidden = true;
  els.login.hidden = false;
  els.loginError.textContent = message;
  els.loginPassword.value = "";
  els.loginName.focus();
}

function showApp() {
  els.login.hidden = true;
  els.app.hidden = false;
}

async function submitLogin(event) {
  event.preventDefault();
  const name = els.loginName.value.trim();
  const password = els.loginPassword.value;
  if (!name || !password) {
    els.loginError.textContent = "请填写账号和密码。";
    return;
  }
  els.loginButton.disabled = true;
  els.loginError.textContent = "正在登录…";
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, password }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      els.loginError.textContent = payload.error || "登录失败。";
      return;
    }
    els.loginError.textContent = "";
    showApp();
    renderAccount(payload.account);
    await loadMeta();
    await bootSession();
  } catch (error) {
    els.loginError.textContent = `无法连接服务：${error.message}`;
  } finally {
    els.loginButton.disabled = false;
  }
}

async function logout() {
  try {
    await fetch("/api/logout", { method: "POST" });
  } catch (error) {
    // 网络失败也把界面退回登录页
  }
  state.account = null;
  state.currentSessionId = null;
  state.sessions = [];
  renderAccount(null);
  showLogin("已退出登录。");
}

async function refreshAccount() {
  const account = await fetchAccount();
  if (!account) {
    showLogin("登录已过期，请重新登录。");
    return null;
  }
  renderAccount(account);
  return account;
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
  // 这个会话可能还有一次任务在服务端跑着（比如刚才刷新过页面），接上去继续看
  await attachRunningRun(payload.session.id);
}

/**
 * 会话里如果有还没跑完的运行，直接接上去继续看。
 *
 * 这正是"断线续传"里"刷新页面"那一半：任务从来没停过，
 * 只是浏览器把连接丢了，重新订阅一下就能看到结果。
 */
async function attachRunningRun(sessionId) {
  let payload;
  try {
    payload = await (await fetch(`/api/runs?session_id=${encodeURIComponent(sessionId)}`)).json();
  } catch (error) {
    return;
  }
  const running = (payload.runs || []).find((run) => run.status === "running");
  if (!running) return;

  state.runId = running.id;
  state.eventCount = 0;
  state.settled = false;
  const pending = appendPending();
  setRunning(true);
  startProgressTimer(performance.now());
  els.runStatus.textContent = "这个会话还有任务在跑，正在接上…";
  try {
    await streamWithResume(null, pending);
  } catch (error) {
    failPending(pending, `接上运行失败：${error.message}`);
  } finally {
    stopProgressTimer();
    setRunning(false);
    refreshAccount();
  }
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
  // 切换会话时总是跳到最新一条，不沿用上一个会话的跟随状态
  state.followTail = true;
  scrollToEnd(true);
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

function isNearBottom() {
  const box = els.messages;
  return box.scrollHeight - box.clientHeight - box.scrollTop <= 120;
}

function scrollToEnd(force = false) {
  const box = els.messages;
  if (!force && !state.followTail) return;
  const apply = () => {
    box.scrollTop = box.scrollHeight;
  };
  apply();
  // 新内容的高度（字体、代码块、产物胶囊）可能这一帧之后才定下来，补一次
  window.requestAnimationFrame(apply);
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
  state.runId = null;
  state.eventCount = 0;
  state.settled = false;
  setRunning(true);
  startProgressTimer(performance.now());

  try {
    await streamWithResume(payload, pending);
  } catch (error) {
    failPending(pending, `无法连接本地服务：${error.message}`);
  } finally {
    stopProgressTimer();
    setRunning(false);
    refreshAccount();
    els.task.focus();
  }
}

/**
 * 跑一次任务，并在连接断掉时自动续上。
 *
 * 服务端把运行的状态放在注册表里，连接只是订阅者：刷新页面、
 * 网络抖一下都不会让任务白跑，重新连上用 from 下标就能把漏掉的事件补回来。
 */
async function streamWithResume(payload, pending) {
  // 已经知道 run_id 说明是"接上一次运行"，否则才是新开一次
  let first = !state.runId;
  let attempts = 0;

  while (true) {
    let response;
    try {
      response = first
        ? await fetch("/api/run", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          })
        : await fetch(
            `/api/run/stream?run_id=${encodeURIComponent(state.runId)}&from=${state.eventCount}`
          );
    } catch (error) {
      if (!(await waitBeforeResume(pending, attempts))) {
        failPending(pending, `无法连接本地服务：${error.message}`);
        return;
      }
      attempts += 1;
      continue;
    }
    first = false;

    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      failPending(pending, detail.error || `服务返回了 ${response.status}。`);
      return;
    }

    const outcome = await readEventStream(response, pending);
    if (outcome === "ended" && state.settled) return;

    // 连接断了、或者断在了半路：只要知道 run_id 就还能续
    if (!state.runId) {
      failPending(pending, "连接中断，而且没拿到运行编号，无法续传。");
      return;
    }
    if (!(await waitBeforeResume(pending, attempts))) {
      failPending(pending, "连接反复中断，已放弃续传；任务在服务端仍会跑完。");
      return;
    }
    attempts += 1;
  }
}

/** 重连前的等待与次数控制；返回 false 表示不该再试了。 */
async function waitBeforeResume(pending, attempts) {
  if (attempts >= MAX_RESUME_ATTEMPTS) return false;
  els.runStatus.textContent = `连接中断，正在重连…（第 ${attempts + 1} 次）`;
  await new Promise((resolve) => setTimeout(resolve, RESUME_BACKOFF_MS * (attempts + 1)));
  return true;
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

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) return "ended";
      buffer += decoder.decode(value, { stream: true });

      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        handleEventBlock(buffer.slice(0, boundary), pending);
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n");
      }
    }
  } catch (error) {
    // 网络断了、或者服务重启了：交给上层决定要不要重连
    return "broken";
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

  // run 帧只是告诉前端"这次运行的编号是什么"，不占事件下标
  if (type === "run") {
    state.runId = payload.id || state.runId;
    return;
  }
  // 服务端丢了老事件：以它给的下标为准重新对齐
  if (type === "truncated") {
    state.eventCount = Number(payload.from) || 0;
    return;
  }
  state.eventCount += 1;

  // step 与 status 事件只会透露推理过程，页面上不做任何渲染
  if (type === "answer") {
    resolvePending(pending, payload);
  } else if (type === "error") {
    failPending(pending, payload.message || "运行失败。");
  }
}

async function resolvePending(pending, result) {
  state.settled = true;
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
  state.settled = true;
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
els.messages.addEventListener("scroll", () => {
  state.followTail = isNearBottom();
});

async function boot() {
  const account = await fetchAccount();
  if (!account) {
    showLogin();
    return;
  }
  showApp();
  renderAccount(account);
  await loadMeta();
  // 等智能体列表渲染完再恢复折叠状态，标题上才能显示正确的当前智能体名
  restoreLayout();
  await bootSession();
}

els.loginForm.addEventListener("submit", submitLogin);
els.logoutButton.addEventListener("click", logout);

// ------------------------------------------------------------ 套餐与用量面板

/**
 * 打开面板并拉一次最新数据。
 *
 * 面板默认收起：主界面只给结果，这是既有约定。
 */
async function openBilling() {
  els.billingPanel.hidden = false;
  try {
    const [billing, plans] = await Promise.all([
      (await fetch("/api/billing")).json(),
      (await fetch("/api/plans")).json(),
    ]);
    renderBilling(billing, plans.plans || []);
  } catch (error) {
    els.billingUsageText.textContent = `读取失败：${error.message}`;
  }
}

function renderBilling(billing, plans) {
  const plan = billing.plan || {};
  els.billingPlan.textContent = plan.title || plan.name || "—";
  els.billingExpiry.textContent = billing.plan_expires_at
    ? `到期 ${String(billing.plan_expires_at).slice(0, 10)}`
    : plan.name === "owner"
      ? "不过期"
      : "永久";

  const unlimited = billing.remaining_today < 0;
  const limit = billing.daily_limit || 0;
  const used = billing.used_today || 0;
  const ratio = unlimited || !limit ? 0 : Math.min(1, used / limit);
  els.billingBarFill.style.width = `${Math.round(ratio * 100)}%`;
  els.billingBarFill.classList.toggle("is-over", !unlimited && used >= limit);
  els.billingUsageText.textContent = unlimited
    ? `今日已用 ${formatTokens(used)} token（不限量）`
    : `今日 ${formatTokens(used)} / ${formatTokens(limit)} token，本周期累计 ${formatTokens(
        billing.used_this_period || 0
      )}`;

  els.billingPlans.innerHTML = plans
    .map(
      (item) => `
      <div class="billing-plan-card">
        <span>${escapeHtml(item.title)}</span>
        <span>${item.price_cents === 0 ? "免费" : `¥${(item.price_cents / 100).toFixed(0)}/月`}</span>
        <span>${formatTokens(item.daily_tokens)} token/天</span>
        <button type="button" data-plan="${escapeHtml(item.name)}">升级</button>
      </div>`
    )
    .join("");
  els.billingPlans.querySelectorAll("button[data-plan]").forEach((button) => {
    button.addEventListener("click", () => checkout(button.dataset.plan));
  });

  els.billingOrders.innerHTML = (billing.orders || [])
    .map(
      (order) =>
        `<li>${String(order.created_at).slice(0, 10)}　${escapeHtml(order.plan)}　` +
        `¥${(order.amount_cents / 100).toFixed(2)}　[${escapeHtml(order.status)}]</li>`
    )
    .join("");
}

/** 下单。手动模式下拿到的是「待支付」，要管理员确认到账才生效。 */
async function checkout(planName) {
  try {
    const response = await fetch("/api/billing/checkout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan: planName, months: 1 }),
    });
    const payload = await response.json();
    if (!response.ok) {
      els.runStatus.textContent = payload.error || "下单失败。";
      return;
    }
    els.runStatus.textContent = `${payload.message} 订单号：${payload.order.id}`;
    await openBilling();
  } catch (error) {
    els.runStatus.textContent = `下单失败：${error.message}`;
  }
}

els.billingToggle.addEventListener("click", openBilling);
els.billingClose.addEventListener("click", () => {
  els.billingPanel.hidden = true;
});

// ------------------------------------------------------------ 我的数据

/**
 * 导出、清空代码目录、注销账号。
 *
 * 这一步的意义不只是"合规"：没有导出能力的话，"删除"就是单向门，
 * 用户不敢按。
 */
function openDataPanel() {
  els.dataPanel.hidden = false;
  els.tokenPanel.hidden = true;
  els.dataConfirm.hidden = true;
  els.dataConfirmInput.value = "";
  els.dataConfirmName.textContent = (state.account && state.account.name) || "";
  loadTrash();
  loadNotifyEmail();
}

/**
 * API 令牌单独一个面板。
 *
 * 一开始塞在「我的数据」里，实测面板内容涨到 800 多像素、超出一屏，
 * 令牌那块被挤到可视区外面（要面板内部滚动才够得到）。拆开之后两边都短。
 */
function openTokenPanel() {
  els.tokenPanel.hidden = false;
  els.dataPanel.hidden = true;
  loadTokens();
}

function exportMyData() {
  // 走浏览器原生下载：服务端带 Content-Disposition: attachment
  window.location.href = "/api/export";
  els.runStatus.textContent = "正在打包下载…";
}

async function purgeMyCode() {
  if (
    !window.confirm(
      "清空代码目录？文件会先放进回收站（保留一段时间，可以恢复），会话记录不受影响。"
    )
  ) {
    return;
  }
  try {
    const payload = await postJSON("/api/account/purge-code", {});
    els.runStatus.textContent = `已清空代码目录（${payload.removed} 个文件进了回收站）。`;
    loadTrash();
  } catch (error) {
    els.runStatus.textContent = error.message;
  }
}

/* ------------------------------------------------------------ 回收站 */

/**
 * 接收通知的邮箱。
 *
 * 只在保存时才校验（服务端校验格式并回中文原因）；留空就是"不接收"，
 * 这一点要写在提示里，否则用户会以为关不掉。
 */
async function loadNotifyEmail() {
  if (!els.notifyEmail) return;
  try {
    const payload = await getJSON("/api/account/email");
    els.notifyEmail.value = payload.email || "";
  } catch (error) {
    els.notifyEmail.placeholder = error.message;
  }
  scrollPanelIntoView();
}

async function saveNotifyEmail() {
  try {
    const payload = await postJSON("/api/account/email", { email: els.notifyEmail.value });
    els.notifyEmail.value = payload.email || "";
    els.runStatus.textContent = payload.email
      ? `已保存：通知会发到 ${payload.email}。`
      : "已清空：不再接收通知。";
  } catch (error) {
    els.runStatus.textContent = error.message;
  }
}

/* ------------------------------------------------------------ API 令牌 */

/**
 * 令牌管理：新建（明文只显示这一次）、复制、吊销。
 *
 * 明文不落盘、不进日志，页面上也只在这一刻出现——所以"复制"要显眼，
 * 并且把"丢了只能重建"说清楚，不然用户会以为还能再看到。
 */
async function loadTokens() {
  if (!els.tokenList) return;
  let payload;
  try {
    payload = await getJSON("/api/tokens");
  } catch (error) {
    els.tokenList.textContent = error.message;
    return;
  }
  els.tokenList.replaceChildren();
  const tokens = payload.tokens || [];
  if (!tokens.length) {
    const empty = document.createElement("p");
    empty.className = "trash-empty";
    empty.textContent = "还没有令牌。";
    els.tokenList.appendChild(empty);
  } else {
    for (const item of tokens) {
      els.tokenList.appendChild(renderToken(item));
    }
  }
  scrollPanelIntoView();
}

function renderToken(item) {
  const row = document.createElement("div");
  row.className = "token-item" + (item.is_active && !item.expired ? "" : " is-off");

  const title = document.createElement("span");
  title.textContent = item.name;
  const meta = document.createElement("span");
  meta.className = "trash-when";
  const state = !item.is_active ? "已吊销" : item.expired ? "已过期" : "有效";
  const used = item.last_used_at ? formatTime(item.last_used_at) : "从未";
  meta.textContent = `${item.prefix}… · ${state} · 最近使用 ${used}`;

  row.append(title, meta);
  if (item.is_active) {
    const revoke = document.createElement("button");
    revoke.type = "button";
    revoke.className = "data-button is-danger";
    revoke.textContent = "吊销";
    revoke.addEventListener("click", () => revokeToken(item));
    row.appendChild(revoke);
  }
  return row;
}

async function createToken() {
  const name = (els.tokenName.value || "").trim();
  try {
    const payload = await postJSON("/api/tokens/create", { name });
    els.tokenName.value = "";
    showFreshToken(payload.token);
    els.runStatus.textContent = "令牌已创建，请立刻复制保存（只显示这一次）。";
  } catch (error) {
    els.runStatus.textContent = error.message;
    return;
  }
  loadTokens();
}

function showFreshToken(plaintext) {
  els.tokenFresh.replaceChildren();
  els.tokenFresh.hidden = false;

  const code = document.createElement("code");
  code.textContent = plaintext;
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "data-button";
  copy.textContent = "复制";
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(plaintext);
      els.runStatus.textContent = "已复制到剪贴板。";
    } catch (_error) {
      // 剪贴板权限被拒（或非 HTTPS）时退化成手动选中——总比什么都不说好
      els.runStatus.textContent = "复制失败，请手动选中上面那串令牌。";
    }
  });
  els.tokenFresh.append(code, copy);
}

async function revokeToken(item) {
  if (!window.confirm(`吊销令牌「${item.name}」？用它调用的脚本会立刻失效。`)) return;
  try {
    await postJSON("/api/tokens/revoke", { id: item.id });
  } catch (error) {
    els.runStatus.textContent = error.message;
    return;
  }
  els.runStatus.textContent = "已吊销。";
  loadTokens();
}


/**
 * 回收站：删错的东西在这里，能恢复，也能彻底删掉。
 *
 * 页面只给两个按钮（恢复 / 彻底删除），不解释内部目录结构——用户不需要知道
 * 文件躺在哪，只需要知道"还能不能回来"。
 */
async function loadTrash() {
  let payload;
  try {
    payload = await getJSON("/api/trash");
  } catch (error) {
    els.trashHint.textContent = error.message;
    return;
  }
  const items = [...(payload.sessions || []), ...(payload.code || [])];
  const totalFiles = items.reduce((sum, item) => sum + (item.files || 0), 0);
  els.trashHint.textContent = items.length ? `${items.length} 项 · ${totalFiles} 个文件` : "空的";
  els.trashList.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "trash-empty";
    empty.textContent = "还没有删掉的东西。";
    els.trashList.appendChild(empty);
  } else {
    for (const item of items) {
      els.trashList.appendChild(renderTrashItem(item));
    }
  }
  // 面板在侧栏底部，而且高度随回收站内容变化；填完之后再把下沿带进视野，
  // 否则"滚到位"是照填之前那个矮面板算的，填完就又被顶出屏幕了。
  scrollPanelIntoView();
}

/**
 * 把「我的数据」面板滚进视野。
 *
 * 用显式滚动而不是 `scrollIntoView`：实测后者在多级滚动容器里没把侧栏带到位
 * （面板下沿还在视口外一百多像素）。面板本身是个滚动容器，所以这里滚的是它
 * 的父级侧栏——滚到最底，下沿自然贴住视口底部。
 */
function scrollPanelIntoView() {
  // 现在有两个面板（我的数据 / API 令牌），滚当前打开的那一个
  const panel = [els.dataPanel, els.tokenPanel].find((item) => item && !item.hidden);
  if (!panel) return;
  const sidebar = panel.closest(".sidebar");
  if (sidebar) sidebar.scrollTop = sidebar.scrollHeight;
}

function renderTrashItem(item) {
  const row = document.createElement("div");
  row.className = "trash-item";

  const label = document.createElement("div");
  label.className = "trash-name";
  const kind = item.kind === "code" ? "代码目录" : "会话";
  label.textContent = `${item.name}（${kind}）`;
  const when = document.createElement("span");
  when.className = "trash-when";
  when.textContent = `删除于 ${formatTime(item.deleted_at)}`;
  label.appendChild(when);

  const actions = document.createElement("div");
  actions.className = "trash-actions";
  const restore = document.createElement("button");
  restore.type = "button";
  restore.className = "data-button";
  restore.textContent = "恢复";
  restore.addEventListener("click", () => restoreTrashItem(item));
  const purge = document.createElement("button");
  purge.type = "button";
  purge.className = "data-button is-danger";
  purge.textContent = "彻底删除";
  purge.addEventListener("click", () => purgeTrashItem(item));
  actions.append(restore, purge);

  row.append(label, actions);
  return row;
}

function formatTime(value) {
  if (!value) return "未知时间";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString();
}

async function restoreTrashItem(item) {
  try {
    await postJSON("/api/trash/restore", { kind: item.kind, entry: item.entry });
    els.runStatus.textContent =
      item.kind === "code" ? "代码目录已恢复。" : `会话「${item.name}」已恢复。`;
  } catch (error) {
    els.runStatus.textContent = error.message;
    return;
  }
  await loadTrash();
  if (item.kind === "session") await loadSessions();
}

async function purgeTrashItem(item) {
  if (!window.confirm(`彻底删除「${item.name}」？这一步没有撤销。`)) return;
  try {
    await postJSON("/api/trash/purge", { kind: item.kind, entry: item.entry });
  } catch (error) {
    els.runStatus.textContent = error.message;
    return;
  }
  els.runStatus.textContent = "已彻底删除。";
  loadTrash();
}

async function emptyMyTrash() {
  if (!window.confirm("清空回收站？里面的东西就再也找不回来了。")) return;
  try {
    await postJSON("/api/trash/empty", {});
  } catch (error) {
    els.runStatus.textContent = error.message;
    return;
  }
  els.runStatus.textContent = "回收站已清空。";
  loadTrash();
}

async function confirmDeleteAccount() {
  try {
    await postJSON("/api/account/delete", { confirm: els.dataConfirmInput.value });
    // 账号已经没了，留在这个页面没有意义
    window.location.href = "/";
  } catch (error) {
    // 用户名打错时把服务端的原因显示出来（"请输入你自己的用户名…"）
    els.runStatus.textContent = error.message;
  }
}

els.dataToggle.addEventListener("click", openDataPanel);
els.dataClose.addEventListener("click", () => {
  els.dataPanel.hidden = true;
});
els.dataExport.addEventListener("click", exportMyData);
els.dataPurge.addEventListener("click", purgeMyCode);
els.trashEmpty.addEventListener("click", emptyMyTrash);
els.notifySave.addEventListener("click", saveNotifyEmail);
els.tokenCreate.addEventListener("click", createToken);
els.tokenToggle.addEventListener("click", openTokenPanel);
els.tokenClose.addEventListener("click", () => {
  els.tokenPanel.hidden = true;
});
els.dataDelete.addEventListener("click", () => {
  els.dataConfirm.hidden = false;
  els.dataConfirmInput.focus();
});
els.dataConfirmCancel.addEventListener("click", () => {
  els.dataConfirm.hidden = true;
  els.dataConfirmInput.value = "";
});
els.dataConfirmOk.addEventListener("click", confirmDeleteAccount);

// ------------------------------------------------------ 侧栏布局：折叠与调宽

// ------------------------------------------------------------ 上传文件

const MAX_UPLOAD_BYTES = 2 * 1024 * 1024;

/**
 * 把文件传进自己的工作目录（每个账号一个目录，互相看不到）。
 *
 * 走原始 body 而不是 FormData：要的只是文本，不走 multipart 更简单，
 * 服务端也少一层解析。
 */
async function uploadFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  els.runStatus.textContent = `正在上传 ${files.length} 个文件…`;

  const failed = [];
  for (const file of files) {
    if (file.size > MAX_UPLOAD_BYTES) {
      failed.push(`${file.name}（超过 2MB）`);
      continue;
    }
    try {
      const response = await fetch(`/api/upload?path=${encodeURIComponent(file.name)}`, {
        method: "POST",
        headers: { "Content-Type": "text/plain; charset=utf-8" },
        body: file,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        failed.push(`${file.name}（${payload.error || response.status}）`);
        continue;
      }
      addUploadChip(payload.path, payload.bytes);
    } catch (error) {
      failed.push(`${file.name}（${error.message}）`);
    }
  }

  els.runStatus.textContent = failed.length
    ? `没能上传：${failed.join("；")}`
    : "上传好了，直接说要对它做什么就行。";
}

/** 上传成功后挂一个小标签，点开就能看内容。 */
function addUploadChip(path, bytes) {
  els.uploadChips.hidden = false;
  const existing = Array.from(els.uploadChips.querySelectorAll(".upload-chip")).find(
    (chip) => chip.dataset.path === path
  );
  if (existing) existing.remove(); // 覆盖上传时只留一个

  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = "upload-chip";
  chip.dataset.path = path;
  chip.textContent = `${path} · ${formatBytes(bytes)}`;
  chip.title = "点开看文件内容";
  chip.addEventListener("click", () => openViewer(path, bytes));
  els.uploadChips.appendChild(chip);
}

function formatBytes(bytes) {
  const value = Number(bytes || 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

/** 拖到输入区就能上传。 */
function wireDropZone() {
  const zone = els.composer;
  ["dragenter", "dragover"].forEach((name) =>
    zone.addEventListener(name, (event) => {
      event.preventDefault();
      zone.classList.add("is-dropping");
    })
  );
  ["dragleave", "dragend"].forEach((name) =>
    zone.addEventListener(name, () => zone.classList.remove("is-dropping"))
  );
  zone.addEventListener("drop", (event) => {
    event.preventDefault();
    zone.classList.remove("is-dropping");
    uploadFiles(event.dataTransfer && event.dataTransfer.files);
  });
}

els.uploadButton.addEventListener("click", () => els.uploadInput.click());
els.uploadInput.addEventListener("change", () => {
  uploadFiles(els.uploadInput.files);
  els.uploadInput.value = ""; // 同一个文件再传一次也要触发 change
});
wireDropZone();

function clampSidebarWidth(px) {
  return Math.min(MAX_SIDEBAR_WIDTH, Math.max(MIN_SIDEBAR_WIDTH, px));
}

function setSidebarWidth(px) {
  document.documentElement.style.setProperty("--sidebar-width", `${Math.round(px)}px`);
}

/** 收起「智能体」列表，把空间让给下面的套餐面板。 */
function setAgentsCollapsed(collapsed) {
  state.agentsCollapsed = collapsed;
  els.agentList.hidden = collapsed;
  els.agentToggle.classList.toggle("is-collapsed", collapsed);
  els.agentToggle.setAttribute("aria-expanded", String(!collapsed));
  // 收起后看不到列表了，把当前选中的智能体写在标题上，免得不知道用的是哪个
  els.agentToggleLabel.textContent = collapsed ? `智能体（${selectedAgent()}）` : "智能体";
}

function toggleAgents() {
  const collapsed = !state.agentsCollapsed;
  setAgentsCollapsed(collapsed);
  localStorage.setItem(AGENTS_COLLAPSED_KEY, collapsed ? "1" : "0");
}

/** 拖右边缘调侧栏宽度。 */
function startSidebarResize(event) {
  if (event.button !== undefined && event.button !== 0) return;
  event.preventDefault();
  const startX = event.clientX;
  const startWidth = els.sidebar.getBoundingClientRect().width;
  document.body.classList.add("is-resizing");

  const onMove = (moveEvent) => {
    setSidebarWidth(clampSidebarWidth(startWidth + moveEvent.clientX - startX));
  };
  const onDone = () => {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onDone);
    window.removeEventListener("pointercancel", onDone);
    document.body.classList.remove("is-resizing");
    const finalWidth = els.sidebar.getBoundingClientRect().width;
    localStorage.setItem(SIDEBAR_WIDTH_KEY, String(Math.round(finalWidth)));
  };

  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", onDone);
  window.addEventListener("pointercancel", onDone);
}

/** 键盘也能调：方向键每次 16px（无障碍要求，别只给鼠标一条路）。 */
function resizeSidebarWithKeyboard(event) {
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  event.preventDefault();
  const current = els.sidebar.getBoundingClientRect().width;
  const next = clampSidebarWidth(current + (event.key === "ArrowRight" ? 16 : -16));
  setSidebarWidth(next);
  localStorage.setItem(SIDEBAR_WIDTH_KEY, String(Math.round(next)));
}

/** 恢复上次的折叠状态与侧栏宽度。 */
function restoreLayout() {
  const storedWidth = Number(localStorage.getItem(SIDEBAR_WIDTH_KEY));
  if (Number.isFinite(storedWidth) && storedWidth >= MIN_SIDEBAR_WIDTH) {
    setSidebarWidth(clampSidebarWidth(storedWidth));
  }
  setAgentsCollapsed(localStorage.getItem(AGENTS_COLLAPSED_KEY) === "1");
}

els.agentToggle.addEventListener("click", toggleAgents);
els.sidebarResizer.addEventListener("pointerdown", startSidebarResize);
els.sidebarResizer.addEventListener("keydown", resizeSidebarWithKeyboard);
// 收起状态下换了智能体，标题上显示的名字也要跟着变
document.addEventListener("change", (event) => {
  if (event.target && event.target.name === "agent" && state.agentsCollapsed) {
    els.agentToggleLabel.textContent = `智能体（${selectedAgent()}）`;
  }
});

boot();
