"use strict";

(() => {
  const PAGE_SIZE = 20;
  const ACTIVE = new Set(["queued", "preparing", "transcribing", "training", "validating", "cancelling"]);
  const STATUS = {queued: "排队等待", preparing: "准备素材与模型", transcribing: "逐片转写", review_required: "等待人工审核", training: "正在训练", validating: "验证训练产物", completed: "训练完成，待试听确认", failed: "任务失败", cancelling: "正在取消", cancelled: "已取消"};
  const REVIEW = {pending: "待审核", approved: "已确认", excluded: "已排除"};
  const HINTS = {forced_split_check_words: "强制切分，请检查断词", short_segment: "片段偏短，请检查内容", low_volume_or_silence: "低音量或静音", possible_clipping: "可能削波失真", no_speech: "未识别到人声，请人工核对", transcription_failed_edit_required: "转写失败，请手动填写原文"};
  const ERRORS = {interrupted: "上次任务因应用退出而中断，请显式重试或恢复。", training_failed: "任务执行失败。请检查素材、可用内存和模型状态，再重试。", dispatch_failed: "任务未能投递，请重试。", cancelled: "任务已取消。可在退出后显式恢复或重试。", media_timeout: "媒体工具处理超时。请缩短素材后重新上传。", invalid_media: "素材损坏或无法解码，请重新导出 MP3、MP4 或 WAV。", unsupported_codec: "WAV 必须使用未压缩 PCM 编码，不能是压缩或浮点格式。", disk_space: "磁盘剩余空间不足，请清理空间后重试。", decoded_size_limit: "解码音频超过限制，请先缩短素材。", duration_limit: "媒体实际时长超限，请缩短素材。", container_mismatch: "扩展名与实际容器不一致，请正确导出素材。", no_audio_stream: "视频没有可用音轨。", media_tool_unavailable: "未找到 FFmpeg 或 FFprobe，请安装后重试。"};

  function formatDuration(value) {
    const seconds = Math.max(0, Math.round(Number(value) || 0));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }

  function errorMessage(detail, status) {
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map(item => item.msg || "输入无效").join("；");
    if (detail && typeof detail === "object") return detail.message || ERRORS[detail.code] || "请求未完成，请检查任务状态。";
    return `请求失败（${status}），请刷新查看任务状态后再操作。`;
  }

  function createWorkbench(doc, win) {
    const $ = id => doc.getElementById(id);
    const state = {datasets: [], jobs: [], capabilities: null, health: null, selected: "", jobId: "", segments: [], offset: 0,
      drafts: new Map(), uploads: [], published: new Map(), busy: "", connected: false, segmentsLoading: false, needsSegments: false, view: "import", summary: null};
    let refreshing = null, segmentRequest = 0, pollTimer = null, disposed = false;
    let librarySignature = "", jobsSignature = "", previewSignature = "";
    const metricCache = new Map(), metricErrors = new Map(), metricRequests = new Map();
    const metricPosting = new Set(), metricVersions = new Map();
    const comparisonSignatures = {dataset: "", job: "", history: ""};
    const dataset = () => state.datasets.find(item => item.id === state.selected);
    const currentJob = () => state.jobs.find(item => item.id === state.jobId);
    const activeJob = () => state.jobs.find(item => item.id === state.capabilities?.active_job);
    const path = (kind, id) => `/api/training/${kind}/${encodeURIComponent(id)}`;
    const storageGet = (storage, key) => { try { return storage.getItem(key); } catch { return null; } };
    const storageSet = (storage, key, value) => { try { storage.setItem(key, value); } catch { /* The page also works without storage. */ } };
    const storageRemove = (storage, key) => { try { storage.removeItem(key); } catch { /* Nothing to clear when storage is blocked. */ } };

    const views = ["import", "review", "training", "comparison"];
    function renderView() {
      const hasDataset = Boolean(dataset());
      if (!hasDataset && ["review", "comparison"].includes(state.view)) state.view = "import";
      for (const view of views) {
        const tab = $(`tab-${view}`), active = state.view === view;
        tab.disabled = !hasDataset && ["review", "comparison"].includes(view);
        tab.setAttribute("aria-selected", String(active));
        tab.tabIndex = active ? 0 : -1;
        $(`panel-${view}`).hidden = !active;
      }
    }
    function setView(view, focus = false) {
      if (!views.includes(view) || $(`tab-${view}`).disabled) return;
      state.view = view;
      renderView();
      if (focus) $(`tab-${view}`).focus();
    }

    function node(tag, className, text) {
      const element = doc.createElement(tag);
      if (className) element.className = className;
      if (text !== undefined) element.textContent = text;
      return element;
    }

    const metricKey = (kind, id) => `${kind}:${id}`;
    const metricVersion = key => metricVersions.get(key) || 0;
    const invalidateMetric = key => metricVersions.set(key, metricVersion(key) + 1);
    const metricNumber = (value, unit = "") => typeof value === "number" && Number.isFinite(value) ? `${Number(value.toPrecision(8))}${unit}` : "不可用";
    const metricText = value => typeof value === "string" && value ? value : "不可用";
    const metricResult = metric => metric?.result && typeof metric.result === "object" ? metric.result : null;
    const metricBusy = metric => ["queued", "running"].includes(metric?.status);

    function metricStatus(metric, key) {
      const error = metricErrors.get(key);
      if (!metric) return metricPosting.has(key) ? "正在提交计算…" : error ? `读取或提交失败：${error}` : metricRequests.has(key) ? "正在读取分析状态…" : "分析状态不可用。";
      const labels = {not_started: "未开始", queued: "排队中", running: "计算中", completed: "计算完成", failed: "计算失败", interrupted: "计算中断"};
      const features = metricResult(metric)?.features;
      const parts = [metric.status === "completed" && features?.status === "failed" ? "音频质量已计算" : labels[metric.status] || "状态不可用"];
      if (features?.status === "insufficient") parts.push("素材不足");
      if (features?.status === "failed") parts.push("音色特征计算失败");
      if (metric.stale) parts.push("结果已过期，请重新计算");
      if (metricPosting.has(key)) parts.push("正在提交计算");
      if (typeof metric.message === "string" && metric.message) parts.push(metric.message);
      if (typeof features?.message === "string" && features.message) parts.push(features.message);
      if (error) parts.push(`读取或提交失败：${error}`);
      return parts.join(" · ");
    }

    function metricActionLabel(metric, subject) {
      if (metric?.status === "failed" || metric?.status === "interrupted" || metricResult(metric)?.features?.status === "failed") return "重试计算";
      return metric?.status === "completed" || metric?.stale ? "重新计算" : `计算${subject}`;
    }

    function qualityBlock(label, quality) {
      const block = node("div", "metrics-quality");
      block.append(node("strong", "", label));
      if (!quality || quality.status !== "ok") {
        block.append(node("span", "muted", `质量数据不可用${quality?.message ? `：${quality.message}` : ""}`));
      } else {
        for (const [name, value, unit, min, max] of [
          ["整体响度", quality.dbfs, " dBFS", -80, 0],
          ["低能量帧", quality.low_energy_percent, "%", 0, 100],
          ["接近削波", quality.clipping_percent, "%", 0, 100]
        ]) {
          const row = node("div", "metrics-quality-row");
          row.append(node("span", "", `${name}：${metricNumber(value, unit)}`));
          if (typeof value === "number" && Number.isFinite(value)) {
            const meter = node("meter", "metrics-meter");
            meter.min = min; meter.max = max; meter.value = Math.max(min, Math.min(max, value));
            meter.setAttribute("aria-hidden", "true");
            row.append(meter);
          }
          block.append(row);
        }
        block.append(node("span", "muted", `时长：${metricNumber(quality.duration_seconds, " 秒")} · 文件：${metricNumber(quality.file_count)}`));
        if (typeof quality.message === "string" && quality.message) block.append(node("span", "muted", quality.message));
      }
      return block;
    }

    function audioControl(url, label) {
      const audio = node("audio");
      audio.controls = true;
      audio.preload = "none";
      audio.src = url;
      audio.setAttribute("aria-label", label);
      return audio;
    }

    function indexChart(features, status) {
      const chart = node("div", "index-chart");
      chart.append(node("strong", "", "训练快照音色指数（0–100）"), node("p", "muted", status));
      for (const [label, value] of [["任务快照原声指数", features.original_index], ["生成音色指数", features.generated_index]]) {
        const row = node("div", "index-row");
        row.append(node("span", "", `${label}：${metricNumber(value)}`));
        if (typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 100) {
          const meter = node("meter", "index-meter");
          meter.min = 0; meter.max = 100; meter.value = value;
          meter.setAttribute("aria-hidden", "true");
          row.append(meter);
        }
        chart.append(row);
      }
      chart.append(node("p", "muted", "原声指数衡量参考片段间的一致性；生成指数衡量生成声音与参考片段的相似度。计算方式不同，不能直接相减。"));
      return chart;
    }

    function renderMetricResult(container, metric, job) {
      container.replaceChildren();
      const result = metricResult(metric);
      if (!result) return;
      const features = result.features || {}, source = result.source || {}, quality = result.quality || {};
      const details = node("div", "metrics-details");
      const selectedIndex = job ? features.generated_index : features.original_index;
      details.append(node("div", "metrics-value", `${job ? "生成音色指数" : "原声音色指数"}：${metricNumber(selectedIndex)}`));
      if (job) details.append(node("p", "", `任务快照原声指数：${metricNumber(features.original_index)}`));
      if (job) details.append(node("p", "", `有效生成试听：${metricNumber(features.generated_count)} / 2`));
      details.append(node("p", "", `参考数量：${metricNumber(features.reference_count)} · 配对数：${metricNumber(features.pair_count)}`));
      const kinds = {validation: "独立验证片段", training: "训练素材（没有独立验证片段）", current: "当前素材"};
      details.append(node("p", "muted", `参考来源：${kinds[source.reference_kind] || "不可用"}`));
      const provenance = node("details", "metrics-provenance");
      provenance.append(node("summary", "", "分析详情"),
        node("p", "muted", `分析时间：${metricText(result.analyzed_at)} · 素材版本：${metricNumber(source.dataset_version)}`),
        node("p", "muted", `素材摘要：${metricText(source.material_digest)}`),
        node("p", "muted", `模型：${metricText(features.model)} · 版本：${metricText(features.revision)} · 算法：${metricText(features.algorithm)}`),
        node("p", "muted", `比较条件标识：${metricText(result.comparison_key)}`));
      details.append(provenance);
      details.append(node("p", "metrics-subtitle", "试听质量"), qualityBlock(job ? "任务快照原声" : "当前原声", quality.original));
      if (job) details.append(qualityBlock("生成试听", quality.generated));

      const referenceIds = Array.isArray(source.reference_ids) ? source.reference_ids : [];
      if (referenceIds.length) {
        details.append(node("p", "metrics-subtitle", "逐条试听参考原声"));
        if (job) details.append(node("p", "muted", "这里播放当前片段，片段变化后可能与任务快照不同。"));
        const references = node("div", "metrics-audio-list");
        referenceIds.forEach((id, index) => {
          if (typeof id !== "string" || !id) return;
          const row = node("div", "metrics-audio");
          row.append(node("span", "", `参考原声 ${index + 1} · ${id}`), audioControl(`${path("segments", id)}/audio`, `试听参考原声 ${index + 1}`));
          references.append(row);
        });
        details.append(references);
      }
      if (job) {
        const texts = Array.isArray(source.preview_texts) ? source.preview_texts : [];
        const previews = Array.isArray(features.previews) ? features.previews : [];
        const qualityPreviews = Array.isArray(quality.previews) ? quality.previews : [];
        const zeroBased = previews.some(preview => preview?.index === 0);
        const normalizedIndex = (preview, fallback) => Number.isInteger(preview?.index) ? preview.index + (zeroBased ? 1 : 0) : fallback + 1;
        const numbers = new Set(texts.map((_, index) => index + 1));
        previews.forEach((preview, index) => numbers.add(normalizedIndex(preview, index)));
        qualityPreviews.forEach((preview, index) => numbers.add(normalizedIndex(preview, index)));
        if (numbers.size) {
          details.append(node("p", "metrics-subtitle", "逐条试听生成声音"));
          const samples = node("div", "metrics-audio-list");
          for (const index of [...numbers].sort((a, b) => a - b)) {
            if (!Number.isInteger(index) || index < 1) continue;
            const preview = previews.find((entry, position) => normalizedIndex(entry, position) === index);
            const previewQuality = qualityPreviews.find((entry, position) => normalizedIndex(entry, position) === index);
            const row = node("div", "metrics-audio");
            row.append(node("strong", "", `试听 ${index} · 指数：${metricNumber(preview?.value)} · 参考数量：${metricNumber(preview?.reference_count)}`));
            if (index <= 2) row.append(audioControl(`${path("jobs", job.id)}/preview/${index}`, `试听生成声音 ${index}`));
            else row.append(node("span", "muted", "此试听音频不可用。"));
            if (typeof texts[index - 1] === "string") row.append(node("p", "", texts[index - 1]));
            if (previewQuality) row.append(qualityBlock("本条试听质量", previewQuality.quality || previewQuality));
            samples.append(row);
          }
          details.append(samples);
        }
      }
      container.append(details);
    }

    function svgNode(tag, attributes = {}, text) {
      const element = doc.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, String(value));
      if (text !== undefined) element.textContent = text;
      return element;
    }

    function historyChart(records, comparable) {
      const points = records.filter(record => record.metrics?.status === "completed" && !record.metrics?.stale &&
        metricResult(record.metrics)?.features?.status !== "failed" &&
        typeof record.config?.steps === "number" && Number.isFinite(record.config.steps) &&
        Number.isFinite(Date.parse(record.created_at)) &&
        typeof metricResult(record.metrics)?.features?.generated_index === "number" && Number.isFinite(metricResult(record.metrics).features.generated_index))
        .map(record => ({steps: record.config.steps, value: metricResult(record.metrics).features.generated_index,
          id: record.id, created: record.created_at, time: Date.parse(record.created_at)}))
        .sort((a, b) => a.time - b.time || a.id.localeCompare(b.id));
      if (!points.length) return node("p", "muted", "没有可绘制的有效指数；完整状态见下方记录。");
      const chart = svgNode("svg", {viewBox: "0 0 360 155", class: "history-chart", role: "img", "aria-label": "生成音色指数随任务创建时间变化；具体数值和参数见下方表格"});
      chart.append(svgNode("title", {}, "生成音色指数与训练时间"), svgNode("desc", {}, "横轴为任务创建时间，纵轴为生成音色指数。完整数值和训练参数见记录表。"));
      chart.append(svgNode("line", {x1: 40, y1: 15, x2: 40, y2: 120, class: "chart-axis"}), svgNode("line", {x1: 40, y1: 120, x2: 340, y2: 120, class: "chart-axis"}));
      chart.append(svgNode("text", {x: 34, y: 19, "text-anchor": "end"}, "100"), svgNode("text", {x: 34, y: 123, "text-anchor": "end"}, "0"));
      const times = points.map(point => point.time), ys = points.map(point => point.value);
      const minX = Math.min(...times), maxX = Math.max(...times), minY = Math.min(...ys), maxY = Math.max(...ys);
      const coords = points.map((point, index) => ({...point,
        x: maxX === minX ? (points.length === 1 ? 190 : 40 + index / (points.length - 1) * 300) : 40 + (point.time - minX) / (maxX - minX) * 300,
        y: 120 - Math.max(0, Math.min(100, point.value)) / 100 * 105}));
      if (comparable && coords.length > 1) chart.append(svgNode("polyline", {points: coords.map(point => `${point.x},${point.y}`).join(" "), class: "chart-line"}));
      for (const point of coords) {
        const circle = svgNode("circle", {cx: point.x, cy: point.y, r: 4, class: "chart-point"});
        circle.append(svgNode("title", {}, `任务 ${point.id}，创建时间 ${point.created}，${point.steps} 步，指数 ${point.value}`));
        chart.append(circle);
      }
      const timeLabel = value => value.slice(0, 16).replace("T", " ");
      chart.append(svgNode("text", {x: 40, y: 139}, timeLabel(points[0].created)),
        svgNode("text", {x: 340, y: 139, "text-anchor": "end"}, timeLabel(points[points.length - 1].created)));
      const wrap = node("div");
      wrap.append(chart, node("p", "muted", `图中生成指数范围：${minY} 至 ${maxY}；步数、时间和参数见下表。`));
      return wrap;
    }

    function historyParameters(config) {
      if (!config || typeof config !== "object" || Array.isArray(config)) return "不可用";
      const entries = Object.entries(config);
      return entries.length ? entries.map(([key, value]) => `${key}: ${JSON.stringify(value) ?? "不可用"}`).join("\n") : "不可用";
    }

    function renderHistory(container, history, selected) {
      container.replaceChildren();
      const historyKey = metricKey("history", selected), error = metricErrors.get(historyKey);
      if (error) container.append(node("p", "job-error", `历史记录读取失败：${error}`));
      if (!history) {
        if (!error) container.append(node("p", "muted", metricRequests.has(historyKey) ? "正在读取历史记录…" : "历史记录不可用。"));
        return;
      }
      const jobs = Array.isArray(history.jobs) ? history.jobs : [];
      if (!jobs.length) { container.append(node("p", "muted", "暂无训练历史。")); return; }
      const groups = new Map();
      jobs.forEach((job, index) => {
        const key = metricResult(job.metrics)?.comparison_key;
        const comparable = typeof key === "string" && key.length > 0;
        const groupKey = comparable ? `key:${key}` : `unknown:${index}`;
        if (!groups.has(groupKey)) groups.set(groupKey, {key: comparable ? key : null, jobs: []});
        groups.get(groupKey).jobs.push(job);
      });
      let groupNumber = 0;
      for (const group of groups.values()) {
        const section = node("section", "history-group");
        section.setAttribute("data-comparison-key", group.key || "");
        section.append(node("h4", "", `条件 ${++groupNumber}${group.key ? "" : " · 不可比较"}`));
        section.append(node("p", "muted history-key", group.key ? `比较条件标识：${group.key}` : "比较条件标识不可用，此记录不与其他记录连线。"));
        section.append(historyChart(group.jobs, Boolean(group.key)));
        const table = node("table", "history-table"), caption = node("caption", "", "训练历史与完整参数"), head = node("thead"), heading = node("tr"), body = node("tbody");
        const labels = ["步数", "任务与时间", "原声 / 生成指数", "分析状态", "全部训练参数", "操作"];
        labels.forEach(label => { const th = node("th", "", label); th.setAttribute("scope", "col"); heading.append(th); });
        head.append(heading);
        for (const job of group.jobs) {
          const row = node("tr"), result = metricResult(job.metrics), features = result?.features || {};
          const values = [metricNumber(job.config?.steps), `${metricText(job.id)}\n创建：${metricText(job.created_at)}\n分析：${metricText(result?.analyzed_at)}`,
            `${metricNumber(features.original_index)} / ${metricNumber(features.generated_index)}`, metricStatus(job.metrics, metricKey("job", job.id)), historyParameters(job.config)];
          values.forEach((value, index) => {
            const cell = node("td", index === 1 || index === 4 ? "history-parameters" : "", value);
            cell.setAttribute("data-label", labels[index]);
            row.append(cell);
          });
          const actionCell = node("td"), action = node("button", "small", metricActionLabel(job.metrics, "任务"));
          action.type = "button";
          action.disabled = !state.connected || typeof job.id !== "string" || !job.id || metricBusy(job.metrics) || metricPosting.has(metricKey("job", job.id));
          action.setAttribute("aria-label", `${metricActionLabel(job.metrics, "任务")} ${metricText(job.id)}`);
          action.addEventListener("click", () => postMetric("job", job.id, selected));
          actionCell.setAttribute("data-label", labels[5]);
          actionCell.append(action); row.append(actionCell); body.append(row);
        }
        table.append(caption, head, body); section.append(table); container.append(section);
      }
    }

    function renderComparison() {
      const selected = state.selected, job = currentJob();
      const datasetKey = metricKey("dataset", selected), jobKey = metricKey("job", job?.id || ""), historyKey = metricKey("history", selected);
      const eligible = Boolean(job && job.dataset_id === selected && job.kind === "train" && job.status === "completed");
      const original = metricCache.get(datasetKey), trained = eligible ? metricCache.get(jobKey) : null, history = metricCache.get(historyKey);
      const signature = key => [metricErrors.get(key), !metricCache.has(key) && metricRequests.has(key), metricPosting.has(key)];
      const datasetSignature = JSON.stringify([selected, state.connected, original, signature(datasetKey)]);
      if (datasetSignature !== comparisonSignatures.dataset) {
        comparisonSignatures.dataset = datasetSignature;
        $("dataset-metrics-status").textContent = selected ? metricStatus(original, datasetKey) : "请选择素材集。";
        $("dataset-metrics-action").textContent = metricActionLabel(original, "原声");
        $("dataset-metrics-action").disabled = !selected || !state.connected || metricBusy(original) || metricPosting.has(datasetKey);
        renderMetricResult($("dataset-metrics-content"), original, null);
      }
      const jobSignature = JSON.stringify([selected, job?.id, job?.dataset_id, job?.kind, job?.status, state.connected, trained, signature(jobKey)]);
      if (jobSignature !== comparisonSignatures.job) {
        comparisonSignatures.job = jobSignature;
        $("job-metrics-status").textContent = !job ? "请选择已完成的训练任务。" : job.dataset_id !== selected ? "所选任务不属于当前素材集，请选择该素材集的训练任务。" : !eligible ? "请选择当前素材集已完成的训练任务。" : metricStatus(trained, jobKey);
        $("job-metrics-action").textContent = metricActionLabel(trained, "任务");
        $("job-metrics-action").disabled = !eligible || !state.connected || metricBusy(trained) || metricPosting.has(jobKey);
        renderMetricResult($("job-metrics-content"), trained, eligible ? job : null);
        const result = metricResult(trained), status = $("job-metrics-status").textContent;
        $("index-comparison").replaceChildren(result ? indexChart(result.features || {}, status) :
          node("p", "muted", eligible ? `${status} 完成分析后显示指数图。` : status));
      }
      const originalResult = metricResult(original), trainedResult = metricResult(trained);
      const conditions = $("comparison-conditions");
      const hideConditions = !(original?.status === "completed" && trained?.status === "completed" && originalResult && trainedResult);
      if (conditions.hidden !== hideConditions) conditions.hidden = hideConditions;
      if (!hideConditions) {
        const a = originalResult.source || {}, b = trainedResult.source || {};
        const af = originalResult.features || {}, bf = trainedResult.features || {};
        const present = value => typeof value === "string" && Boolean(value);
        const validRefs = refs => Array.isArray(refs) && refs.length > 0 && refs.every(present) && new Set(refs).size === refs.length;
        const aRefs = validRefs(a.reference_ids), bRefs = validRefs(b.reference_ids);
        const modelFields = ["model", "revision", "algorithm"];
        const different = (a.dataset_version != null && b.dataset_version != null && a.dataset_version !== b.dataset_version) ||
          (present(a.material_digest) && present(b.material_digest) && a.material_digest !== b.material_digest) ||
          (aRefs && bRefs && (a.reference_ids.length !== b.reference_ids.length || a.reference_ids.some(id => !b.reference_ids.includes(id)))) ||
          modelFields.some(field => present(af[field]) && present(bf[field]) && af[field] !== bf[field]);
        let message;
        if (original.stale || trained.stale) message = "分析结果已过期，不能直接比较；请先重新计算。";
        else if (different) message = "当前原声与任务快照的素材、参考片段或分析模型不同，不能直接比较。";
        else if (a.dataset_version == null || b.dataset_version == null || !present(a.material_digest) || !present(b.material_digest) ||
          !aRefs || !bRefs || modelFields.some(field => !present(af[field]) || !present(bf[field]))) {
          message = "素材或分析条件信息不完整，无法确认参考条件是否一致。";
        } else message = "素材版本、参考片段与分析模型一致；两项指数含义不同，仍请逐条试听。";
        if (conditions.textContent !== message) conditions.textContent = message;
      }
      const historySignature = JSON.stringify([selected, state.connected, history, signature(historyKey), [...metricPosting], [...metricErrors]]);
      if (historySignature !== comparisonSignatures.history) {
        comparisonSignatures.history = historySignature;
        if (selected) renderHistory($("metrics-history"), history, selected);
        else $("metrics-history").replaceChildren(node("p", "muted", "请选择素材集。"));
      }
    }

    function updateHistoryMetric(datasetId, jobId, metric) {
      const key = metricKey("history", datasetId), history = metricCache.get(key);
      if (Array.isArray(history?.jobs)) metricCache.set(key, {...history, jobs: history.jobs.map(job => job.id === jobId ? {...job, metrics: metric} : job)});
    }

    function fetchMetric(kind, id) {
      const key = metricKey(kind, id);
      if (metricRequests.has(key)) return metricRequests.get(key);
      if (metricPosting.has(key)) return Promise.resolve();
      const version = metricVersion(key), url = kind === "history" ? `${path("datasets", id)}/metrics/history` : `${path(kind === "dataset" ? "datasets" : "jobs", id)}/metrics`;
      const request = (async () => {
        try {
          const result = await api(url);
          if (version === metricVersion(key)) { metricCache.set(key, result); metricErrors.delete(key); }
        } catch (error) {
          if (version === metricVersion(key)) metricErrors.set(key, error.message);
        } finally {
          metricRequests.delete(key);
          renderComparison();
        }
      })();
      metricRequests.set(key, request);
      renderComparison();
      return request;
    }

    function refreshMetrics() {
      const selected = state.selected, job = currentJob();
      if (!selected || !state.connected) return Promise.resolve();
      const requests = [fetchMetric("dataset", selected), fetchMetric("history", selected)];
      if (job?.dataset_id === selected && job.kind === "train" && job.status === "completed") requests.push(fetchMetric("job", job.id));
      return Promise.all(requests);
    }

    async function postMetric(kind, id, datasetId) {
      if (!id || !state.connected) return;
      const key = metricKey(kind, id), historyKey = metricKey("history", datasetId);
      if (metricPosting.has(key)) return;
      metricPosting.add(key); metricErrors.delete(key); invalidateMetric(key); invalidateMetric(historyKey);
      renderComparison();
      try {
        const result = await api(`${path(kind === "dataset" ? "datasets" : "jobs", id)}/metrics`, {method: "POST"});
        metricCache.set(key, result);
        if (kind === "job") updateHistoryMetric(datasetId, id, result);
      } catch (error) {
        metricErrors.set(key, error.message);
      } finally {
        invalidateMetric(historyKey);
        metricPosting.delete(key);
        renderComparison();
        void fetchMetric("history", datasetId);
      }
    }

    function notice(message, error = false) {
      $("notice").textContent = message;
      $("notice").className = error ? "notice error" : "notice";
      $("notice").hidden = !message;
    }

    async function api(url, options = {}) {
      let response;
      try {
        response = await win.fetch(url, {credentials: "same-origin", cache: "no-store", ...options});
      } catch {
        throw new Error("无法连接本机服务。请确认应用已启动，再刷新状态；不要直接重复提交训练。");
      }
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error(errorMessage(data?.detail, response.status));
      if (data === null) throw new Error("服务返回了无效数据，请刷新状态后再操作。");
      return data;
    }

    const json = (method, body, headers = {}) => ({method, headers: {"Content-Type": "application/json", ...headers}, body: JSON.stringify(body)});

    function canTrain() {
      const item = dataset();
      return Boolean(item?.speaker_confirmed && item.approved_duration > 0 && state.capabilities?.available
        && state.connected && !state.busy && !state.drafts.size);
    }

    function editingBlocked() {
      const active = activeJob();
      return Boolean(state.busy || state.segmentsLoading || !state.connected || !dataset() ||
        (active?.kind === "prepare" && active.dataset_id === state.selected));
    }

    function renderLibrary() {
      const signature = JSON.stringify([state.datasets, state.selected]);
      if (signature === librarySignature) return;
      librarySignature = signature;
      const list = $("datasets");
      list.replaceChildren();
      if (!state.datasets.length) {
        list.append(node("p", "muted", state.connected ? "还没有素材集。点击新建开始。" : "未连接服务，点击刷新状态重试。"));
      }
      for (const item of state.datasets) {
        const button = node("button", "dataset-item");
        button.type = "button";
        button.setAttribute("aria-current", String(item.id === state.selected));
        button.append(node("strong", "", item.name), node("span", "", `已确认 ${formatDuration(item.approved_duration)} · 版本 ${item.version}`));
        button.addEventListener("click", () => selectDataset(item.id));
        list.append(button);
      }
    }

    function renderPreview(item, preview) {
      const isAb = (preview?.ab || 0) >= 2;
      const signature = item ? `${item.id}:${JSON.stringify(preview?.texts || [])}:${Boolean(preview?.confirmed)}:${isAb}` : "";
      if (signature !== previewSignature) {
        previewSignature = signature;
        const samples = $("preview-samples");
        samples.replaceChildren();
        const players = [];
        for (const [index, text] of (preview?.texts || []).entries()) {
          const sample = node("div", "preview-sample");
          if (isAb) {
            sample.append(node("p", "preview-label", `验收 ${index + 1} · 同文本同参考`));
            for (const variant of ["base", "trained"]) {
              const row = node("div", "preview-variant");
              const name = variant === "base" ? "基座" : "训练后";
              const metrics = preview.preview_metrics?.[variant]?.[String(index + 1).padStart(2, "0")];
              const detail = metrics ? ` · RTF ${metricNumber(metrics.rtf)} · ${metricNumber(metrics.duration, " 秒")}` : "";
              row.append(node("strong", "", name + detail));
              const audio = node("audio");
              audio.controls = true;
              audio.preload = "none";
              audio.src = `${path("jobs", item.id)}/preview/${index + 1}?variant=${variant}`;
              audio.setAttribute("aria-label", `${name}试听 ${index + 1}`);
              audio.addEventListener("play", () => players.forEach(other => { if (other !== audio) other.pause(); }));
              players.push(audio);
              row.append(audio);
              sample.append(row);
            }
          } else {
            const audio = node("audio");
            audio.controls = true;
            audio.preload = "none";
            audio.src = `${path("jobs", item.id)}/preview/${index + 1}`;
            audio.setAttribute("aria-label", `试听验收样本 ${index + 1}`);
            audio.addEventListener("play", () => players.forEach(other => { if (other !== audio) other.pause(); }));
            players.push(audio);
            sample.append(audio);
          }
          sample.append(node("p", "", text));
          samples.append(sample);
        }
        if (!preview?.confirmed) {
          $("preview-check").checked = false;
          $("preview-note").value = "";
        }
      }
      const confirmed = Boolean(preview?.confirmed);
      const ready = Boolean(item && preview?.texts?.length === 2 && !confirmed && state.connected && !state.busy);
      $("preview-check").disabled = !ready;
      $("preview-note").disabled = !ready;
      $("confirm-preview").disabled = !ready || !$("preview-check").checked;
      $("preview-status").hidden = !confirmed;
      if (confirmed) {
        const note = preview.note ? ` 备注：${preview.note}` : "";
        $("preview-status").textContent = `已确认试听，可发布为新声音。${note}`;
      }
    }

    function renderJob() {
      const item = currentJob();
      const preview = item?.kind === "train" ? item.preview : null;
      const signature = JSON.stringify(state.jobs.map(job => [job.id, job.status, job.dataset_id, job.preview?.confirmed]));
      const selects = [$("job-select"), $("comparison-job-select")];
      if (jobsSignature !== signature) {
        jobsSignature = signature;
        for (const select of selects) {
          select.replaceChildren();
          if (!state.jobs.length) select.append(node("option", "", "暂无任务"));
          for (const job of state.jobs) {
            const name = state.datasets.find(entry => entry.id === job.dataset_id)?.name || "素材集";
            const label = job.kind === "train" && job.status === "completed"
              ? (job.preview?.confirmed ? "训练完成，已确认试听" : STATUS[job.status]) : STATUS[job.status] || job.status;
            const option = node("option", "", `${name} / ${job.kind === "train" ? "训练" : "处理"} / ${label} / ${job.id.slice(0, 8)}`);
            option.value = job.id;
            select.append(option);
          }
        }
      }
      for (const select of selects) {
        select.value = state.jobId;
        select.disabled = Boolean(state.busy || !state.jobs.length);
      }
      $("job-detail").hidden = !item;
      $("job-empty").hidden = Boolean(item);
      $("preview-section").hidden = !(item?.kind === "train" && item.status === "completed");
      $("publish-form").hidden = !(item?.kind === "train" && item.status === "completed" && preview?.confirmed);
      $("cancel-job").hidden = !item || !ACTIVE.has(item.status);
      $("retry-job").hidden = !item || !["failed", "cancelled"].includes(item.status);
      $("restart-job").hidden = !item || !["failed", "cancelled"].includes(item.status) || !item.checkpoint || item.kind !== "train";
      $("variant-job").hidden = !(item?.kind === "train" && item.status === "completed");
      $("variant-note").hidden = !(item?.kind === "train" && item.status === "completed");
      $("cancel-note").hidden = !item || !ACTIVE.has(item.status);
      if (item) {
        $("job-status").textContent = item.status === "completed" && preview?.confirmed ? "训练完成，已确认试听" : STATUS[item.status] || item.status;
        $("job-attempt").textContent = `第 ${item.attempt} 次`;
        const training = item.kind === "train";
        const done = Number(training ? item.steps_done : item.segments_done) || 0;
        const total = Number(training ? item.steps_total : item.segments_total) || 0;
        $("job-progress-text").textContent = total > 0 ? `${training ? "训练步骤" : "当前来源的片段"} ${done} / ${total}` : "等待可用的进度信息，不估算百分比。";
        $("job-progress").hidden = !(total > 0);
        $("job-progress").max = Math.max(total, 1);
        $("job-progress").value = Math.min(done, total);
        $("job-error").hidden = !item.error_code;
        $("job-error").textContent = item.error_code ? `${ERRORS[item.error_code] || "任务未完成，请检查素材和模型后重试。"}（${item.error_code}）` : "";
        $("validation-warning").hidden = !item.snapshot?.validation_warning;
        $("validation-warning").textContent = "独立验证不足：当前素材不能可靠划分验证集。请另用未参与训练的录音和文本试听验收。";
        const summary = preview?.summary;
        $("job-summary").hidden = !summary;
        if (summary) {
          const parts = [`已训练 ${summary.training_seconds} 秒 / 约 ${summary.approx_passes} 轮`];
          if (summary.unique_segments_seen < summary.training_segments) {
            parts.push(`${summary.training_segments - summary.unique_segments_seen} 个片段未参与参数更新`);
          } else if (summary.approx_passes < 2) {
            parts.push("数据覆盖不到两轮，建议补清晰素材或做训练量对照");
          }
          if (!summary.independent_validation) parts.push("没有独立验证片段，请用未训练文本验收");
          $("job-summary").textContent = parts.join("；");
        }
        renderPreview(item, preview);
        $("retry-job").textContent = item.kind === "train" && item.checkpoint ? "从检查点恢复" : "重新尝试";
        $("cancel-job").disabled = Boolean(state.busy || item.status === "cancelling" || !state.connected);
        $("retry-job").disabled = $("restart-job").disabled = Boolean(state.busy || state.capabilities?.active_job || !state.connected);
        $("variant-job").disabled = Boolean(state.busy || state.capabilities?.active_job || !state.connected);
        $("publish").disabled = Boolean(state.busy || state.capabilities?.active_job || !state.connected);
        if ($("voice-name").dataset.job !== item.id) {
          $("voice-name").dataset.job = item.id;
          const name = state.datasets.find(entry => entry.id === item.dataset_id)?.name || "角色";
          $("voice-name").value = `${name} · 训练声音`.slice(0, 60);
        }
      } else {
        $("job-summary").hidden = true;
        renderPreview(null, null);
      }
      const published = state.published.get(state.jobId);
      $("published").hidden = !published;
      if (published) {
        $("published-name").textContent = published.name;
        $("published-id").value = published.id;
        const link = $("start-realtime");
        if (link) link.href = `/realtime/?voice_id=${encodeURIComponent(published.id)}`;
      }
    }

    function render() {
      renderLibrary();
      const item = dataset(), cap = state.capabilities;
      $("welcome").hidden = Boolean(item);
      $("workspace-heading").hidden = !item;
      $("import-content").hidden = !item;
      renderView();
      $("connection-dot").className = `dot ${!state.connected ? "failed" : cap?.available ? "ready" : ""}`;
      $("connection").textContent = state.busy || (!state.connected ? "未连接本机服务" : cap?.active_job ? `本机任务：${STATUS[activeJob()?.status] || "正在执行"}` : cap?.available ? "本机模型就绪" : "模型加载中、忙碌或不可用");
      $("refresh").disabled = Boolean(state.busy);
      for (const id of ["new-dataset", "welcome-create", "create-dataset"]) $(id).disabled = Boolean(state.busy || !state.connected);
      $("close-create").disabled = Boolean(state.busy);
      for (const button of doc.querySelectorAll(".dataset-item")) button.disabled = Boolean(state.busy);
      if (item) {
        $("dataset-title").textContent = item.name;
        $("dataset-version").textContent = `版本 ${item.version}`;
        $("speaker-confirmed").checked = Boolean(item.speaker_confirmed);
        $("review-summary").textContent = `素材集已确认 ${formatDuration(item.approved_duration)}；本页 ${state.segments.length} 个片段。只有确认的片段进入训练。`;
      }
      $("speaker-confirmed").disabled = editingBlocked();
      $("source-files").disabled = Boolean(state.busy || !state.connected || cap?.active_job || !item);
      $("prepare").disabled = Boolean(state.busy || !state.connected || cap?.active_job || !item || !cap?.ffmpeg || !cap?.ffprobe);
      $("refresh-segments").disabled = Boolean(state.busy || state.segmentsLoading || !state.connected);
      $("previous-segments").disabled = Boolean(state.busy || state.segmentsLoading || state.offset === 0);
      $("next-segments").disabled = Boolean(state.busy || state.segmentsLoading || state.segments.length < PAGE_SIZE);
      $("segment-page").textContent = `第 ${Math.floor(state.offset / PAGE_SIZE) + 1} 页 · 每页 ${PAGE_SIZE} 条`;
      $("draft-notice").hidden = !state.drafts.size && !state.needsSegments;
      $("draft-notice").textContent = state.needsSegments ? "素材处理有更新。保存原文后点击刷新片段查看。" : "有尚未保存的原文。刷新或切换素材集前，请先保存。";
      for (const control of doc.querySelectorAll(".segment button, .segment textarea")) control.disabled = editingBlocked();
      $("start-training").disabled = !canTrain();
      const readiness = !item ? "选择素材集后，先确认说话者并审核片段。" : state.drafts.size ? "请先保存或确认正在编辑的原文，再启动训练。" : !item.speaker_confirmed ? "请先确认素材属于同一位目标说话者。" : !(item.approved_duration > 0) ? "还没有确认的片段。先试听、校对并确认至少一条。" : !cap?.available ? "素材已具备审核条件，等待本机模型空闲且可用。" : `可训练：已确认 ${formatDuration(item.approved_duration)}。开始时会冻结当前原文与音频清单。`;
      const summary = item ? state.summary : null;
      const rollup = summary
        ? `已确认 ${summary.approved_segments} 段（${formatDuration(summary.approved_seconds)}）· 待审核 ${summary.pending_segments} 段 · 独立验证：${summary.independent_validation ? "有" : "无，需用未训练文本试听"}`
        : "";
      $("training-readiness").textContent = rollup ? `${readiness} ${rollup}` : readiness;
      if (cap) $("environment").textContent = `FFmpeg：${cap.ffmpeg ? "可用" : "未安装"} / FFprobe：${cap.ffprobe ? "可用" : "未安装"}\n训练环境：${cap.compatible ? "版本兼容" : "不兼容，请检查固定依赖与 Apple Silicon"}\n${cap.hardware?.chip || "本机"}${cap.hardware?.memory_gib ? ` / ${cap.hardware.memory_gib} GiB` : ""}\n基座：${cap.base_model}\n${state.health?.model_error || state.health?.stt_error || "batch size 固定 1，原始模型权重冻结。"}`;
      renderJob();
      renderComparison();
    }

    function renderSegments() {
      const container = $("segments");
      container.replaceChildren();
      if (!state.segments.length) {
        const empty = node("div", "empty", state.segmentsLoading ? "正在读取片段…" : state.offset ? "本页没有更多片段" : "还没有可审核的片段");
        empty.append(node("p", "", state.offset ? "点击上一页返回。" : "先上传素材，再点击“处理素材并转写”。处理完成后刷新这里。"));
        container.append(empty);
      }
      state.segments.forEach((segment, index) => {
        const article = node("article", "segment");
        article.title = `来源 ${segment.source_id.slice(0, 8)} / ${(segment.end - segment.start).toFixed(2)} 秒`;
        const meta = node("div", "segment-meta");
        meta.append(
          node("span", "segment-index", String(state.offset + index + 1).padStart(2, "0")),
          node("span", "segment-time", `${formatDuration(segment.start)}–${formatDuration(segment.end)}`),
          node("span", `segment-status ${segment.review_status}`, REVIEW[segment.review_status] || "待审核"));
        const main = node("div", "segment-main");
        const audio = node("audio");
        audio.controls = true;
        audio.preload = "none";
        audio.src = `${path("segments", segment.id)}/audio`;
        audio.setAttribute("aria-label", `试听片段 ${state.offset + index + 1}`);
        const body = node("div", "segment-body");
        const label = node("label", "segment-transcript"), textarea = node("textarea");
        textarea.maxLength = 2000;
        textarea.value = state.drafts.has(segment.id) ? state.drafts.get(segment.id) : segment.transcript;
        textarea.setAttribute("aria-label", `片段 ${state.offset + index + 1} 原文`);
        textarea.addEventListener("input", () => {
          if (textarea.value === segment.transcript) state.drafts.delete(segment.id);
          else state.drafts.set(segment.id, textarea.value);
          render();
        });
        label.append(textarea);
        body.append(label);
        if (segment.quality_hints?.length) body.append(node("p", "quality-hints", segment.quality_hints.map(hint => HINTS[hint] || "素材需要人工检查").join("；")));
        main.append(audio, body);
        const actions = node("div", "segment-actions");
        for (const [text, review, className] of [["确认片段", "approved", "small"], ["保存原文", "pending", "quiet small"], ["排除此片段", "excluded", "quiet small"]]) {
          const button = node("button", className, text);
          button.type = "button";
          button.addEventListener("click", () => saveSegment(segment.id, textarea.value, review));
          actions.append(button);
        }
        article.append(meta, main, actions);
        container.append(article);
      });
      render();
    }

    function discardDrafts() {
      if (state.drafts.size && !win.confirm("有未保存的原文。确定放弃这些修改吗？")) return false;
      state.drafts.clear();
      return true;
    }

    async function loadSegments() {
      if (!state.selected) return;
      const request = ++segmentRequest, selected = state.selected, offset = state.offset;
      state.segmentsLoading = true;
      render();
      try {
        const [segments, summary] = await Promise.all([
          api(`${path("datasets", selected)}/segments?offset=${offset}&limit=${PAGE_SIZE}`),
          api(`${path("datasets", selected)}/summary`).catch(() => null),
        ]);
        if (request !== segmentRequest || selected !== state.selected || offset !== state.offset) return;
        state.segments = segments;
        state.summary = summary;
        state.needsSegments = false;
        renderSegments();
      } catch (error) {
        if (request === segmentRequest && selected === state.selected) {
          state.segments = [];
          renderSegments();
        }
        throw error;
      } finally {
        if (request === segmentRequest) { state.segmentsLoading = false; render(); }
      }
    }

    async function selectDataset(id) {
      if (state.busy || state.selected === id || !discardDrafts()) return;
      state.selected = id;
      state.offset = 0;
      state.segments = [];
      state.uploads = [];
      $("uploads").replaceChildren();
      state.jobId = state.jobs.find(job => job.dataset_id === id)?.id || "";
      storageSet(win.localStorage, "voice-training-dataset", id);
      renderSegments();
      void refreshMetrics();
      try { await loadSegments(); } catch (error) { notice(error.message, true); }
    }

    async function refresh() {
      if (refreshing) return refreshing;
      refreshing = (async () => {
        try {
          const [datasets, jobs, cap, health] = await Promise.all([
            api("/api/training/datasets"), api("/api/training/jobs?limit=50"), api("/api/training/capabilities"), api("/api/health")]);
          const previousJobs = state.jobs;
          if (cap.active_job && !jobs.some(job => job.id === cap.active_job)) jobs.unshift(await api(path("jobs", cap.active_job)));
          if (state.jobId && !jobs.some(job => job.id === state.jobId)) {
            try { jobs.push(await api(path("jobs", state.jobId))); } catch { /* A missing historical task must not hide the rest of the page. */ }
          }
          state.datasets = datasets;
          state.jobs = jobs;
          const selectedTrainJob = state.jobs.find(job => job.id === state.jobId);
          if (selectedTrainJob?.kind === "train" && selectedTrainJob.status === "completed" && !selectedTrainJob.preview) {
            try {
              const detail = await api(path("jobs", selectedTrainJob.id));
              state.jobs = state.jobs.map(job => job.id === detail.id ? detail : job);
            } catch { /* Keep the list view when an old task has no preview record. */ }
          }
          state.capabilities = cap;
          state.health = health;
          state.connected = true;
          const saved = state.selected || storageGet(win.localStorage, "voice-training-dataset");
          const selected = datasets.find(item => item.id === saved)?.id || datasets[0]?.id || "";
          const changed = selected !== state.selected;
          state.selected = selected;
          if (changed) { state.offset = 0; state.drafts.clear(); state.segments = []; }
          if (!state.jobs.some(job => job.id === state.jobId)) state.jobId = jobs.find(job => job.dataset_id === selected)?.id || jobs[0]?.id || "";
          const finished = jobs.some(job => job.kind === "prepare" && job.dataset_id === selected && !ACTIVE.has(job.status)
            && previousJobs.some(previous => previous.id === job.id && ACTIVE.has(previous.status)));
          render();
          void refreshMetrics();
          if (changed || finished) {
            if (state.drafts.size) { state.needsSegments = true; render(); }
            else await loadSegments();
          }
        } catch (error) {
          state.connected = false;
          librarySignature = "";
          notice(error.message, true);
          render();
        }
      })();
      try { await refreshing; } finally { refreshing = null; }
    }

    async function perform(label, work) {
      if (state.busy) return false;
      state.busy = label;
      notice("");
      render();
      let succeeded = false;
      try {
        if (refreshing) await refreshing;
        await work();
        succeeded = true;
      } catch (error) {
        notice(error.message, true);
        if ($("create-dialog").open) {
          $("create-error").textContent = error.message;
          $("create-error").hidden = false;
        }
      } finally {
        await refresh();
        state.busy = "";
        render();
      }
      return succeeded;
    }

    function renderUploads() {
      $("uploads").replaceChildren();
      for (const file of state.uploads) {
        const row = node("li");
        row.append(node("span", "", file.name), node("span", file.failed ? "failed" : "done", file.message));
        $("uploads").append(row);
      }
    }

    async function uploadFiles(files) {
      if (!state.selected || state.busy || !state.connected || state.capabilities?.active_job) return;
      const selected = state.selected;
      const list = Array.from(files);
      if (!list.length) return;
      await perform("正在上传素材", async () => {
        for (const [index, file] of list.entries()) {
          $("upload-status").textContent = `正在上传第 ${index + 1} / ${list.length} 个文件：${file.name}`;
          const entry = {name: file.name, failed: false, message: "已上传"};
          try {
            if (!/\.(mp3|mp4|wav)$/i.test(file.name)) throw new Error("仅支持 MP3 / MP4 / WAV");
            if (file.size > 200 * 1024 ** 2) throw new Error("超过 200 MiB");
            if (!file.size) throw new Error("空文件");
            const body = new win.FormData();
            body.append("source", file, file.name);
            await api(`${path("datasets", selected)}/sources`, {method: "POST", body});
          } catch (error) { entry.failed = true; entry.message = error.message; }
          state.uploads.push(entry);
          renderUploads();
        }
        $("upload-status").textContent = "本次上传已结束。成功的文件已保存；失败的文件可重新选择。下一步点击处理素材并转写。";
        $("source-files").value = "";
      });
    }

    async function saveSegment(id, transcript, reviewStatus) {
      if (editingBlocked()) return;
      if (reviewStatus === "approved" && !transcript.trim()) { notice("请先填写并核对原文，再确认片段。", true); return; }
      await perform("正在保存片段", async () => {
        const saved = await api(path("segments", id), json("PATCH", {transcript, review_status: reviewStatus}));
        state.segments = state.segments.map(segment => segment.id === id ? saved : segment);
        state.drafts.delete(id);
        renderSegments();
        notice(reviewStatus === "approved" ? "片段已确认，将进入新任务的训练快照。" : reviewStatus === "excluded" ? "片段已排除，不进入新任务训练。" : "原文已保存，片段恢复为待审核。请核对后确认。");
      });
    }

    function trainingKey(config) {
      const fingerprint = JSON.stringify([state.selected, dataset()?.version, config]);
      let previous;
      try { previous = JSON.parse(storageGet(win.sessionStorage, "voice-training-request")); } catch { previous = null; }
      if (previous?.fingerprint === fingerprint && previous.key) return previous.key;
      const key = win.crypto.randomUUID();
      storageSet(win.sessionStorage, "voice-training-request", JSON.stringify({fingerprint, key}));
      return key;
    }

    async function train(config) {
      if (!canTrain()) return;
      if (!win.confirm("开始训练会暂时占用本机模型，聊天合成与识别需要等待。确定开始吗？")) return;
      const selected = state.selected, key = trainingKey(config);
      await perform("正在提交训练任务", async () => {
        const job = await api(`${path("datasets", selected)}/jobs`, json("POST", config, {"Idempotency-Key": key}));
        storageRemove(win.sessionStorage, "voice-training-request");
        state.jobs.unshift(job);
        state.jobId = job.id;
        notice("训练任务已提交。可以关闭页面，稍后回来查看进度；请勿退出后端应用。");
      });
    }

    function variantKey(jobId, steps) {
      const fingerprint = JSON.stringify([jobId, steps]);
      let previous;
      try { previous = JSON.parse(storageGet(win.sessionStorage, "voice-variant-request")); } catch { previous = null; }
      if (previous?.fingerprint === fingerprint && previous.key) return previous.key;
      const key = win.crypto.randomUUID();
      storageSet(win.sessionStorage, "voice-variant-request", JSON.stringify({fingerprint, key}));
      return key;
    }

    async function createVariant() {
      const job = currentJob();
      if (!job || job.kind !== "train" || job.status !== "completed" || state.busy) return;
      const raw = win.prompt("对照训练的目标步数（1–1000），原快照与其余参数保持不变：", String(job.config?.steps || 20));
      if (raw === null) return;
      const steps = Number(raw);
      if (!Number.isInteger(steps) || steps < 1 || steps > 1000) { notice("步数必须是 1 到 1000 的整数。", true); return; }
      if (!win.confirm("对照训练会复制原任务的数据快照与参数，仅改步数，从同一基座重新训练。确定继续吗？")) return;
      await perform("正在提交对照训练", async () => {
        const created = await api(`${path("jobs", job.id)}/variants`, json("POST", {steps}, {"Idempotency-Key": variantKey(job.id, steps)}));
        storageRemove(win.sessionStorage, "voice-variant-request");
        state.jobs.unshift(created);
        state.jobId = created.id;
        notice("对照训练已提交。它与原任务使用同一不可变快照，只改变步数。");
      });
    }

    async function jobAction(action, resume = true) {
      const job = currentJob();
      if (!job || state.busy) return;
      if (action === "cancel" && !win.confirm("确定取消此任务吗？当前片段或训练步骤退出后才会释放资源。")) return;
      if (action === "retry" && !win.confirm(resume ? "确定显式重试此任务吗？有兼容检查点时将恢复原任务。" : "确定放弃检查点，从原始快照重新训练吗？")) return;
      await perform(action === "cancel" ? "正在请求取消" : "正在请求重试", async () => {
        const updated = await api(`${path("jobs", job.id)}/${action}`, json("POST", action === "retry" ? {resume} : {}));
        state.jobs = state.jobs.map(entry => entry.id === updated.id ? updated : entry);
        notice(action === "cancel" ? "已请求取消。以任务最终状态为准，当前计算退出前仍会占用模型。" : "已提交重试请求，使用原任务的数据快照。");
      });
    }

    async function confirmPreview() {
      const job = currentJob();
      if (!job || job.kind !== "train" || job.status !== "completed" || state.busy) return;
      if (!$("preview-check").checked) { notice("请先逐条试听并勾选确认。", true); return; }
      await perform("正在保存试听确认", async () => {
        const preview = await api(`${path("jobs", job.id)}/preview-confirm`, json("POST", {
          confirmed: true, note: $("preview-note").value.trim()}));
        state.jobs = state.jobs.map(entry => entry.id === job.id ? {...entry, preview} : entry);
        notice("试听已确认。现在可以发布为新声音。");
      });
    }

    async function publish(name) {
      const job = currentJob();
      if (!job || job.kind !== "train" || job.status !== "completed" || state.busy) return;
      if (!job.preview?.confirmed) { notice("请先试听并确认两条验收样本，再发布声音。", true); return; }
      await perform("正在验证并发布声音", async () => {
        const voice = await api(`${path("jobs", job.id)}/publish`, json("POST", {name}));
        state.published.set(job.id, voice);
        notice("声音已发布为独立版本。可以在聊天项目中选择这个新声音，创建新会话试听。");
      });
    }

    function openCreate() {
      if (state.busy || !state.connected) return;
      $("create-error").hidden = true;
      $("create-dialog").showModal();
      $("dataset-name").focus();
    }

    function start() {
      for (const view of views) {
        const tab = $(`tab-${view}`);
        tab.addEventListener("click", () => setView(view));
        tab.addEventListener("keydown", event => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const available = views.filter(name => !$(`tab-${name}`).disabled);
          const index = available.indexOf(view);
          const next = event.key === "Home" ? 0 : event.key === "End" ? available.length - 1 :
            (index + (event.key === "ArrowRight" ? 1 : -1) + available.length) % available.length;
          setView(available[next], true);
        });
      }
      $("new-dataset").addEventListener("click", openCreate);
      $("welcome-create").addEventListener("click", openCreate);
      $("close-create").addEventListener("click", () => $("create-dialog").close());
      $("create-dialog").addEventListener("cancel", event => { if (state.busy) event.preventDefault(); });
      $("create-form").addEventListener("submit", async event => {
        event.preventDefault();
        const name = $("dataset-name").value.trim();
        if (!$("create-form").reportValidity()) return;
        if (!name) {
          $("create-error").textContent = "请输入素材集名称，不能只填写空格。";
          $("create-error").hidden = false;
          return;
        }
        if (!discardDrafts()) return;
        await perform("正在创建素材集", async () => {
          const created = await api("/api/training/datasets", json("POST", {name, speaker_confirmed: false}));
          state.datasets.unshift(created);
          state.selected = created.id;
          state.view = "import";
          state.jobId = "";
          state.offset = 0;
          state.segments = [];
          state.uploads = [];
          $("uploads").replaceChildren();
          renderSegments();
          storageSet(win.localStorage, "voice-training-dataset", created.id);
          $("create-dialog").close();
          $("create-form").reset();
          notice("素材集已创建。请选择或拖入 MP3 / MP4 素材。");
        });
      });
      $("source-files").addEventListener("change", event => uploadFiles(event.target.files));
      const drop = $("drop-zone");
      drop.addEventListener("dragover", event => { event.preventDefault(); if (!$("source-files").disabled) drop.classList.add("dragging"); });
      drop.addEventListener("dragleave", () => drop.classList.remove("dragging"));
      drop.addEventListener("drop", event => { event.preventDefault(); drop.classList.remove("dragging"); uploadFiles(event.dataTransfer.files); });
      $("prepare").addEventListener("click", async () => {
        if (!discardDrafts()) return;
        const selected = state.selected;
        await perform("正在提交素材处理任务", async () => {
          const job = await api(`${path("datasets", selected)}/prepare`, {method: "POST"});
          state.jobs.unshift(job);
          state.jobId = job.id;
          state.view = "training";
          notice("素材处理已开始。转写完成后，请逐片试听和审核。");
        });
      });
      $("speaker-confirmed").addEventListener("change", async event => {
        const selected = state.selected, confirmed = event.target.checked;
        await perform("正在保存说话者确认", async () => {
          await api(path("datasets", selected), json("PATCH", {speaker_confirmed: confirmed}));
          notice(confirmed ? "已确认单一目标说话者。" : "已取消说话者确认，无法启动新训练。");
        });
      });
      const page = async offset => {
        if (state.busy || !discardDrafts()) return;
        state.offset = offset;
        try { await loadSegments(); } catch (error) { notice(error.message, true); }
      };
      $("refresh-segments").addEventListener("click", () => page(state.offset));
      $("previous-segments").addEventListener("click", () => page(Math.max(0, state.offset - PAGE_SIZE)));
      $("next-segments").addEventListener("click", () => page(state.offset + PAGE_SIZE));
      $("refresh").addEventListener("click", async () => { notice(""); await refresh(); });
      $("train-form").addEventListener("submit", event => {
        event.preventDefault();
        if (!$("train-form").reportValidity()) return;
        const config = Object.fromEntries(Array.from(new win.FormData($("train-form")), ([key, value]) => [key, Number(value)]));
        train(config);
      });
      for (const id of ["job-select", "comparison-job-select"]) {
        $(id).addEventListener("change", event => { state.jobId = event.target.value; renderJob(); renderComparison(); void refreshMetrics(); });
      }
      $("dataset-metrics-action").addEventListener("click", () => postMetric("dataset", state.selected, state.selected));
      $("job-metrics-action").addEventListener("click", () => {
        const job = currentJob();
        if (job && job.dataset_id === state.selected && job.kind === "train" && job.status === "completed") return postMetric("job", job.id, state.selected);
      });
      $("cancel-job").addEventListener("click", () => jobAction("cancel"));
      $("retry-job").addEventListener("click", () => jobAction("retry", true));
      $("restart-job").addEventListener("click", () => jobAction("retry", false));
      $("variant-job").addEventListener("click", createVariant);
      $("preview-check").addEventListener("change", renderJob);
      $("confirm-preview").addEventListener("click", () => confirmPreview());
      $("publish-form").addEventListener("submit", event => {
        event.preventDefault();
        if ($("publish-form").reportValidity()) publish($("voice-name").value.trim());
      });
      $("copy-voice").addEventListener("click", async () => {
        try { await win.navigator.clipboard.writeText($("published-id").value); notice("声音 UUID 已复制。"); }
        catch { $("published-id").focus(); $("published-id").select(); notice("无法自动复制，已选中声音 UUID，请手动复制。"); }
      });
      win.addEventListener("beforeunload", event => {
        if (state.drafts.size || state.busy) { event.preventDefault(); event.returnValue = ""; }
      });
      async function poll() {
        if (disposed) return;
        if (!state.busy && !doc.hidden) await refresh();
        if (!disposed) pollTimer = win.setTimeout(poll, 2500);
      }
      render();
      poll();
    }

    function dispose() { disposed = true; win.clearTimeout(pollTimer); }
    return {state, start, dispose, refresh, refreshMetrics, selectDataset, loadSegments, uploadFiles, saveSegment, train, createVariant, jobAction, confirmPreview, publish, canTrain};
  }

  if (typeof module !== "undefined" && module.exports) module.exports = {createWorkbench, formatDuration, errorMessage};
  if (typeof document !== "undefined") createWorkbench(document, window).start();
})();
