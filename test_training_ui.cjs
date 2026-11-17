const assert = require("node:assert/strict");
const fs = require("node:fs");
const {randomUUID} = require("node:crypto");
const {createWorkbench, formatDuration, errorMessage} = require("./training_web/app.js");
const html = fs.readFileSync("./training_web/index.html", "utf8");

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {}; this.dataset = {};
    this.attributes = {}; this.className = ""; this.hidden = false; this.disabled = false;
    this.value = ""; this.open = false; this._text = "";
    this.classList = {
      add: name => { this.className = [...new Set([...this.className.split(" "), name])].join(" "); },
      remove: name => { this.className = this.className.split(" ").filter(item => item !== name).join(" "); }
    };
  }
  set innerHTML(_) { throw new Error("Untrusted HTML must never be inserted"); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ""; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  async emit(name, options = {}) {
    for (const fn of this.listeners[name] || []) await fn({target: this, preventDefault() {}, ...options});
  }
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() { this.focused = true; }
  select() { this.selected = true; }
  reset() {}
  reportValidity() { return true; }
}

function walk(element) { return [element, ...element.children.flatMap(walk)]; }
function fakeDocument() {
  const elements = new Map([...html.matchAll(/<([a-z-]+)[^>]*\bid="([^"]+)"[^>]*>/g)].map(match => [match[2], new Element(match[1])]));
  return {hidden: false, elements,
    getElementById(id) { assert(elements.has(id), `Missing real HTML element ${id}`); return elements.get(id); },
    createElement: tag => new Element(tag),
    createElementNS: (namespace, tag) => { assert.equal(namespace, "http://www.w3.org/2000/svg"); return new Element(tag); },
    querySelectorAll(selector) {
      if (selector === ".dataset-item") return walk(elements.get("datasets")).filter(item => item.className === "dataset-item");
      if (selector === ".segment button, .segment textarea") return walk(elements.get("segments")).filter(item => ["BUTTON", "TEXTAREA"].includes(item.tagName));
      throw new Error(`Unexpected selector ${selector}`);
    }
  };
}

const storage = () => {
  const values = new Map();
  return {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)};
};
class FormData {
  constructor(form) { this.values = Object.entries(form?.fields || {}); }
  append(...value) { this.values.push(value); }
  [Symbol.iterator]() { return this.values[Symbol.iterator](); }
}
const config = {steps: 20, rank: 4, learning_rate: .0001, accumulation_steps: 1, checkpoint_every: 1, seed: 42};

function fixture(sharedSession = storage()) {
  const id = randomUUID();
  const item = {id, name: "角色 <script>alert(1)</script>", approved_duration: 0, speaker_confirmed: true, version: 1};
  const segment = {id: randomUUID(), dataset_id: id, source_id: randomUUID(), start: 0, end: 4,
    transcript: "<img src=x onerror=alert(1)>", review_status: "pending", quality_hints: ["forced_split_check_words"]};
  const data = {datasets: [item], segments: [segment], jobs: [], posts: [], keys: new Map(), loseTrainResponse: false,
    datasetMetrics: new Map(), jobMetrics: new Map(), history: new Map(),
    cap: {ffmpeg: true, ffprobe: true, compatible: true, available: true, active_job: null, base_model: "test-base", hardware: {chip: "Test Mac", memory_gib: 16}}};
  const response = (value, status = 200) => ({ok: status < 400, status, json: async () => structuredClone(value)});
  const windowListeners = {};
  const win = {localStorage: storage(), sessionStorage: sharedSession, crypto: {randomUUID}, FormData,
    navigator: {clipboard: {writeText: async () => {}}}, confirm: () => true,
    addEventListener: (name, fn) => { windowListeners[name] = fn; }, setTimeout: () => 1, clearTimeout() {},
    async fetch(url, options = {}) {
      const method = options.method || "GET";
      if (method !== "GET") data.posts.push({url, ...options});
      const body = options.body && typeof options.body === "string" ? JSON.parse(options.body) : {};
      const metricsRoute = url.match(/^\/api\/training\/(datasets|jobs)\/([^/]+)\/metrics(\/history)?$/);
      if (metricsRoute) {
        const [, kind, rawId, history] = metricsRoute, target = decodeURIComponent(rawId);
        const metrics = kind === "datasets" ? data.datasetMetrics : data.jobMetrics;
        if (method === "GET") return response(history ? data.history.get(target) || {jobs: []} : metrics.get(target) || {status: "not_started", stale: false, result: null});
        if (method === "POST" && !history) {
          const result = {status: "queued", stale: false, result: null};
          metrics.set(target, result);
          for (const value of data.history.values()) {
            const record = value.jobs.find(entry => entry.id === target);
            if (record && kind === "jobs") record.metrics = result;
          }
          return response(result, 202);
        }
      }
      if (method === "GET") {
        if (url === "/api/training/datasets") return response(data.datasets);
        if (url.startsWith("/api/training/jobs?")) return response(data.jobs);
        if (url === "/api/training/capabilities") return response(data.cap);
        if (url === "/api/health") return response({model_status: "ready", stt_status: "ready"});
        if (/\/datasets\/[^/]+\/segments\?/.test(url)) {
          const parsed = new URL(url, "http://127.0.0.1");
          const datasetId = parsed.pathname.split("/")[4];
          const offset = Number(parsed.searchParams.get("offset"));
          return response(data.segments.filter(entry => entry.dataset_id === datasetId).slice(offset, offset + 20));
        }
        const job = data.jobs.find(entry => url === `/api/training/jobs/${entry.id}`);
        if (job) return response(job);
      }
      if (method === "PATCH" && /\/datasets\/[^/]+$/.test(url)) {
        const entry = data.datasets.find(dataset => url.endsWith(dataset.id));
        Object.assign(entry, body); entry.version++;
        return response(entry);
      }
      if (method === "PATCH" && url.includes("/segments/")) {
        const original = data.segments.find(entry => url.endsWith(entry.id));
        if (body.transcript === "拒绝保存") return response({detail: "保存失败，请重试。"}, 409);
        Object.assign(original, body);
        const dataset = data.datasets.find(entry => entry.id === original.dataset_id);
        dataset.approved_duration = body.review_status === "approved" ? 4 : 0;
        dataset.version++;
        return response(original);
      }
      if (method === "POST" && url === "/api/training/datasets") {
        const created = {...body, id: randomUUID(), version: 1, approved_duration: 0};
        data.datasets.unshift(created);
        return response(created, 201);
      }
      if (method === "POST" && url.endsWith("/sources")) return response({id: randomUUID()}, 201);
      if (method === "POST" && url.endsWith("/prepare")) {
        const job = {id: randomUUID(), dataset_id: url.split("/")[4], kind: "prepare", status: "queued", attempt: 1, segments_done: 0, segments_total: 0};
        data.jobs.unshift(job); data.cap.active_job = job.id; data.cap.available = false;
        return response(job, 202);
      }
      if (method === "POST" && /\/datasets\/[^/]+\/jobs$/.test(url)) {
        const key = options.headers["Idempotency-Key"];
        let job = data.keys.get(key);
        if (!job) {
          job = {id: randomUUID(), dataset_id: id, kind: "train", status: "training", attempt: 1,
            config: body, steps_done: 0, steps_total: body.steps, snapshot: {validation_warning: "insufficient_independent_validation"},
            preview: {texts: ["你好，好久不见", "试听句二。"], confirmed: false, note: "",
              summary: {training_seconds: 34, approx_passes: .5, unique_segments_seen: 5,
                training_segments: 10, independent_validation: true}}};
          data.jobs.unshift(job); data.keys.set(key, job);
        }
        data.cap.active_job = job.id; data.cap.available = false;
        if (data.loseTrainResponse) { data.loseTrainResponse = false; throw new Error("Lost response"); }
        return response(job, 202);
      }
      const job = data.jobs.find(entry => url.includes(`/jobs/${entry.id}/`));
      if (method === "POST" && job && url.endsWith("/cancel")) {
        job.status = "cancelling";
        return response(job);
      }
      if (method === "POST" && job && url.endsWith("/retry")) {
        job.status = "queued"; job.attempt++;
        return response(job);
      }
      if (method === "POST" && job && url.endsWith("/preview-confirm")) {
        job.preview = {...job.preview, confirmed: body.confirmed, note: body.note || "", confirmed_at: "2026-01-01T00:00:00+00:00"};
        return response(job.preview);
      }
      if (method === "POST" && job && url.endsWith("/publish")) return response({id: randomUUID(), name: body.name, model_id: randomUUID()}, 201);
      throw new Error(`Unhandled request ${method} ${url}`);
    }
  };
  const doc = fakeDocument();
  doc.getElementById("train-form").fields = config;
  const app = createWorkbench(doc, win);
  return {app, data, doc, win, windowListeners};
}

async function testMetrics() {
  const f = fixture(), datasetId = f.data.datasets[0].id, segmentId = f.data.segments[0].id;
  const quality = {status: "ok", dbfs: -19.123456, low_energy_percent: 0.125, clipping_percent: 0, duration_seconds: 3.75, file_count: 2};
  const metric = (key, generated) => ({status: "completed", stale: false, result: {
    analyzed_at: "2026-03-04T05:06:07Z", comparison_key: key,
    source: {dataset_version: 1, material_digest: "digest-1", reference_kind: "validation", reference_ids: [segmentId], preview_texts: ["试听 <img src=x>", "第二句"]},
    quality: {original: quality, generated: {...quality, dbfs: -21.987654}, previews: [{...quality}]},
    features: {status: "completed", model: "test-model", revision: "v1", algorithm: "cosine", reference_count: 4, pair_count: 6,
      original_index: 0.923456, generated_index: generated, previews: [{index: 1, value: 0.812345, reference_count: 4}, {index: 2, value: null, reference_count: 3}]}
  }});
  const selectedId = randomUUID(), sameId = randomUUID(), b1 = randomUUID(), b2 = randomUUID(), unknown1 = randomUUID(), unknown2 = randomUUID(), staleId = randomUUID(), oldId = randomUUID();
  const preview = {texts: ["试听一", "试听二"], confirmed: false, note: ""};
  const selected = {id: selectedId, dataset_id: datasetId, kind: "train", status: "completed", attempt: 1, config, preview};
  const otherDataset = {...f.data.datasets[0], id: randomUUID(), name: "另一素材集"};
  const foreign = {...selected, id: randomUUID(), dataset_id: otherDataset.id};
  f.data.datasets.push(otherDataset);
  f.data.jobs.push(selected, foreign);
  f.data.datasetMetrics.set(datasetId, metric(null, null));
  f.data.jobMetrics.set(selectedId, metric("same", 0.8));
  const record = (id, steps, result, created_at = "2026-02-03T04:05:06Z") => ({id, created_at, config: {...config, steps, future_parameter: "完整参数"}, metrics: result});
  const singleReference = metric("same", 0.8);
  singleReference.result.features.status = "insufficient";
  f.data.history.set(datasetId, {jobs: [
    record(selectedId, 20, singleReference, "2026-03-03T04:05:06Z"), record(sameId, 50, metric("same", 0.9)),
    record(b1, 20, metric("same-but-different", 0.4)), record(b2, 100, metric("same-but-different", 0.5)),
    record(unknown1, 30, metric(null, 0.2)), record(unknown2, 60, metric(null, 0.3)),
    record(staleId, 100, {...metric("same", 0.99), stale: true}),
    record(oldId, 200, {status: "failed", stale: false, message: "旧任务分析失败", result: null})
  ]});
  f.app.start();
  await f.app.refresh();
  await f.app.refreshMetrics();
  assert.equal(f.data.posts.length, 0, "loading and polling analysis never submit work");
  assert.equal(f.app.state.jobId, selectedId);
  const original = f.doc.getElementById("dataset-metrics-content"), trained = f.doc.getElementById("job-metrics-content");
  assert.match(original.textContent, /原声音色指数：0\.923456/);
  assert.match(original.textContent, /参考数量：4 · 配对数：6/);
  assert.match(original.textContent, /-19\.123456 dBFS/);
  assert.match(original.textContent, /低能量帧：0\.125%/);
  assert.match(original.textContent, /接近削波：0%/);
  const meters = walk(original).filter(element => element.tagName === "METER");
  assert.equal(meters.length, 3, "quality values are shown with comparable visual scales");
  assert.deepEqual(meters.map(meter => [meter.min, meter.max]), [[-80, 0], [0, 100], [0, 100]]);
  assert.match(trained.textContent, /生成音色指数：0\.8/);
  const indexMeters = walk(trained).filter(element => element.className === "index-meter");
  assert.deepEqual(indexMeters.map(meter => [meter.min, meter.max, meter.value]), [[0, 100, 0.923456], [0, 100, 0.8]]);
  assert.match(trained.textContent, /计算方式不同，不能直接相减/);
  assert.match(trained.textContent, /-21\.987654 dBFS/);
  assert.match(trained.textContent, /试听 1 · 指数：0\.812345/);
  assert.match(trained.textContent, /试听 2 · 指数：不可用/);
  assert.match(trained.textContent, /试听 <img src=x>/, "preview text remains plain text");
  const audios = walk(trained).filter(element => element.tagName === "AUDIO");
  assert.equal(audios.length, 3);
  assert(audios.some(audio => audio.src === `/api/training/segments/${segmentId}/audio`));
  assert(audios.some(audio => audio.src === `/api/training/jobs/${selectedId}/preview/1`));
  assert(audios.every(audio => audio.controls && audio.attributes["aria-label"]));
  await f.app.refreshMetrics();
  assert.equal(walk(trained).find(element => element.tagName === "AUDIO"), audios[0], "unchanged polling does not interrupt audio");
  assert.match(trained.textContent, /本条试听质量/);
  assert.match(f.doc.getElementById("comparison-conditions").textContent, /信息不完整/);
  f.data.datasetMetrics.set(datasetId, metric("different", null));
  await f.app.refreshMetrics();
  assert.match(f.doc.getElementById("comparison-conditions").textContent, /条件不同，不能直接比较/);
  f.data.datasetMetrics.set(datasetId, metric("same", null));
  await f.app.refreshMetrics();
  assert.match(f.doc.getElementById("comparison-conditions").textContent, /条件标识一致/);
  const zeroBased = metric("same", 0.8);
  zeroBased.result.features.previews = [{index: 0, value: 0.111, reference_count: 4}, {index: 1, value: 0.222, reference_count: 4}];
  f.data.jobMetrics.set(selectedId, zeroBased);
  await f.app.refreshMetrics();
  assert.match(trained.textContent, /试听 1 · 指数：0\.111/);
  assert.match(trained.textContent, /试听 2 · 指数：0\.222/);

  const history = f.doc.getElementById("metrics-history");
  const groups = history.children.filter(item => item.className === "history-group");
  const same = groups.find(group => group.attributes["data-comparison-key"] === "same");
  const different = groups.find(group => group.attributes["data-comparison-key"] === "same-but-different");
  assert(same && different, "distinct comparison keys have distinct charts");
  for (const [group, ids] of [[same, [selectedId, sameId]], [different, [b1, b2]]]) {
    const lines = walk(group).filter(item => item.tagName === "POLYLINE");
    const circles = walk(group).filter(item => item.tagName === "CIRCLE");
    assert.equal(lines.length, 1);
    assert.equal(lines[0].attributes.points.split(" ").length, 2);
    assert.equal(circles.length, 2, "stale or differently keyed points are excluded");
    for (const id of ids) assert(circles.some(circle => circle.textContent.includes(id)));
  }
  const chronologicalPoints = walk(same).filter(item => item.tagName === "CIRCLE");
  assert(chronologicalPoints[0].textContent.includes(sameId), "history follows time even when step counts decrease");
  assert(chronologicalPoints[1].textContent.includes(selectedId), "a valid generated score is graphed with one reference");
  const unknownGroups = groups.filter(group => !group.attributes["data-comparison-key"]);
  assert.equal(unknownGroups.length, 3, "missing keys do not form a comparable group");
  assert(unknownGroups.every(group => !walk(group).some(item => item.tagName === "POLYLINE")));
  assert.match(history.textContent, /2026-02-03T04:05:06Z/);
  assert.match(history.textContent, /2026-03-04T05:06:07Z/);
  assert.match(history.textContent, /future_parameter: "完整参数"/);
  assert.match(history.textContent, /已过期/);
  const oldAction = walk(history).find(element => element.tagName === "BUTTON" && element.attributes["aria-label"]?.includes(oldId));
  assert(oldAction && !oldAction.disabled, "old tasks outside the recent task list can be retried");
  await oldAction.emit("click");
  assert.equal(f.data.posts.at(-1).url, `/api/training/jobs/${oldId}/metrics`);
  assert.equal(f.data.posts.at(-1).method, "POST");
  assert.equal(f.data.posts.at(-1).body, undefined);
  assert.match(f.doc.getElementById("metrics-history").textContent, /排队中/);

  const jobSelect = f.doc.getElementById("job-select"), comparisonSelect = f.doc.getElementById("comparison-job-select");
  assert.equal(comparisonSelect.value, selectedId);
  jobSelect.value = foreign.id;
  await jobSelect.emit("change");
  assert.equal(comparisonSelect.value, foreign.id, "task selectors remain synchronized");
  assert.match(f.doc.getElementById("job-metrics-status").textContent, /不属于当前素材集/);
  assert.equal(f.doc.getElementById("job-metrics-action").disabled, true);
  comparisonSelect.value = selectedId;
  await comparisonSelect.emit("change");
  assert.equal(jobSelect.value, selectedId, "comparison selection also updates the task panel");
  await f.app.refreshMetrics();
  f.data.jobMetrics.set(selectedId, {status: "interrupted", stale: true, result: {features: {status: "insufficient", message: "参考太少"}}});
  await f.app.refreshMetrics();
  assert.equal(walk(trained).filter(element => element.className === "index-meter").length, 0, "missing indices do not create fabricated bars");
  assert.match(f.doc.getElementById("job-metrics-status").textContent, /计算中断.*素材不足.*过期.*参考太少/);
  assert.equal(f.doc.getElementById("job-metrics-action").textContent, "重试计算");
  await f.doc.getElementById("job-metrics-action").emit("click");
  assert.equal(f.data.posts.at(-1).url, `/api/training/jobs/${selectedId}/metrics`);
  assert.equal(f.doc.getElementById("job-metrics-action").disabled, true, "queued work cannot be submitted twice");
  await f.doc.getElementById("dataset-metrics-action").emit("click");
  assert.equal(f.data.posts.at(-1).url, `/api/training/datasets/${datasetId}/metrics`);
  assert.equal(f.doc.getElementById("dataset-metrics-action").disabled, true);
  await f.app.refreshMetrics();

  const realFetch = f.win.fetch;
  f.win.fetch = async (url, options) => url.endsWith("/metrics/history")
    ? {ok: false, status: 503, json: async () => ({detail: "分析暂不可用"})} : realFetch(url, options);
  await f.app.refreshMetrics();
  assert.match(f.doc.getElementById("metrics-history").textContent, /分析暂不可用/);
  assert.equal(f.app.state.connected, true, "analysis errors do not mark the whole app offline");
  const check = f.doc.getElementById("preview-check");
  check.checked = true;
  await check.emit("change");
  await f.app.confirmPreview();
  assert.equal(f.doc.getElementById("publish-form").hidden, false);
  await f.app.publish("分析故障时仍可发布");
  assert.equal(f.doc.getElementById("published-name").textContent, "分析故障时仍可发布");
  f.app.dispose();

  const race = fixture();
  race.app.start();
  await race.app.refresh();
  await race.app.refreshMetrics();
  const originalFetch = race.win.fetch, url = `/api/training/datasets/${race.data.datasets[0].id}/metrics`;
  let deliver;
  race.win.fetch = async (requestUrl, options = {}) => {
    if (requestUrl === url && (!options.method || options.method === "GET")) return new Promise(resolve => { deliver = resolve; });
    return originalFetch(requestUrl, options);
  };
  const pending = race.app.refreshMetrics();
  await race.doc.getElementById("dataset-metrics-action").emit("click");
  assert.match(race.doc.getElementById("dataset-metrics-status").textContent, /排队中/);
  deliver({ok: true, status: 200, json: async () => ({status: "completed", stale: false, result: null})});
  await pending;
  assert.match(race.doc.getElementById("dataset-metrics-status").textContent, /排队中/, "a late GET cannot overwrite a newer POST");
  race.app.dispose();
}

async function testNavigation() {
  const f = fixture();
  f.app.start();
  await f.app.refresh();
  const tab = name => f.doc.getElementById(`tab-${name}`), panel = name => f.doc.getElementById(`panel-${name}`);
  assert.equal(panel("import").hidden, false);
  assert.equal(panel("review").hidden, true);
  await tab("review").emit("click");
  const textarea = f.doc.querySelectorAll(".segment button, .segment textarea").find(item => item.tagName === "TEXTAREA");
  textarea.value = "未保存的原文";
  await textarea.emit("input");
  await tab("comparison").emit("click");
  assert.equal(panel("review").hidden, true);
  assert.equal(panel("comparison").hidden, false);
  assert.equal(tab("comparison").attributes["aria-selected"], "true");
  await tab("comparison").emit("keydown", {key: "ArrowLeft"});
  assert.equal(panel("training").hidden, false);
  assert.equal(tab("training").focused, true);
  await tab("review").emit("click");
  assert.equal(f.app.state.drafts.get(f.data.segments[0].id), "未保存的原文");
  assert.equal(f.doc.querySelectorAll(".segment button, .segment textarea").find(item => item.tagName === "TEXTAREA"), textarea);
  assert.equal(textarea.value, "未保存的原文", "switching views keeps the editor and its draft");
  f.app.dispose();

  const empty = fixture();
  empty.data.datasets = [];
  empty.app.start();
  await empty.app.refresh();
  assert.equal(empty.doc.getElementById("tab-review").disabled, true);
  assert.equal(empty.doc.getElementById("tab-comparison").disabled, true);
  await empty.doc.getElementById("tab-import").emit("keydown", {key: "ArrowRight"});
  assert.equal(empty.doc.getElementById("panel-training").hidden, false, "keyboard navigation skips unavailable views");
  empty.app.dispose();
}

async function main() {
  assert.equal(formatDuration(125), "2:05");
  assert.equal(formatDuration(-3), "0:00");
  assert.equal(errorMessage([{msg: "步骤无效"}], 422), "步骤无效");
  assert.match(errorMessage({code: "disk_space"}, 422), /磁盘/);

  const f = fixture();
  f.app.start();
  await f.app.refresh();
  assert.equal(f.app.state.selected, f.data.datasets[0].id);
  assert.equal(f.app.canTrain(), false, "unreviewed material must not train");
  assert.equal(f.data.posts.length, 0, "initialization and polling must be read-only");
  assert.equal(f.doc.getElementById("dataset-title").textContent, f.data.datasets[0].name);
  assert.equal(f.doc.querySelectorAll(".segment button, .segment textarea").find(item => item.tagName === "TEXTAREA").value, f.data.segments[0].transcript);
  const segmentCard = walk(f.doc.getElementById("segments")).find(item => item.className === "segment");
  assert(segmentCard, "compact segment card is rendered");
  for (const className of ["segment-meta", "segment-main", "segment-actions"]) {
    assert(walk(segmentCard).some(item => item.className.split(" ").includes(className)), `missing ${className}`);
  }
  const metaParts = walk(segmentCard.children[0]).map(item => item.className);
  assert(metaParts.includes("segment-index") && metaParts.includes("segment-time"), "meta row holds index and time");
  assert(metaParts.some(name => name.startsWith("segment-status ")), "meta row holds a status pill");
  assert.match(segmentCard.children[0].textContent, /01/, "index is zero padded");
  assert.match(segmentCard.children[0].textContent, /0:00–0:04/, "time range is rendered");
  assert.equal(walk(segmentCard).filter(item => item.tagName === "TEXTAREA").length, 1);
  assert.equal(walk(segmentCard).filter(item => item.tagName === "BUTTON").length, 3);
  assert.equal(f.doc.getElementById("publish-form").hidden, true);
  assert.equal(f.doc.getElementById("environment").textContent.includes("Test Mac"), true);

  const segmentId = f.data.segments[0].id;
  await f.app.saveSegment(segmentId, " ", "approved");
  assert.equal(f.data.posts.length, 0);
  f.app.state.drafts.set(segmentId, "拒绝保存");
  await f.app.saveSegment(segmentId, "拒绝保存", "approved");
  assert.equal(f.app.state.drafts.get(segmentId), "拒绝保存", "failed writes preserve drafts");
  await f.app.saveSegment(segmentId, "逐字核对的原文。", "approved");
  assert.equal(f.app.state.drafts.size, 0);
  assert.equal(f.app.canTrain(), true);

  const textarea = f.doc.querySelectorAll(".segment button, .segment textarea").find(item => item.tagName === "TEXTAREA");
  textarea.value = "未保存的新原文";
  await textarea.emit("input");
  await f.app.refresh();
  assert.equal(textarea.value, "未保存的新原文");
  assert.equal(f.app.state.drafts.get(segmentId), "未保存的新原文");
  assert.equal(f.app.canTrain(), false, "dirty text cannot silently train the older transcript");
  let prevented = false;
  f.windowListeners.beforeunload({preventDefault() { prevented = true; }});
  assert.equal(prevented, true);

  const second = {...f.data.datasets[0], id: randomUUID(), name: "第二位说话者"};
  f.data.datasets.push(second);
  await f.app.refresh();
  f.win.confirm = () => false;
  await f.app.selectDataset(second.id);
  assert.notEqual(f.app.state.selected, second.id, "rejecting discard keeps the current dataset");
  f.win.confirm = () => true;
  await f.app.selectDataset(second.id);
  assert.equal(f.app.state.selected, second.id);
  assert.equal(f.app.state.drafts.size, 0);
  await f.app.selectDataset(f.data.datasets[0].id);

  const beforeUpload = f.data.posts.length;
  await f.app.uploadFiles([{name: "bad.flac", size: 1}, {name: "huge.mp3", size: 201 * 1024 ** 2}, {name: "empty.mp4", size: 0}, {name: "first.mp3", size: 100}, {name: "second.mp4", size: 200}]);
  const uploads = f.data.posts.slice(beforeUpload);
  assert.equal(uploads.length, 2, "invalid files are rejected before sending");
  assert.equal(uploads[0].body.values[0][1].name, "first.mp3");
  assert.equal(uploads[1].body.values[0][1].name, "second.mp4");
  assert.equal(uploads[0].headers, undefined, "multipart boundary is browser generated");
  assert.equal(f.app.state.uploads.filter(entry => entry.failed).length, 3);

  f.data.loseTrainResponse = true;
  await f.app.train(config);
  const pending = JSON.parse(f.win.sessionStorage.getItem("voice-training-request"));
  assert(pending.key, "keep idempotency key when the response is lost");
  assert.equal(f.data.jobs.length, 1);
  assert.equal(f.doc.getElementById("publish-form").hidden, true);
  f.data.jobs[0].status = "completed";
  f.data.jobs[0].steps_done = 20;
  f.data.cap.active_job = null; f.data.cap.available = true;
  await f.app.refresh();
  await f.app.train(config);
  assert.equal(f.data.keys.size, 1, "explicit retry must reuse the same backend job");
  assert.equal(f.win.sessionStorage.getItem("voice-training-request"), null);
  await f.app.refresh();
  f.data.jobs[0].status = "completed"; f.data.cap.active_job = null; f.data.cap.available = true;
  await f.app.refresh();
  assert.equal(f.doc.getElementById("preview-section").hidden, false);
  assert.equal(f.doc.getElementById("publish-form").hidden, true, "publish stays hidden until preview is confirmed");
  assert.equal(f.doc.getElementById("preview-samples").children.length, 2);
  const greeting = f.doc.getElementById("preview-samples").children[0];
  assert.equal(greeting.children[1].textContent, "你好，好久不见");
  assert.equal(greeting.children[0].controls, true);
  assert.equal(greeting.children[0].src, `/api/training/jobs/${f.data.jobs[0].id}/preview/1`);
  assert.equal(f.doc.getElementById("confirm-preview").disabled, true);
  assert.match(f.doc.getElementById("job-summary").textContent, /5 个片段未参与参数更新/);
  const postsBeforeConfirm = f.data.posts.length;
  await f.app.publish("未确认");
  assert.equal(f.data.posts.length, postsBeforeConfirm, "unconfirmed publish is blocked in the UI");
  const previewCheck = f.doc.getElementById("preview-check");
  previewCheck.checked = true;
  await previewCheck.emit("change");
  assert.equal(f.doc.getElementById("confirm-preview").disabled, false);
  f.doc.getElementById("preview-note").value = "音色接近";
  await f.app.confirmPreview();
  assert.equal(f.data.jobs[0].preview.confirmed, true);
  assert.equal(f.doc.getElementById("publish-form").hidden, false);
  assert.equal(f.doc.getElementById("preview-status").hidden, false);
  assert.equal(f.doc.getElementById("validation-warning").hidden, false);
  await f.app.refresh();
  assert.equal(f.doc.getElementById("publish-form").hidden, false, "confirmation survives refresh");
  await f.app.publish("角色一 · 已训练声音");
  assert.equal(f.doc.getElementById("published").hidden, false);
  assert.equal(f.doc.getElementById("published-name").textContent, "角色一 · 已训练声音");
  assert(f.doc.getElementById("published-id").value);

  f.data.jobs[0].status = "training"; f.data.cap.active_job = f.data.jobs[0].id;
  await f.app.refresh();
  await f.app.jobAction("cancel");
  assert.equal(f.doc.getElementById("job-status").textContent, "正在取消");
  assert.equal(f.doc.getElementById("cancel-job").disabled, true);
  assert.equal(f.doc.getElementById("publish-form").hidden, true);
  f.data.jobs[0].status = "failed"; f.data.jobs[0].checkpoint = "checkpoint-test";
  f.data.cap.active_job = null;
  await f.app.refresh();
  assert.equal(f.doc.getElementById("retry-job").textContent, "从检查点恢复");
  const postsBeforeRead = f.data.posts.length;
  await f.app.refresh();
  assert.equal(f.data.posts.length, postsBeforeRead, "failed tasks are never automatically retried");
  await f.app.jobAction("retry", false);
  assert.deepEqual(JSON.parse(f.data.posts.findLast(entry => entry.url.endsWith("/retry")).body), {resume: false});
  f.app.dispose();

  const forms = fixture();
  forms.app.start();
  await forms.app.refresh();
  await forms.doc.getElementById("new-dataset").emit("click");
  assert.equal(forms.doc.getElementById("create-dialog").open, true);
  forms.doc.getElementById("dataset-name").value = "  ";
  await forms.doc.getElementById("create-form").emit("submit");
  assert.equal(forms.data.posts.length, 0);
  assert.match(forms.doc.getElementById("create-error").textContent, /空格/);
  forms.doc.getElementById("dataset-name").value = "新建角色声音";
  await forms.doc.getElementById("create-form").emit("submit");
  const createdId = forms.app.state.selected;
  assert.equal(forms.app.state.datasets.find(entry => entry.id === createdId).name, "新建角色声音");
  assert.equal(forms.doc.getElementById("create-dialog").open, false);
  forms.doc.getElementById("speaker-confirmed").checked = true;
  await forms.doc.getElementById("speaker-confirmed").emit("change");
  assert.equal(forms.app.state.datasets.find(entry => entry.id === createdId).speaker_confirmed, true);
  await forms.doc.getElementById("prepare").emit("click");
  assert.equal(forms.doc.getElementById("job-status").textContent, "排队等待");
  forms.data.jobs[0].status = "review_required";
  forms.data.cap.active_job = null; forms.data.cap.available = true;
  forms.data.segments.push({...forms.data.segments[0], id: randomUUID(), dataset_id: createdId});
  await forms.app.refresh();
  assert.equal(forms.app.state.segments.length, 1, "completed preparation refreshes only its dataset's clips");
  assert.equal(forms.doc.getElementById("publish-form").hidden, true, "prepared audio is not a trained voice");
  forms.app.dispose();

  const race = fixture();
  await race.app.refresh();
  const other = {...race.data.datasets[0], id: randomUUID(), name: "另一个数据集"};
  const otherSegment = {...race.data.segments[0], id: randomUUID(), dataset_id: other.id, transcript: "另一个原文"};
  race.data.datasets.push(other); race.data.segments.push(otherSegment);
  await race.app.refresh();
  const realFetch = race.win.fetch;
  let deliver;
  race.win.fetch = async (url, options) => {
    if (url.includes(`/datasets/${race.data.datasets[0].id}/segments?`)) return new Promise(resolve => {
      deliver = () => resolve({ok: true, json: async () => [race.data.segments[0]]});
    });
    return realFetch(url, options);
  };
  const delayed = race.app.loadSegments();
  await race.app.selectDataset(other.id);
  deliver(); await delayed;
  assert.equal(race.app.state.segments[0].id, otherSegment.id, "late responses cannot replace another speaker's clips");
  race.win.fetch = async () => { throw new Error("offline"); };
  await race.app.refresh();
  assert.equal(race.app.state.connected, false);
  assert.equal(race.doc.getElementById("start-training").disabled, true);
  assert.match(race.doc.getElementById("notice").textContent, /无法连接/);
  race.app.dispose();
  await testMetrics();
  await testNavigation();
  console.log("Training UI checks passed: safe rendering, review and publishing, analysis status and retries, comparison groups, history, and stale reads.");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
