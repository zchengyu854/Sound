"use strict";

const $ = id => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";
const STATUS = {
  running: "训练中",
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
  waiting: "等待训练"
};
const state = { runs: [], selected: "", detail: null, timer: 0, loading: false };

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function svgNode(tag, attributes = {}) {
  const element = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
  return element;
}

async function api(path) {
  const response = await fetch(path, {cache: "no-store"});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
  return data;
}

function number(value, digits = 6) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  const absolute = Math.abs(value);
  if (absolute !== 0 && (absolute < 0.001 || absolute >= 10000)) return value.toExponential(2);
  return Number(value.toPrecision(digits)).toString();
}

function duration(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "—";
  const total = Math.round(value);
  if (total < 60) return `${total} 秒`;
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return `${minutes} 分 ${String(total % 60).padStart(2, "0")} 秒`;
  return `${Math.floor(minutes / 60)} 小时 ${String(minutes % 60).padStart(2, "0")} 分`;
}

function time(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", {hour12: false});
}

function latestValidation(progress) {
  const trend = Array.isArray(progress?.validation_trend) ? progress.validation_trend : [];
  const last = trend[trend.length - 1];
  return last && typeof last.mean === "number" ? last : null;
}

function schedule() {
  clearTimeout(state.timer);
  const running = state.runs.some(run => run.status === "running");
  const delay = document.hidden ? 15000 : running ? 2000 : 8000;
  state.timer = setTimeout(refresh, delay);
}

async function refresh() {
  if (state.loading) return;
  state.loading = true;
  try {
    const data = await api("/api/training/runs");
    state.runs = Array.isArray(data.runs) ? data.runs : [];
    $("connection-dot").className = "dot ready";
    $("connection-text").textContent = state.runs.length ? `已连接 · ${state.runs.length} 个任务` : "已连接 · 暂无任务";
    renderRuns();
    const selected = state.runs.find(run => run.id === state.selected);
    const active = selected || state.runs.find(run => run.status === "running") || state.runs[0];
    if (active) {
      await select(active.id, false);
    } else {
      state.detail = null;
      renderEmpty();
    }
  } catch (error) {
    $("connection-dot").className = "dot failed";
    $("connection-text").textContent = error.message;
    if (!state.detail) renderEmpty(error.message);
  } finally {
    state.loading = false;
    schedule();
  }
}

function renderRuns() {
  const list = $("runs");
  list.replaceChildren();
  $("runs-count").textContent = state.runs.length ? `${state.runs.length} 个任务，自动刷新` : "暂无任务";
  for (const run of state.runs) {
    const button = node("button", "run-item");
    button.type = "button";
    button.dataset.id = run.id;
    button.setAttribute("aria-current", String(run.id === state.selected));
    const name = node("strong", "", run.id);
    const status = node("span", "run-state", STATUS[run.status] || run.status);
    status.dataset.status = run.status;
    const detail = run.total_steps
      ? `第 ${run.step || 0} / ${run.total_steps} 步${typeof run.loss === "number" ? ` · loss ${number(run.loss, 5)}` : ""}`
      : (run.result ? "结果包已生成" : "等待训练");
    button.append(name, status, node("span", "", detail));
    button.addEventListener("click", () => select(run.id, true));
    list.append(button);
  }
}

async function select(id, updateHash = true) {
  if (!id) return;
  state.selected = id;
  if (updateHash) history.replaceState(null, "", `#${encodeURIComponent(id)}`);
  try {
    state.detail = await api(`/api/training/runs/${encodeURIComponent(id)}`);
    renderDetail();
  } catch (error) {
    $("connection-dot").className = "dot failed";
    $("connection-text").textContent = error.message;
  }
  renderRuns();
}

function renderEmpty(message) {
  $("empty").hidden = false;
  $("workspace").hidden = true;
  $("empty-command").textContent = [
    "mkdir -p training-runs/demo",
    "cp /path/to/task.zip training-runs/demo/task.zip",
    "python ubuntu_training.py training-runs/demo/task.zip training-runs/demo/result.zip",
    "python training_dashboard.py"
  ].join("\n");
  const title = $("empty").querySelector("h2");
  const copy = $("empty").querySelector("p:not(.eyebrow)");
  if (message) {
    title.textContent = "训练服务暂时不可用";
    copy.textContent = message;
  } else {
    title.textContent = "还没有可显示的训练";
    copy.textContent = "把 Mac 导出的 task.zip 放进一个任务目录，再用 CLI 启动训练。页面会自动出现进度，不需要刷新。";
  }
}

function renderDetail() {
  const detail = state.detail;
  if (!detail) return;
  const progress = detail.progress || {};
  const stepLog = Array.isArray(progress.step_log) ? progress.step_log : [];
  const trend = Array.isArray(progress.validation_trend) ? progress.validation_trend : [];
  const validation = latestValidation(progress);
  const total = Number(progress.total_steps) || 0;
  const step = Number(progress.step) || 0;

  $("empty").hidden = true;
  $("workspace").hidden = false;
  $("run-title").textContent = detail.id;
  $("run-subtitle").textContent = `${detail.task ? "task.zip 已就位" : "目录中没有 task.zip"} · 更新 ${time(progress.updated_at || detail.updated_at)}`;
  const tag = $("status-tag");
  tag.textContent = STATUS[detail.status] || detail.status;
  tag.dataset.status = detail.status;
  $("run-progress").max = Math.max(total, 1);
  $("run-progress").value = Math.min(step, Math.max(total, 1));
  $("step-text").textContent = total ? `${step} / ${total} 步` : (detail.result ? "结果包已生成" : "等待训练进度");

  $("stat-loss").textContent = number(progress.loss);
  $("stat-validation").textContent = number(validation?.mean);
  $("stat-gradient").textContent = number(progress.gradient_norm);
  $("stat-lr").textContent = number(progress.learning_rate);
  $("stat-epoch").textContent = typeof progress.epoch === "number" ? String(progress.epoch) : "—";
  $("stat-elapsed").textContent = duration(progress.elapsed_seconds);
  $("stat-eta").textContent = duration(progress.eta_seconds);
  $("stat-memory").textContent = typeof progress.peak_memory_gib === "number" ? `${number(progress.peak_memory_gib, 4)} GiB` : "—";

  drawChart("loss-chart", stepLog.map(item => ({x: item.step, y: item.loss})),
            trend.map(item => ({x: item.step, y: item.mean})), total);
  drawChart("gradient-chart", stepLog.map(item => ({x: item.step, y: item.gradient_norm})), [], total);

  const table = $("validation-table");
  table.replaceChildren();
  if (!trend.length) {
    $("validation-note").textContent = progress.status === "running" ? "等待第一个检查点" : "没有留出验证片段";
    table.append(node("p", "muted", "该任务没有独立验证片段，或还没有到保存检查点的步数。"));
  } else {
    $("validation-note").textContent = `${trend.length} 次记录`;
    const header = node("div", "validation-row header");
    header.append(node("span", "", "STEP"), node("span", "", "MEAN LOSS"), node("span", "", "逐个片段"));
    table.append(header);
    for (const entry of trend.slice().reverse()) {
      const row = node("div", "validation-row");
      const losses = Array.isArray(entry.losses) ? entry.losses.map(value => number(value, 5)).join(" · ") : "—";
      row.append(node("strong", "", String(entry.step)), node("strong", "", number(entry.mean)), node("p", "", losses));
      table.append(row);
    }
  }

  const log = $("log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
  log.textContent = detail.log || "暂无日志。";
  if (atBottom) log.scrollTop = log.scrollHeight;

  const link = $("download-result");
  const resultUrl = detail.links?.result;
  link.hidden = !resultUrl;
  if (resultUrl) link.href = resultUrl;

  const notes = [];
  if (progress.error) notes.push(`错误：${progress.error}`);
  if (progress.best_checkpoint) notes.push(`最佳检查点：第 ${progress.best_checkpoint.step} 步，验证损失 ${number(progress.best_checkpoint.validation_loss)}`);
  if (progress.final_checkpoint_step) notes.push(`最终检查点：第 ${progress.final_checkpoint_step} 步`);
  if (progress.adapter_sha256) notes.push(`adapter SHA-256：${progress.adapter_sha256}`);
  $("result-note").textContent = notes.join(" · ");
}

function drawChart(id, primary, secondary, total) {
  const svg = $(id);
  svg.replaceChildren();
  const width = 640, height = 220;
  const margin = {top: 14, right: 14, bottom: 26, left: 52};
  const values = [...primary, ...secondary].map(point => point.y).filter(value => typeof value === "number" && Number.isFinite(value));
  if (!values.length) {
    const note = svgNode("text", {x: width / 2, y: height / 2, "text-anchor": "middle", class: "empty-note"});
    note.textContent = "等待训练数据…";
    svg.append(note);
    return;
  }
  const maxX = Math.max(total || 1, ...primary.map(point => point.x), ...secondary.map(point => point.x), 1);
  const maxY = Math.max(...values, 0.000001) * 1.12;
  const x = value => margin.left + (value / maxX) * (width - margin.left - margin.right);
  const y = value => height - margin.bottom - (value / maxY) * (height - margin.top - margin.bottom);

  for (let index = 0; index <= 4; index += 1) {
    const value = maxY * index / 4;
    const lineY = y(value);
    svg.append(svgNode("line", {x1: margin.left, x2: width - margin.right, y1: lineY, y2: lineY, class: "grid-line"}));
    const label = svgNode("text", {x: margin.left - 8, y: lineY + 3, "text-anchor": "end"});
    label.textContent = number(value, 3);
    svg.append(label);
  }
  for (let index = 0; index <= 4; index += 1) {
    const value = maxX * index / 4;
    const lineX = x(value);
    const label = svgNode("text", {x: lineX, y: height - 8, "text-anchor": "middle"});
    label.textContent = Math.round(value);
    svg.append(label);
  }
  svg.append(svgNode("line", {x1: margin.left, x2: width - margin.right, y1: height - margin.bottom, y2: height - margin.bottom, class: "axis-line"}));
  svg.append(svgNode("line", {x1: margin.left, x2: margin.left, y1: margin.top, y2: height - margin.bottom, class: "axis-line"}));

  if (primary.length > 1) {
    svg.append(svgNode("polyline", {
      points: primary.map(point => `${x(point.x)},${y(point.y)}`).join(" "),
      class: "primary-line"
    }));
  } else if (primary.length === 1) {
    svg.append(svgNode("circle", {cx: x(primary[0].x), cy: y(primary[0].y), r: 3, class: "point"}));
  }
  if (secondary.length > 1) {
    svg.append(svgNode("polyline", {
      points: secondary.map(point => `${x(point.x)},${y(point.y)}`).join(" "),
      class: "validation-line"
    }));
  }
  for (const point of secondary) {
    svg.append(svgNode("circle", {cx: x(point.x), cy: y(point.y), r: 3, class: "point"}));
  }
}

$("refresh").addEventListener("click", refresh);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh();
});
state.selected = decodeURIComponent(location.hash.slice(1) || "");
refresh();
