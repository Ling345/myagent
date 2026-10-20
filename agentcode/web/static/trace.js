/* 运行追踪页：左边最近运行，右边把一次运行的事件流画成时间线。
   数据全部来自已有接口：GET /api/runs 与 GET /api/run/stream（支持从 0 重放）。 */

"use strict";

const trEl = {
  runs: document.getElementById("tr-runs"),
  refresh: document.getElementById("tr-refresh"),
  title: document.getElementById("tr-title"),
  meta: document.getElementById("tr-meta"),
  status: document.getElementById("tr-status"),
  steps: document.getElementById("tr-steps"),
  answer: document.getElementById("tr-answer"),
};

const trState = { source: null, runId: "", steps: 0 };

function trText(node, value) {
  node.textContent = value;
}

async function trApi(path) {
  const response = await fetch(path);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || `请求失败（${response.status}）`);
  }
  return data;
}

function closeStream() {
  if (trState.source) {
    trState.source.close();
    trState.source = null;
  }
}

/* ------------------------------------------------------------ 运行列表 */

async function loadRuns() {
  const data = await trApi("/api/runs");
  const runs = data.runs || [];
  trEl.runs.replaceChildren(
    ...runs.map((run) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "tr-run";
      button.dataset.run = run.id;
      const mark = { done: "完成", running: "跑着" }[run.status] || run.status;
      const task = String(run.task || "").replace(/\s+/g, " ").slice(0, 42);
      button.innerHTML = "<strong></strong><span></span>";
      button.querySelector("strong").textContent = `${run.agent} · ${mark}`;
      button.querySelector("span").textContent =
        `${run.id} · ${run.event_count} 事件 · ${Math.round(run.duration_ms)} ms`;
      button.title = task;
      button.addEventListener("click", () => openRun(run.id, button));
      return button;
    })
  );
  if (!runs.length) {
    const empty = document.createElement("p");
    empty.className = "tr-sub";
    empty.textContent = "还没有运行。回到对话页发一个任务，再刷新这里。";
    trEl.runs.append(empty);
  }
  return runs;
}

/* ------------------------------------------------------------ 时间线 */

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderStep(step) {
  const item = element("li", "tr-step");
  item.dataset.error = step.error ? "1" : "0";

  const head = element("div", "tr-step-head");
  head.append(element("span", "tr-index", `#${step.index}`));
  const action = String(step.action || "").trim();
  head.append(element("span", "tr-action", action || "（无行动）"));
  head.append(element("span", "tr-ms", `${Math.round(step.duration_ms || 0)} ms`));
  item.append(head);

  if (step.thought) {
    const details = element("details", "tr-fold");
    details.open = true;
    details.append(element("summary", "", "思考"));
    details.append(element("div", "tr-text", step.thought));
    item.append(details);
  }
  if (step.tool) {
    const tool = element("div", "tr-tool");
    tool.append(element("span", "tr-tool-name", `工具 ${step.tool}`));
    if (step.tool_input) {
      tool.append(element("code", "tr-tool-input", step.tool_input));
    }
    item.append(tool);
  }
  if (step.observation) {
    const details = element("details", "tr-fold");
    details.append(
      element("summary", "", `观察（${String(step.observation).length} 字）`)
    );
    details.append(element("pre", "tr-observation", step.observation));
    item.append(details);
  }
  if (step.answer) {
    item.append(element("div", "tr-final", `最终答案：${step.answer}`));
  }
  if (step.error) {
    item.append(element("div", "tr-error", `错误：${step.error}`));
  }
  return item;
}

function showAnswer(data) {
  trEl.answer.hidden = false;
  trEl.answer.replaceChildren();
  const usage = data.usage || {};
  const ok = data.success !== false;
  trEl.answer.append(
    element("strong", ok ? "tr-answer-ok" : "tr-answer-bad", ok ? "完成" : "失败")
  );
  trEl.answer.append(element("pre", "tr-answer-text", data.answer || data.error || ""));
  trEl.answer.append(
    element(
      "p",
      "wf-sub",
      `用时 ${Math.round(data.duration_ms || 0)} ms · token ${usage.total_tokens || 0}` +
        `（输入 ${usage.prompt_tokens || 0} / 输出 ${usage.completion_tokens || 0}）`
    )
  );
}

function openRun(runId, button) {
  closeStream();
  trState.runId = runId;
  trState.steps = 0;
  trEl.steps.replaceChildren();
  trEl.answer.hidden = true;
  trEl.answer.replaceChildren();
  trText(trEl.title, `运行 ${runId}`);
  trText(trEl.meta, "正在打开事件流…");
  trText(trEl.status, "");
  for (const item of trEl.runs.querySelectorAll(".tr-run")) {
    item.classList.toggle("is-active", item === button);
  }

  const source = new EventSource(
    `/api/run/stream?run_id=${encodeURIComponent(runId)}&from=0`
  );
  trState.source = source;

  source.addEventListener("run", (message) => {
    const data = JSON.parse(message.data);
    trText(
      trEl.meta,
      `${data.agent} · ${data.status === "done" ? "已完成" : "跑着"} · ${data.event_count} 个事件`
    );
    trText(trEl.title, String(data.task || `运行 ${runId}`));
  });

  source.addEventListener("status", (message) => {
    const data = JSON.parse(message.data);
    trText(trEl.status, data.message || "");
  });

  source.addEventListener("truncated", (message) => {
    const data = JSON.parse(message.data);
    trText(trEl.status, `（前面 ${data.skipped} 条事件已滚动丢弃）`);
  });

  source.addEventListener("step", (message) => {
    trState.steps += 1;
    trEl.steps.append(renderStep(JSON.parse(message.data)));
  });

  source.addEventListener("answer", (message) => {
    showAnswer(JSON.parse(message.data));
    trText(trEl.status, "");
    closeStream();
    loadRuns().catch(() => {});
  });

  source.addEventListener("error", (message) => {
    // 运行本身报错（不是 SSE 断线）：event 里有 data 才是业务错误
    if (message.data) {
      try {
        const data = JSON.parse(message.data);
        trText(trEl.status, `运行失败：${data.message || "原因不明"}`);
      } catch (err) {
        trText(trEl.status, "运行失败（原因不明）");
      }
    }
  });

  source.onerror = () => {
    // 服务端在运行结束时关闭连接；此时 answer 已经处理过了
    if (trState.source === source && !trEl.answer.hidden) {
      closeStream();
      return;
    }
    if (trState.source === source) {
      closeStream();
      trText(trEl.status, "事件流断了：运行可能已经结束很久（记录被回收），刷新列表看看。");
    }
  };
}

/* ------------------------------------------------------------ 启动 */

trEl.refresh.addEventListener("click", () => {
  loadRuns().catch((error) => trText(trEl.status, `刷新失败：${error.message}`));
});

loadRuns()
  .then((runs) => {
    const wanted = new URLSearchParams(location.search).get("run_id");
    if (!wanted) return;
    const button = trEl.runs.querySelector(`.tr-run[data-run="${CSS.escape(wanted)}"]`);
    if (button) {
      openRun(wanted, button);
    } else if (runs.length) {
      trText(trEl.status, `没找到运行 ${wanted}（可能已经超过 30 分钟被回收）。`);
    }
  })
  .catch((error) => {
    if (String(error.message).includes("登录")) {
      trEl.status.innerHTML = '请先登录：<a href="/">去登录页</a>';
      return;
    }
    trText(trEl.status, `加载失败：${error.message}`);
  });
