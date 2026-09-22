/* AgentCode 页面逻辑：查询智能体 → 提交任务 → 解析 SSE 逐步渲染轨迹 */

const PREFERRED_AGENT = "react";

const els = {
  form: document.getElementById("run-form"),
  agentList: document.getElementById("agent-list"),
  task: document.getElementById("task"),
  runButton: document.getElementById("run-button"),
  runStatus: document.getElementById("run-status"),
  configList: document.getElementById("config-list"),
  empty: document.getElementById("empty"),
  steps: document.getElementById("steps"),
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
};

const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

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
  ];
  els.configList.innerHTML = rows
    .map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`)
    .join("");

  if (!config.ready) {
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = `缺少 ${config.missing_keys.join("、")}，用真实模型前请先补齐 .env；离线演示不受影响。`;
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

  const data = new FormData(els.form);
  const payload = {
    agent: data.get("agent"),
    llm: data.get("llm"),
    task,
  };

  clearStream();
  setRunning(true);
  const startedAt = performance.now();

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
    setRunning(false);
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
  if (type === "status") {
    els.runStatus.textContent = payload.message || "";
  } else if (type === "step") {
    appendStep(payload);
  } else if (type === "answer") {
    showAnswer(payload);
  } else if (type === "error") {
    showFailure(payload.message || "运行失败。");
  }
}

/* ---------------------------------------------------------------- 渲染 */

function field(label, value, kind) {
  return `
    <div class="field${kind ? ` field-${kind}` : ""}">
      <span class="field-label">${escapeHtml(label)}</span>
      <span class="field-value">${escapeHtml(value)}</span>
    </div>`;
}

function appendStep(step) {
  els.empty.hidden = true;
  els.steps.hidden = false;

  const rows = [];
  if (step.thought) rows.push(field("思考", step.thought));
  if (step.action) rows.push(field("行动", step.action, "action"));
  if (step.observation) rows.push(field("观察", step.observation));
  if (step.error) rows.push(field("异常", step.error, "error"));

  const meta = step.tool
    ? `<p class="step-meta">调用了工具 ${escapeHtml(step.tool)}，用时 ${Math.round(
        step.duration_ms
      )} 毫秒</p>`
    : "";

  const item = document.createElement("li");
  item.className = "step";
  item.innerHTML = `
    <span class="step-index">${String(step.index).padStart(2, "0")}</span>
    <div class="step-body">${rows.join("")}${meta}</div>`;
  els.steps.appendChild(item);
  item.scrollIntoView({ block: "nearest", behavior: reduceMotion ? "auto" : "smooth" });
}

function showAnswer(result) {
  state.lastResult = result;
  els.answer.hidden = false;
  els.exportButton.disabled = false;
  els.answerText.textContent = result.answer || "（没有给出答案）";
  const usage = result.usage || {};
  els.answerMeta.textContent = [
    `${result.agent} 智能体`,
    `模型调用 ${usage.calls || 0} 次`,
    `约 ${usage.total_tokens || 0} token${usage.estimated ? "（估算）" : ""}`,
    `耗时 ${formatSeconds(result.duration_ms)}`,
  ].join("，");
  els.runStatus.textContent = result.success ? "已完成" : "已结束，但没有得出结论";
}

function showFailure(message) {
  els.empty.hidden = true;
  els.failure.hidden = false;
  els.failureText.textContent = `${message} 可以换成离线演示再试，或检查左侧的配置。`;
  els.runStatus.textContent = "失败";
}

function clearStream() {
  els.steps.innerHTML = "";
  els.steps.hidden = true;
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

function exportTrace() {
  if (!state.lastResult) return;
  const blob = new Blob([JSON.stringify(state.lastResult, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${state.lastResult.agent}-trace.json`;
  link.click();
  URL.revokeObjectURL(url);
}

els.form.addEventListener("submit", submitRun);
els.exportButton.addEventListener("click", exportTrace);
els.clearButton.addEventListener("click", clearStream);

loadMeta();
