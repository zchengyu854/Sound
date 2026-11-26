"use strict";
const $ = id => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";
const ACTIVE = new Set(["queued", "thinking", "replying", "synthesizing"]);
const TERMINAL = new Set(["complete", "text_failed", "audio_failed"]);
const state = {
  characters: [], voices: [], conversations: [], current: null, health: null, messages: [],
  signatures: new Map(), thinking: new Map(), streams: new Map(), noStream: new Set(),
  listSignature: "", voiceSignature: "", online: false, pinned: true, stopping: null
};
let pendingPrompt = "";
let homePreviewSignature = "";
let indexSignature = "";
let homeMemoryLoaded = 0;
// Adaptive polling: fast while work is in flight, slow when idle, paused while hidden.
const POLL_ACTIVE = 1200;
const POLL_IDLE = 4000;
const POLL_OFFLINE = 6000;
let renderFrame = 0;
let healthFrame = 0;
let toastTimer;
let pollTimer = 0;
let polling = false;
const player = new VoiceAudio.Player(updatePlayback, toast);
const recorder = new VoiceRecorder(renderHealth, text => {
  if (!state.current || $("message-input").value) return;
  $("message-input").value = text;
  autosizeInput();
  $("message-input").focus();
  renderHealth();
}, toast);

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}
function icon(d, viewBox = "0 0 24 24") {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", viewBox);
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG, "path");
  path.setAttribute("d", d);
  svg.append(path);
  return svg;
}
function toast(message) {
  $("toast-text").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 4000);
}
async function api(path, {timeoutMs = 30000, ...options} = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`/api/${path}`, {...options, signal: controller.signal});
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(typeof data.detail === "string" ? data.detail : "输入不符合要求，请检查后重试。");
      error.status = response.status;
      error.memoryConflict = response.headers.get("X-Memory-Conflict");
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("请求超时，请刷新确认是否已保存，再重试。");
    if (error instanceof TypeError) throw new Error("无法连接本地服务，请确认应用仍在运行。");
    throw error;
  } finally { clearTimeout(timeout); }
}
const post = (path, data) => api(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)});
function currentConversation() { return state.conversations.find(c => c.id === state.current); }
function profileOf(conversation) { return conversation?.character_snapshot ? JSON.parse(conversation.character_snapshot) : null; }
function displayName(conversation) { return profileOf(conversation)?.name || conversation?.voice_name || "AI"; }
function voiceName() { return displayName(currentConversation()); }
function pauseAudio() {
  player.stop();
  document.querySelectorAll("audio").forEach(audio => audio.pause());
}
function updatePlayback() {
  document.querySelectorAll("[data-stream-id]").forEach(button => {
    button.textContent = player.playing(button.dataset.streamId) ? "停止试听" : "试听生成中的语音";
    button.hidden = !player.playing(button.dataset.streamId) && state.messages.find(message => message.id === button.dataset.streamId)?.status !== "synthesizing";
  });
}
function activeMessage() { return state.messages.filter(message => message.role === "assistant" && ACTIVE.has(message.status)).at(-1); }
function scheduleRender() {
  if (renderFrame) return;
  renderFrame = requestAnimationFrame(() => { renderFrame = 0; renderMessages(); });
}
function scheduleHealth() {
  if (healthFrame) return;
  healthFrame = requestAnimationFrame(() => { healthFrame = 0; renderHealth(); });
}
function hashText(value) {
  const text = typeof value === "string" ? value : "";
  let hash = 0;
  for (let index = 0; index < text.length; index++) hash = (hash * 31 + text.charCodeAt(index)) | 0;
  return hash;
}
function messageSignature(message) {
  return [message.id, message.status, message.attempt ?? "", message.duration ?? "",
    hashText(message.content), hashText(message.reasoning), hashText(message.warning), hashText(message.error),
    state.thinking.get(message.id) ? 1 : 0].join("|");
}
function autosizeInput() {
  const input = $("message-input");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}

function shortDate(value) {
  const date = new Date(value);
  return `${String(date.getMonth() + 1).padStart(2, "0")}.${String(date.getDate()).padStart(2, "0")}`;
}
function clipText(value, limit = 46) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}
function greetingText(hour = new Date().getHours()) {
  if (hour < 5) return "夜深了";
  if (hour < 11) return "早上好";
  if (hour < 14) return "中午好";
  if (hour < 18) return "下午好";
  return "晚上好";
}
function primaryCharacter() { return state.characters[0] || null; }
function primaryVoice() {
  const character = primaryCharacter();
  return (character && state.voices.find(voice => voice.id === character.voice_id)) || state.voices[0] || null;
}
function renderHome() {
  const character = primaryCharacter();
  const voice = primaryVoice();
  const reference = voice?.duration ? `${voice.duration}s 参考` : "无参考录音";
  document.querySelectorAll("[data-character-count]").forEach(element => { element.textContent = String(state.characters.length).padStart(2, "0"); });
  document.querySelectorAll("[data-voice-count]").forEach(element => { element.textContent = String(state.voices.length).padStart(2, "0"); });
  $("pulse-sessions").textContent = String(state.conversations.length).padStart(2, "0");
  $("pulse-last").textContent = state.conversations.length ? `最近 ${shortDate(state.conversations[0].updated_at)}` : "还没有留下日期";

  $("sidebar-voice-name").textContent = voice ? voice.name : character ? character.name : "尚未添加声音";
  $("sidebar-voice-meta").textContent = voice ? `${reference} · 只在本机` : "添加录音或导入训练声音";
  $("sidebar-voice-tag").textContent = state.voices.length ? `${String(state.voices.length).padStart(2, "0")} VOICES` : "VOICE";

  $("composer-to").textContent = character?.name || "她";
  $("composer-voice").textContent = voice ? `${voice.name} · ${reference}` : "等待一段声音";
  $("home-eyebrow").textContent = character ? `${character.name} · 会生长的声音记忆` : "声音记忆";
  $("home-lede").textContent = voice && character
    ? `和${character.name}继续聊天，曾经聊过的事可以留在人物记忆里。`
    : voice ? "选一个人物，或用已有的声音开始对话。" : "添加参考录音或导入训练声音后，即可开始对话。";

  $("rail-monogram").textContent = character ? (character.name || "她").slice(0, 1) : "◦";
  $("rail-character-name").textContent = character ? character.name : "还没有人物";
  $("rail-character-meta").textContent = character ? (character.relationship || character.personality || "本地人物") : "创建人物，声音才会留下";
  $("rail-voice-meta").querySelector("b").textContent = voice ? reference : "等待添加";

  const groups = new Map();
  for (const conversation of state.conversations) {
    const day = shortDate(conversation.updated_at);
    groups.set(day, (groups.get(day) || 0) + 1);
  }
  const rows = [...groups.entries()].slice(0, 3);
  const signature = JSON.stringify(rows);
  if (signature !== indexSignature) {
    indexSignature = signature;
    $("index-rows").replaceChildren();
    for (const [day, count] of rows) {
      const row = node("div");
      row.append(node("b", "", day), node("span", "", String(count).padStart(2, "0")));
      $("index-rows").append(row);
    }
    if (!rows.length) $("index-rows").append(node("p", "index-empty", "还没有可索引的回忆。"));
  }
  $("index-note").textContent = rows.length ? `共 ${state.conversations.length} 段 · 最近的 ${rows.length} 个日期` : "按日期归档，只统计本机已有的对话。";
}
function loadHomePreview() {
  const latest = state.conversations[0];
  $("home-resume").hidden = !latest;
  if (!latest) {
    homePreviewSignature = "";
    return;
  }
  $("resume-date").textContent = shortDate(latest.updated_at);
  if (homePreviewSignature === latest.id) return;
  homePreviewSignature = latest.id;
  $("resume-quote").textContent = latest.title;
  api(`conversations/${latest.id}/messages`).then(messages => {
    if (state.current || state.conversations[0]?.id !== latest.id) return;
    const last = [...messages].reverse().find(message => message.role === "assistant" && message.content)
      || [...messages].reverse().find(message => message.content);
    $("resume-quote").textContent = last ? `“${clipText(last.content)}”` : "这段对话还没有留下文字。";
  }).catch(() => {
    if (homePreviewSignature === latest.id) homePreviewSignature = "";
  });
}
async function loadHomeMemory(force = false) {
  const character = primaryCharacter();
  if (!character) {
    document.querySelectorAll("[data-memory-count]").forEach(element => { element.textContent = "00"; });
    $("rail-memory-count").textContent = "00";
    $("rail-memory-title").textContent = "还没有记忆";
    $("rail-memory-note").textContent = "可以手动添加记忆";
    return;
  }
  if (!force && Date.now() - homeMemoryLoaded < 30000) return;
  homeMemoryLoaded = Date.now();
  try {
    const data = await api(`characters/${character.id}/memories`);
    if (character.id !== primaryCharacter()?.id) return;
    const count = data.items.length;
    document.querySelectorAll("[data-memory-count]").forEach(element => { element.textContent = String(count).padStart(2, "0"); });
    $("rail-memory-count").textContent = String(count).padStart(2, "0");
    $("rail-memory-title").textContent = count ? `${count} 条记忆` : "还没有记忆";
    $("rail-memory-note").textContent = data.status === "updating" ? "正在从对话里提炼…" : count ? "已确认的记忆在人物会话间共享" : "确认建议后，记忆会出现在这里";
  } catch (_) { /* Home stats stay quiet when memory is unavailable. */ }
}
function setHome(home) {
  if (!home) $("app").removeAttribute("data-intro");
  $("app").dataset.home = String(home);
  $(home ? "home-network-start" : "chat-network").append($("neural-tree"));
  $("top-mode").textContent = home ? "MEMORY ROOM" : "CONVERSATION";
  $("timeline").focus();
}
function goHome() {
  pauseAudio();
  recorder.cancel();
  closeStreams();
  state.noStream.clear();
  state.current = null;
  state.messages = [];
  state.signatures.clear();
  state.pinned = true;
  homePreviewSignature = "";
  $("messages").replaceChildren();
  $("message-input").value = "";
  autosizeInput();
  setHome(true);
  renderLists();
  renderMessages();
  renderHealth();
  loadHomePreview();
  loadHomeMemory(true);
}
function beginConversation(prompt = "") {
  pendingPrompt = prompt;
  openChat();
}

function renderLists() {
  const signature = JSON.stringify(state.conversations) + state.current;
  if (signature !== state.listSignature) {
    state.listSignature = signature;
    $("conversations").replaceChildren();
    if (!state.conversations.length) $("conversations").append(node("p", "list-empty", "还没有会话。添加声音后，\n在这里找回每一次对话。"));
    for (const conversation of state.conversations) {
      const button = node("button", `conversation${conversation.id === state.current ? " active" : ""}`);
      button.type = "button";
      button.setAttribute("aria-current", conversation.id === state.current ? "true" : "false");
      button.append(node("span", "avatar", displayName(conversation).slice(0, 1)));
      const copy = node("span", "conversation-copy");
      copy.append(node("strong", "", conversation.title), node("small", "", displayName(conversation)));
      const time = node("time", "", new Date(conversation.updated_at).toLocaleDateString("zh-CN", {month: "numeric", day: "numeric"}));
      button.append(copy, time);
      button.addEventListener("click", () => selectConversation(conversation.id));
      $("conversations").append(button);
    }
  }
  const total = String(state.conversations.length).padStart(2, "0");
  document.querySelectorAll("[data-count]").forEach(element => { element.textContent = total; });
  const voicesSignature = JSON.stringify([state.voices, state.characters.map(c => [c.id, c.name])]);
  if (voicesSignature !== state.voiceSignature) {
    state.voiceSignature = voicesSignature;
    for (const select of [$("voice-select"), $("new-voice-select")]) {
      select.replaceChildren();
      for (const character of state.characters) select.add(new Option(character.name, `c:${character.id}`));
      for (const voice of state.voices) select.add(new Option(`仅声音：${voice.name}`, `v:${voice.id}`));
      if (!select.options.length) select.add(new Option("先添加声音并创建人物", ""));
    }
  }
  const conversation = currentConversation();
  const realtimeLink = $("realtime-link");
  const realtimeCharacter = conversation ? conversation.character_id : primaryCharacter()?.id;
  const realtimeVoice = conversation ? conversation.voice_id : primaryVoice()?.id;
  realtimeLink.href = realtimeCharacter
    ? `/realtime/?character_id=${encodeURIComponent(realtimeCharacter)}`
    : realtimeVoice ? `/realtime/?voice_id=${encodeURIComponent(realtimeVoice)}` : "/realtime/";
  $("voice-select").value = conversation ? (conversation.character_id ? `c:${conversation.character_id}` : `v:${conversation.voice_id}`) : (state.characters[0] ? `c:${state.characters[0].id}` : state.voices[0] ? `v:${state.voices[0].id}` : "");
  $("voice-select").disabled = !state.voices.length;
  $("chat-title").textContent = conversation ? displayName(conversation) : "创建一个有声音的人物";
  $("conversation-summary").hidden = !conversation;
  $("character-settings").hidden = $("character-memories").hidden = $("character-evolution").hidden = !conversation?.character_id;
  $("chat-subtitle").textContent = conversation ? `声音：${conversation.voice_name}${conversation.character_id ? " · 虚拟人物 · 本会话设定已固定" : ""}` : "添加参考录音，选择声音开始聊天";
  renderHome();
}

function renderHealth() {
  const health = state.health;
  let text = "正在连接本地服务…", warning = false, quiet = false;
  if (!state.online) { text = "无法连接本地服务，正在重连。请确认终端中的应用仍在运行。"; warning = true; }
  else if (health?.model_status === "loading") text = "Breeze 模型加载中，首次启动可能需要下载权重。你可以先添加声音。";
  else if (health?.model_status === "failed") { text = health.model_error; warning = true; }
  else if (!health?.llm_configured) { text = "尚未配置聊天模型：请打开右上角“设置”，填写并保存大模型配置。"; warning = true; }
  else if (health?.busy) text = "正在生成回复，思考与文字会实时显示。";
  else { text = "本地语音已就绪。AI 回复生成后，点击语音手动播放。"; quiet = true; }
  $("service-status").textContent = text;
  $("service-status").classList.toggle("warning", warning);
  $("service-status").hidden = quiet;
  $("intro-status").textContent = !state.online ? "正在重连本地服务"
    : health?.model_status === "loading" ? "正在加载并预热语音模型…"
    : health?.model_status === "failed" ? "语音模型加载失败"
    : health?.stt_status === "loading" ? "正在加载语音识别模型…"
    : health?.busy ? "正在生成回复"
    : !health?.llm_configured ? "等待配置聊天模型"
    : $("app").hasAttribute("data-intro") ? "正在打开声音记忆" : "本地语音已就绪";
  $("nav-dot").className = `nav-dot ${state.online ? (warning ? "warning" : "online") : ""}`;
  const voiceReady = state.online && health?.model_status === "ready";
  const setRailState = (id, active, text) => {
    $(id).classList.toggle("active", active);
    $(`${id}-text`).textContent = text;
  };
  setRailState("state-voice", voiceReady, !state.online ? "离线" : health?.model_status === "failed" ? "异常" : voiceReady ? "就绪" : "加载中");
  setRailState("state-llm", !!health?.llm_configured, health?.llm_configured ? "已配置" : "未配置");
  setRailState("state-local", state.online, state.online ? "已连接" : "离线");
  $("state-label").textContent = !state.online ? "OFFLINE" : recorder.phase === "recording" ? "LISTEN" : health?.busy ? "THINKING" : "IDLE";
  $("top-status-text").textContent = state.online ? "只在本机" : "本地服务离线";
  $("top-status").classList.toggle("warning", !state.online);
  $("local-status").textContent = state.online ? "已连接" : "未连接";
  $("local-model").textContent = health?.model_status === "ready" ? "已加载" : health?.model_status === "failed" ? "加载失败" : "加载中";
  $("local-llm").textContent = health?.llm_configured ? "已配置" : "未配置";
  $("local-busy").textContent = health?.active_kind === "transcription" ? "正在识别" : health?.active_kind === "loading_stt" ? "加载识别模型" : health?.busy ? "正在生成" : "空闲";
  $("local-stt").textContent = health?.stt_status === "ready" ? "已加载" : health?.stt_status === "failed" ? "加载失败" : "加载中";

  const lastAssistant = state.messages.filter(m => m.role === "assistant").at(-1);
  const pending = state.messages.some(m => ACTIVE.has(m.status));
  const failedText = lastAssistant?.status === "text_failed";
  const available = state.online && !!state.current && health?.model_status === "ready" && health?.llm_configured && !health?.busy && !pending && !failedText;
  const recording = recorder.phase !== "idle";
  $("message-input").disabled = recording;
  $("send").disabled = !available || recording || !$("message-input").value.trim();
  $("record").disabled = recorder.phase === "transcribing" || (!recording && (!recorder.supported || !state.current || !state.online || health?.stt_status !== "ready" || health?.busy || pending || !!$("message-input").value));
  $("record").textContent = recorder.phase === "recording" ? "松开识别" : recorder.phase === "transcribing" ? "正在识别…" : "按住说话";
  $("record").setAttribute("aria-pressed", String(recording));
  $("recording-status").textContent = recorder.phase === "requesting" ? "请允许麦克风权限，然后按住录音。" : recorder.phase === "recording" ? "正在录音，松开后识别，最长30秒。" : recorder.phase === "transcribing" ? "正在本机识别，完成后可编辑再发送。" : !recorder.supported ? "当前浏览器不支持录音，请使用文字输入。" : health?.stt_status === "failed" ? health.stt_error : "";
  $("composer-hint").textContent = failedText ? "请先重试上一条文字回复，或新建会话" : pending ? "正在处理，可以先写下下一条消息" : "聊天文字会发送至你配置的大模型服务";
  const active = activeMessage();
  if (!active) state.stopping = null;
  const stop = $("stop");
  stop.hidden = !active || !state.online;
  stop.disabled = !active || !state.online || state.stopping === active?.id;
  stop.textContent = active && state.stopping === active.id ? "停止中…" : "停止";
  document.querySelectorAll(".retry").forEach(button => { button.disabled = !state.online || health?.busy || health?.model_status !== "ready"; });
}

function thinkingBlock(message) {
  const live = ACTIVE.has(message.status);
  const open = state.thinking.has(message.id) ? state.thinking.get(message.id) : false;
  const box = node("div", "thinking");
  box.dataset.open = String(open);
  box.dataset.live = String(live);
  const head = node("button", "thinking-head");
  head.type = "button";
  head.setAttribute("aria-expanded", String(open));
  head.setAttribute("aria-label", open ? "收起思考过程" : "展开思考过程");
  head.append(node("span", "thinking-pulse"), node("span", "thinking-label", live ? "THINKING" : "THINKING · DONE"));
  head.append(node("span", "thinking-hint", open ? "收起" : "展开"));
  const chev = icon("M6 9l6 6 6-6");
  chev.classList.add("thinking-chev");
  head.append(chev);
  head.addEventListener("click", () => {
    state.pinned = false;
    state.thinking.set(message.id, !(state.thinking.get(message.id) ?? false));
    state.signatures.delete(message.id);
    renderMessages();
  });
  const body = node("div", "thinking-body");
  const copy = node("p", "", message.reasoning);
  body.append(copy);
  box.append(head, body);
  return box;
}

function replyParagraphs(content) {
  const paragraphs = [];
  for (const block of VoiceReply.blocks(content)) {
    const paragraph = node("p", "reply-paragraph");
    for (const token of VoiceReply.tokens(block)) paragraph.append(node(token.strong ? "strong" : "span", "", token.text));
    paragraphs.push(paragraph);
  }
  return paragraphs;
}
function replyCard(content) {
  const card = node("div", "reply-card");
  card.dataset.contentHash = String(hashText(content));
  card.append(...replyParagraphs(content));
  return card;
}
function streamPreview(message) {
  const preview = node("button", "stream-preview", player.playing(message.id) ? "停止试听" : "试听生成中的语音");
  preview.type = "button";
  preview.dataset.streamId = message.id;
  preview.disabled = !(window.AudioContext && window.ReadableStream);
  preview.addEventListener("click", () => {
    if (player.playing(message.id)) { player.stop(); return; }
    pauseAudio();
    player.play(message.id, message.attempt);
  });
  return preview;
}

function renderMessage(message) {
  const user = message.role === "user";
  const article = node("article", `message ${user ? "user" : "assistant"}`);
  article.id = `message-${message.id}`;
  const body = node("div", "message-body");
  const time = new Date(message.created_at).toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"});
  body.append(node("div", "message-meta", `${user ? "我" : voiceName()} · ${time}`));
  if (!user && message.reasoning) body.append(thinkingBlock(message));
  if (message.content) {
    if (user) body.append(node("div", "bubble", message.content));
    else body.append(replyCard(message.content));
  }
  if (ACTIVE.has(message.status)) {
    const labels = {queued: "等待处理…", thinking: "正在思考…", replying: "正在回复…", synthesizing: "正在合成语音…"};
    const status = node("div", "message-state", labels[message.status]);
    status.setAttribute("role", "status");
    body.append(status);
  }
  if (message.status.endsWith("_failed")) {
    const failure = node("div", "message-state failed");
    failure.append(node("span", "state-label", message.status === "audio_failed" ? "合成失败" : "回复失败"));
    failure.append(node("span", "error", message.error));
    const retry = node("button", "retry", message.status === "audio_failed" ? "重试合成" : "重试回复");
    retry.type = "button";
    retry.addEventListener("click", async () => {
      retry.disabled = true;
      try { await post(`messages/${message.id}/retry`, {}); await refresh(); }
      catch (error) { toast(error.message); retry.disabled = false; }
    });
    failure.append(retry);
    body.append(failure);
  }
  if (message.warning) body.append(node("p", "message-warning", message.warning));
  if (!user && (message.status === "synthesizing" || player.playing(message.id))) {
    body.append(streamPreview(message));
  }
  if (!user && message.status === "complete") {
    const card = node("div", "voice-card"), header = node("div", "voice-card-header");
    header.append(node("span", "", `语音回复 · ${Number(message.duration).toFixed(1)} 秒`));
    const download = node("a", "", "下载 WAV");
    download.href = `/api/audio/${message.id}?download=true`;
    download.download = `${message.id}.wav`;
    header.append(download);
    const audio = node("audio");
    audio.controls = true;
    audio.preload = "none";
    audio.src = `/api/audio/${message.id}`;
    audio.setAttribute("aria-label", `${voiceName()}的语音回复`);
    audio.addEventListener("play", () => {
      player.stop();
      document.querySelectorAll("audio").forEach(other => { if (other !== audio) other.pause(); });
    });
    audio.addEventListener("error", () => { if (!card.querySelector(".error")) card.append(node("p", "error", "音频无法播放，请检查服务或下载 WAV。")); });
    card.append(header, audio);
    body.append(card);
  }
  article.append(node("span", "avatar", user ? "我" : voiceName().slice(0, 1)), body);
  article.dataset.status = message.status;
  return article;
}

function canPatchMessage(article, message) {
  return ACTIVE.has(article.dataset.status) && ACTIVE.has(message.status);
}
function patchThinking(article, message) {
  const block = article.querySelector(".thinking");
  if (!message.reasoning) return !block;
  if (!block) return false;
  const open = state.thinking.get(message.id) ?? false;
  const live = ACTIVE.has(message.status);
  block.dataset.open = String(open);
  block.dataset.live = String(live);
  const head = block.querySelector(".thinking-head");
  const copy = block.querySelector(".thinking-body p");
  if (!head || !copy) return false;
  head.setAttribute("aria-expanded", String(open));
  head.setAttribute("aria-label", open ? "收起思考过程" : "展开思考过程");
  const label = head.querySelector(".thinking-label"), hint = head.querySelector(".thinking-hint");
  if (label) label.textContent = live ? "THINKING" : "THINKING · DONE";
  if (hint) hint.textContent = open ? "收起" : "展开";
  if (copy.textContent !== message.reasoning) copy.textContent = message.reasoning;
  return true;
}
// Repaint one streaming message in place; rebuilding the whole article on
// every token would drop focus and recreate controls dozens of times a second.
function patchMessage(article, message) {
  const body = article.querySelector(".message-body");
  if (!body || !patchThinking(article, message)) return false;
  const user = message.role === "user";
  if (message.content) {
    if (user) {
      const bubble = body.querySelector(".bubble");
      if (!bubble) return false;
      if (bubble.textContent !== message.content) bubble.textContent = message.content;
    } else {
      const card = body.querySelector(".reply-card");
      if (!card) return false;
      const hash = String(hashText(message.content));
      if (card.dataset.contentHash !== hash) {
        card.dataset.contentHash = hash;
        card.replaceChildren(...replyParagraphs(message.content));
      }
    }
  } else if (body.querySelector(".bubble, .reply-card")) {
    return false;
  }
  const status = body.querySelector(".message-state:not(.failed)");
  if (!status) return false;
  const labels = {queued: "等待处理…", thinking: "正在思考…", replying: "正在回复…", synthesizing: "正在合成语音…"};
  const label = labels[message.status];
  if (label && status.textContent !== label) status.textContent = label;
  if (message.warning) {
    let warning = body.querySelector(".message-warning");
    if (!warning) {
      warning = node("p", "message-warning", message.warning);
      body.insertBefore(warning, body.querySelector(".stream-preview"));
    } else if (warning.textContent !== message.warning) {
      warning.textContent = message.warning;
    }
  } else if (body.querySelector(".message-warning")) {
    return false;
  }
  if (!user && message.status === "synthesizing" && !body.querySelector(".stream-preview")) {
    body.append(streamPreview(message));
  }
  article.dataset.status = message.status;
  return true;
}

function renderMessages() {
  const timeline = $("timeline");
  const hasMessages = state.messages.length > 0;
  $("empty-state").hidden = hasMessages;
  if (!hasMessages) {
    const copy = $("empty-copy"), add = $("empty-add"), note = document.querySelector(".empty-note");
    if (state.current) {
      copy.textContent = "写下第一条消息。\nAI 的思考过程、文字回复和语音会依次出现在这里。";
      add.hidden = true;
      note.hidden = true;
    } else if (state.voices.length) {
      copy.textContent = "选择一个声音，开始新的对话。\n聊天文字、人物设定与相关记忆会发给配置的 LLM，语音合成留在本机。";
      add.hidden = false;
      $("empty-add-label").textContent = "新建会话";
      note.hidden = true;
    } else {
      copy.textContent = "一段清晰的录音，一份对应的原文。\n然后，让这个声音读出 AI 的回复。";
      add.hidden = false;
      $("empty-add-label").textContent = "添加声音";
      note.hidden = false;
    }
  }
  for (const message of state.messages) {
    const signature = messageSignature(message);
    if (state.signatures.get(message.id) === signature) continue;
    const existing = $(`message-${message.id}`);
    if (existing && canPatchMessage(existing, message) && patchMessage(existing, message)) {
      state.signatures.set(message.id, signature);
      continue;
    }
    const rendered = renderMessage(message);
    if (existing) existing.replaceWith(rendered); else $("messages").append(rendered);
    state.signatures.set(message.id, signature);
  }
  if (state.pinned && state.messages.length) timeline.scrollTop = timeline.scrollHeight;
}

function openStream(id) {
  if (state.streams.has(id) || state.noStream.has(id) || !("EventSource" in window)) return;
  const source = new EventSource(`/api/messages/${id}/events`);
  state.streams.set(id, source);
  source.onmessage = event => {
    let row;
    try { row = JSON.parse(event.data); } catch (_) { return; }
    const index = state.messages.findIndex(message => message.id === row.id);
    if (index < 0) return;
    state.messages[index] = row;
    if (row.status.endsWith("_failed") && player.playing(row.id)) {
      player.stop();
      toast("语音生成失败，试听已停止；可重试合成。");
    }
    state.signatures.delete(id);
    scheduleRender();
    if (TERMINAL.has(row.status)) {
      closeStream(id);
      refresh();
    }
  };
  source.onerror = () => {
    state.noStream.add(id);
    closeStream(id);
  };
}
function closeStream(id) {
  const source = state.streams.get(id);
  if (source) source.close();
  state.streams.delete(id);
}
function closeStreams() {
  for (const id of [...state.streams.keys()]) closeStream(id);
}

async function selectConversation(id) {
  pauseAudio();
  recorder.cancel();
  closeStreams();
  state.noStream.clear();
  state.current = id;
  state.messages = [];
  state.pinned = true;
  state.signatures.clear();
  setHome(false);
  $("messages").replaceChildren();
  $("message-input").value = "";
  autosizeInput();
  try { localStorage.setItem("voice-current-conversation", id); } catch (_) { /* Storage can be disabled. */ }
  renderLists(); renderMessages(); renderHealth();
  await refresh();
  if (pendingPrompt) {
    $("message-input").value = pendingPrompt;
    pendingPrompt = "";
    autosizeInput();
    renderHealth();
    $("message-input").focus();
  }
}
async function newConversation(selection) {
  const [kind, id] = selection.includes(":") ? selection.split(":") : ["v", selection];
  const conversation = await post("conversations", kind === "c" ? {character_id: id} : {voice_id: id});
  state.conversations = await api("conversations");
  await selectConversation(conversation.id);
  return conversation.id;
}
async function refresh() {
  const greeting = $("home-greeting");
  const text = greetingText();
  if (greeting.textContent !== text) greeting.textContent = text;
  const selected = state.current;
  try {
    const [health, voices, conversations, messages, characters] = await Promise.all([
      api("health"), api("voices"), api("conversations"), selected ? api(`conversations/${selected}/messages`) : Promise.resolve([]), api("characters")
    ]);
    state.health = health; state.characters = characters; state.voices = voices; state.conversations = conversations; state.online = true;
    showStartupIntro(health.startup_id);
    if (selected === state.current) {
      state.messages = messages.map(message => state.streams.has(message.id)
        ? state.messages.find(current => current.id === message.id) || message
        : message);
      for (const message of state.messages) {
        if (message.role === "assistant" && ACTIVE.has(message.status)) openStream(message.id);
      }
    }
    renderLists(); renderMessages(); renderHealth();
    if (!state.current) { loadHomePreview(); loadHomeMemory(); }
    if ($("memory-dialog").open) await loadMemories();
    if ($("summary-dialog").open) await loadSummary();
    if ($("evolution-dialog").open) await loadEvolution();
  } catch (error) {
    state.online = false;
    renderHealth();
  }
}

function openVoice() {
  $("voice-error").textContent = "";
  const selection = $("tone-voice");
  const chosen = selection.value || primaryVoice()?.id;
  const trainedVoices = state.voices.filter(voice => voice.adapter_id || voice.model_id);
  selection.replaceChildren(...trainedVoices.map(voice => new Option(voice.name, voice.id)));
  if (trainedVoices.some(voice => voice.id === chosen)) selection.value = chosen;
  $("voice-tone-settings").hidden = !trainedVoices.length;
  $("trained-voice-empty").hidden = !!trainedVoices.length;
  $("voice-reference-settings").open = false;
  renderVoiceTone();
  if (!$("voice-dialog").open) $("voice-dialog").showModal();
}
function renderVoiceTone() {
  const selected = state.voices.find(voice => voice.id === $("tone-voice").value);
  $("voice-tone").value = selected?.tone_preset || "default";
  $("voice-tone-error").textContent = "";
}
$("tone-voice").addEventListener("change", renderVoiceTone);
$("voice-tone-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = $("save-voice-tone");
  button.disabled = true;
  $("voice-tone-error").textContent = "";
  try {
    const voice = await api(`voices/${$("tone-voice").value}/tone`, {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({tone_preset: $("voice-tone").value})
    });
    state.voices = state.voices.map(current => current.id === voice.id ? voice : current);
    toast("说话语气已保存。");
  } catch (error) {
    $("voice-tone-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
function openChat() {
  if (!state.voices.length) return openVoice();
  $("chat-error").textContent = "";
  if (!$("chat-dialog").open) $("chat-dialog").showModal();
}
function closeDialogs() {
  document.querySelectorAll("dialog[open]").forEach(dialog => dialog.close());
}

let llmSettings = null;
let llmSettingsBusy = false;
function fillLlmSettings(settings) {
  llmSettings = settings;
  const empty = !settings.base_url && !settings.model;
  $("llm-base-url").value = empty ? "https://api.lingsuan.si/v1" : settings.base_url;
  $("llm-model").value = empty ? "deepseek-v4.1-flash-1017" : settings.model;
  $("llm-key-status").textContent = settings.api_key_set ? "已设置" : "未设置";
  $("llm-api-key").value = "";
  $("llm-api-key").disabled = false;
  $("llm-clear-key").checked = false;
}
async function loadLlmSettings() {
  if (llmSettingsBusy) return;
  llmSettingsBusy = true;
  llmSettings = null;
  $("llm-fields").disabled = true;
  $("llm-api-key").value = "";
  $("llm-key-status").textContent = "未读取";
  $("llm-status").textContent = "正在读取配置…";
  $("llm-error").textContent = "";
  $("llm-reload").hidden = true;
  try {
    const settings = await api("settings/llm");
    fillLlmSettings(settings);
    $("llm-status").textContent = !settings.base_url && !settings.model ? "已预填接口地址与模型名；点击保存后才生效。" : "配置已读取。密钥不会回显。";
  } catch (error) {
    $("llm-status").textContent = "";
    $("llm-error").textContent = `${error.message} 请点击“重新读取配置”重试。`;
    $("llm-reload").hidden = false;
  } finally {
    llmSettingsBusy = false;
    $("llm-fields").disabled = !llmSettings;
  }
}
$("llm-reload").addEventListener("click", loadLlmSettings);
$("llm-clear-key").addEventListener("change", event => {
  $("llm-api-key").disabled = event.target.checked;
  if (event.target.checked) $("llm-api-key").value = "";
});
$("settings-dialog").addEventListener("close", () => { $("llm-api-key").value = ""; });
$("llm-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (llmSettingsBusy || !llmSettings) return;
  llmSettingsBusy = true;
  const body = {base_url: $("llm-base-url").value.trim(), model: $("llm-model").value.trim(),
    api_key: $("llm-api-key").value, clear_api_key: $("llm-clear-key").checked};
  $("llm-fields").disabled = true;
  $("llm-save").textContent = "保存中…";
  $("llm-status").textContent = "正在保存配置…";
  $("llm-error").textContent = "";
  try {
    fillLlmSettings(await post("settings/llm", body));
    $("llm-status").textContent = "配置已保存，新请求立即生效。尚未验证接口连通性。";
    await refresh();
  } catch (error) {
    $("llm-status").textContent = "";
    $("llm-error").textContent = error.message;
  } finally {
    llmSettingsBusy = false;
    $("llm-fields").disabled = false;
    $("llm-save").textContent = "保存配置";
  }
});

function setChatTransparency(raw, save = false) {
  const number = Number(raw);
  const value = Number.isFinite(number) ? Math.round(Math.max(0, Math.min(85, number)) / 5) * 5 : 35;
  document.documentElement.style.setProperty("--chat-opacity", String(1 - value / 100));
  $("chat-transparency").value = value;
  $("chat-transparency").setAttribute("aria-valuetext", `${value}% 透明`);
  $("chat-transparency-value").value = `${value}%`;
  if (save) {
    try { localStorage.setItem("voice-chat-transparency", String(value)); } catch (_) { /* Private browsing may block storage. */ }
  }
}
let savedTransparency = 35;
try { savedTransparency = localStorage.getItem("voice-chat-transparency") ?? 35; } catch (_) { /* Use the default if storage is unavailable. */ }
setChatTransparency(savedTransparency);
$("chat-transparency").addEventListener("input", event => setChatTransparency(event.target.value, true));

$("add-character").addEventListener("click", () => openCharacter());
$("new-chat").addEventListener("click", openChat);
$("nav-voices").addEventListener("click", openVoice);
function openLocalStatus() {
  renderHealth();
  if (!$("local-dialog").open) $("local-dialog").showModal();
}
$("nav-local").addEventListener("click", openLocalStatus);
$("top-status").addEventListener("click", openLocalStatus);
$("nav-settings").addEventListener("click", () => {
  if ($("settings-dialog").open) return;
  $("settings-dialog").showModal();
  loadLlmSettings();
});
$("brand-home").addEventListener("click", () => {
  closeDialogs();
  goHome();
});
$("voice-disc").addEventListener("click", openVoice);
$("home-characters").addEventListener("click", () => openLibrary());
$("home-resume").addEventListener("click", () => {
  const latest = state.conversations[0];
  if (latest) selectConversation(latest.id); else beginConversation();
});
$("home-voice-library").addEventListener("click", openVoice);
$("sidebar-voice").addEventListener("click", openVoice);
$("chat-dialog").addEventListener("close", () => { if (!state.current) pendingPrompt = ""; });
const clearAbandonedPrompt = () => setTimeout(() => {
  if (!state.current && !$("voice-dialog").open && !$("character-dialog").open && !$("chat-dialog").open) pendingPrompt = "";
}, 0);
$("voice-dialog").addEventListener("close", clearAbandonedPrompt);
$("character-dialog").addEventListener("close", clearAbandonedPrompt);
document.addEventListener("keydown", event => {
  if ((!event.metaKey && !event.ctrlKey) || event.shiftKey || event.key.toLowerCase() !== "n") return;
  if (document.querySelector("dialog[open]")) return;
  event.preventDefault();
  beginConversation();
});
$("empty-add").addEventListener("click", () => state.voices.length ? openChat() : openVoice());
document.querySelectorAll("dialog .close-dialog").forEach(button => button.addEventListener("click", () => button.closest("dialog").close()));
document.querySelectorAll("dialog").forEach(dialog => dialog.addEventListener("click", event => {
  if (event.target === dialog) dialog.close();
}));
$("voice-select").addEventListener("change", async event => {
  const conversation = currentConversation();
  const previous = conversation ? (conversation.character_id ? `c:${conversation.character_id}` : `v:${conversation.voice_id}`) : "";
  if (state.messages.length && !confirm("切换人物或声音会新建会话，当前会话会保留。继续？")) {
    event.target.value = previous;
    return;
  }
  try { await newConversation(event.target.value); }
  catch (error) { toast(error.message); event.target.value = previous; renderLists(); }
});
$("voice-form").addEventListener("submit", async event => {
  event.preventDefault();
  const file = $("voice-file").files[0];
  if (!file || file.size > 10 * 1024 * 1024) { $("voice-error").textContent = "请选择不超过 10 MB 的 WAV 录音。"; return; }
  $("save-voice").disabled = true;
  $("save-voice").textContent = "正在校验并保存…";
  $("voice-error").textContent = "";
  try {
    await api("voices", {method: "POST", body: new FormData(event.target)});
    await refresh();
    event.target.reset();
    $("voice-reference-settings").open = false;
    toast("参考录音已保存，导入训练 ZIP 时可选用。");
  } catch (error) {
    if ($("voice-dialog").open) $("voice-error").textContent = error.message; else toast(error.message);
  } finally { $("save-voice").disabled = false; $("save-voice").textContent = "保存参考录音"; }
});
function openAdapterImport() {
  const select = $("adapter-reference");
  select.replaceChildren(new Option("无参考录音", ""));
  for (const voice of state.voices) {
    if (voice.duration > 0) select.add(new Option(voice.name, voice.id));
  }
  if (select.options.length > 1) select.selectedIndex = 1;
  $("adapter-error").textContent = "";
  if ($("voice-dialog").open) $("voice-dialog").close();
  if (!$("adapter-dialog").open) $("adapter-dialog").showModal();
}
$("open-adapter-import").addEventListener("click", openAdapterImport);
$("adapter-form").addEventListener("submit", async event => {
  event.preventDefault();
  const file = $("adapter-file").files[0];
  if (!file || file.size > 41 * 1024 * 1024) {
    $("adapter-error").textContent = "请选择不超过 41 MiB 的训练结果 ZIP。";
    return;
  }
  $("import-adapter").disabled = true;
  $("import-adapter").textContent = "正在校验并加载…";
  $("adapter-error").textContent = "";
  try {
    const voice = await api("voice-adapters", {
      method: "POST",
      body: new FormData(event.target),
      timeoutMs: 120000
    });
    await refresh();
    $("adapter-dialog").close();
    event.target.reset();
    if ($("character-dialog").open) fillCharacterVoices(voice.id);
    else openCharacter(null, voice.id);
    toast("训练声音已导入并可立即使用。");
  } catch (error) {
    $("adapter-error").textContent = error.message;
  } finally {
    $("import-adapter").disabled = false;
    $("import-adapter").textContent = "导入并使用";
  }
});
$("chat-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.target.querySelector("button[type=submit]"); button.disabled = true;
  try { await newConversation($("new-voice-select").value); $("chat-dialog").close(); }
  catch (error) { $("chat-error").textContent = error.message; }
  finally { button.disabled = false; }
});
let startingHomeConversation = false;
$("composer").addEventListener("submit", async event => {
  event.preventDefault();
  if (startingHomeConversation) return;
  const content = $("message-input").value.trim();
  if (!state.current) {
    const voice = primaryVoice();
    if (!voice) return beginConversation(content);
    const character = primaryCharacter();
    const selection = character?.voice_id === voice.id ? `c:${character.id}` : `v:${voice.id}`;
    const start = $("home-start");
    startingHomeConversation = true;
    start.disabled = true;
    try {
      const created = await newConversation(selection);
      if (state.current !== created) return;
      $("message-input").value = content;
      autosizeInput();
      renderHealth();
      $("message-input").focus();
    } catch (error) {
      toast(error.message);
      return;
    } finally {
      startingHomeConversation = false;
      start.disabled = false;
    }
  }
  if ($("send").disabled) return;
  const selected = state.current;
  $("send").disabled = true;
  try {
    await post(`conversations/${selected}/messages`, {content});
    if (selected === state.current) { $("message-input").value = ""; autosizeInput(); }
    state.pinned = true;
    await refresh();
  } catch (error) { toast(error.message); renderHealth(); }
});
$("stop").addEventListener("click", async () => {
  const message = activeMessage();
  if (!message) return;
  pauseAudio();
  state.stopping = message.id;
  renderHealth();
  try { await post(`messages/${message.id}/cancel`, {}); await refresh(); }
  catch (error) { state.stopping = null; renderHealth(); toast(error.message); }
});
$("message-input").addEventListener("input", () => { autosizeInput(); scheduleHealth(); });
let recordingPointer = null;
function beginRecording() {
  if ($("record").disabled || recorder.phase !== "idle") return;
  pauseAudio(); recorder.start();
}
$("record").addEventListener("pointerdown", event => {
  if (event.button !== 0 || $("record").disabled) return;
  event.preventDefault(); recordingPointer = event.pointerId;
  $("record").setPointerCapture(event.pointerId); beginRecording();
});
$("record").addEventListener("pointerup", event => {
  if (recordingPointer !== event.pointerId) return;
  recordingPointer = null; recorder.finish();
});
for (const name of ["pointercancel", "lostpointercapture"]) $("record").addEventListener(name, event => {
  if (recordingPointer !== event.pointerId) return;
  recordingPointer = null; recorder.cancel();
});
$("record").addEventListener("keydown", event => {
  if (![" ", "Enter"].includes(event.key)) return;
  event.preventDefault(); if (!event.repeat) beginRecording();
});
$("record").addEventListener("keyup", event => {
  if (![" ", "Enter"].includes(event.key)) return;
  event.preventDefault(); recorder.finish();
});
addEventListener("blur", () => { recordingPointer = null; recorder.cancel(); });
addEventListener("pagehide", () => { clearTimeout(pollTimer); pollTimer = 0; recorder.cancel(); pauseAudio(); });
$("timeline").addEventListener("scroll", () => {
  const timeline = $("timeline");
  state.pinned = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 90;
});
$("message-input").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault(); $("composer").requestSubmit();
  }
});
function pollDelay() {
  if (!state.online) return POLL_OFFLINE;
  const warming = state.health?.model_status === "loading" || state.health?.stt_status === "loading";
  return warming || state.health?.busy || activeMessage() || recorder.phase !== "idle" ? POLL_ACTIVE : POLL_IDLE;
}
function schedulePoll(delay = pollDelay()) {
  clearTimeout(pollTimer);
  pollTimer = document.hidden ? 0 : setTimeout(poll, delay);
}
async function poll() {
  pollTimer = 0;
  if (document.hidden || polling) return;
  polling = true;
  try { await refresh(); } finally { polling = false; }
  if (!document.hidden && !pollTimer) schedulePoll();
}
function startPolling() {
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { clearTimeout(pollTimer); pollTimer = 0; }
    else if (!pollTimer && !polling) void poll();
  });
  addEventListener("pageshow", event => {
    if (event.persisted && !pollTimer && !polling) void poll();
  });
  schedulePoll();
}
let lastIntroStartupId = null;
function showStartupIntro(startupId) {
  const app = $("app");
  if (!startupId || app.dataset.home !== "true" ||
      matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  if (lastIntroStartupId === startupId) return;
  lastIntroStartupId = startupId;
  app.setAttribute("data-intro", "");
  setTimeout(() => {
    if (lastIntroStartupId !== startupId || !app.hasAttribute("data-intro")) return;
    app.removeAttribute("data-intro");
    renderHealth();
  }, 3600);
}

async function start() {
  await refresh();
  renderMessages();
  renderHealth();
  startPolling();
}

let editingCharacter = null, editingCharacterRevision = null, memoryCharacter = null, editingMemory = null, memorySignature = "";
const candidateRows = new Map();
let memoryRequest = 0;
const characterFields = ["name", "background", "personality", "relationship", "speech_style"];
for (let i = 0; i < 3; i++) {
  const label = node("label", "", `示例 ${i + 1}：用户`), input = node("textarea");
  input.id = `example-user-${i}`; input.maxLength = 500; input.rows = 2; label.htmlFor = input.id;
  const answerLabel = node("label", "", "人物回复"), answer = node("textarea");
  answer.id = `example-assistant-${i}`; answer.maxLength = 500; answer.rows = 2; answerLabel.htmlFor = answer.id;
  $("character-examples").append(label, input, answerLabel, answer);
}
function fillCharacterVoices(selected) {
  const select = $("character-voice"); select.replaceChildren();
  for (const voice of state.voices) select.add(new Option(voice.name, voice.id));
  if (selected) select.value = selected;
}
function openCharacter(character = null, voiceId = null) {
  editingCharacter = character?.id || null;
  editingCharacterRevision = character?.profile_revision_id || null;
  $("character-title").textContent = character ? "编辑人物（新会话生效）" : "创建人物";
  $("character-form").reset(); $("character-error").textContent = "";
  for (const field of characterFields) $("character-" + field).value = character?.[field] || "";
  fillCharacterVoices(voiceId || character?.voice_id);
  for (let i = 0; i < 3; i++) for (const role of ["user", "assistant"]) $( `example-${role}-${i}`).value = character?.examples?.[i]?.[role] || "";
  if (!$("character-dialog").open) $("character-dialog").showModal();
}
async function openLibrary() {
  await refresh(); $("character-library").replaceChildren();
  if (!state.characters.length) $("character-library").append(node("p", "dialog-intro", "还没有人物，先创建一个。"));
  for (const character of state.characters) {
    const row = node("section", "character-row");
    const actions = node("div", "character-row-actions");
    row.append(node("h3", "", character.name), node("p", "", character.personality || "尚未填写性格"));
    for (const [label, action] of [["开始对话", async () => { await newConversation(`c:${character.id}`); $("characters-dialog").close(); }], ["编辑", () => openCharacter(character)], ["记忆", () => openMemories(character.id)], ["交流风格与版本", () => openEvolution(character.id)]]) {
      const button = node("button", "", label); button.type = "button";
      button.addEventListener("click", () => Promise.resolve(action()).catch(error => toast(error.message))); actions.append(button);
    }
    row.append(actions);
    $("character-library").append(row);
  }
  if (!$("characters-dialog").open) $("characters-dialog").showModal();
}
$("create-character").addEventListener("click", () => openCharacter());
$("character-add-voice").addEventListener("click", openVoice);
$("character-form").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.target.querySelector("button[type=submit]"); button.disabled = true;
  try {
    const body = Object.fromEntries(characterFields.map(field => [field, $("character-" + field).value]));
    body.voice_id = $("character-voice").value; body.examples = [];
    if (editingCharacter) body.base_revision_id = editingCharacterRevision;
    for (let i = 0; i < 3; i++) {
      const user = $(`example-user-${i}`).value.trim(), assistant = $(`example-assistant-${i}`).value.trim();
      if (user || assistant) { if (!user || !assistant) throw new Error("示例需同时填写用户和人物发言。"); body.examples.push({user, assistant}); }
    }
    const character = await api(editingCharacter ? `characters/${editingCharacter}` : "characters", {method: editingCharacter ? "PUT" : "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const wasEditing = !!editingCharacter;
    $("character-dialog").close(); if ($("characters-dialog").open) $("characters-dialog").close();
    await refresh();
    if ($("character-settings-dialog").open) renderCharacterSnapshot(currentConversation());
    if (!wasEditing) await newConversation(`c:${character.id}`);
    toast(wasEditing ? "人物已保存，新会话生效。" : "人物已创建，可以开始聊天。");
  } catch (error) { $("character-error").textContent = error.message; }
  finally { button.disabled = false; }
});
function snapshotStatus(conversation) {
  const latest = state.characters.find(character => character.id === conversation.character_id);
  if (!conversation.character_revision_id) return {text: "本会话创建于版本记录之前，无法确认是否为最新设定。", updated: false};
  if (!latest) return {text: "暂时无法确认人物是否已有更新。", updated: false};
  if (latest.profile_revision_id === conversation.character_revision_id) return {text: "本会话使用的就是人物最新设定。", updated: false};
  return {text: "人物在本会话创建后已更新设定；下方为创建会话时的快照。", updated: true};
}
function snapshotField(label, value) {
  const field = node("div", "snapshot-field");
  field.append(node("h3", "", label), node("p", value ? "" : "empty", value || "未填写"));
  return field;
}
function renderCharacterSnapshot(conversation) {
  const profile = profileOf(conversation); if (!profile) return false;
  const status = snapshotStatus(conversation), version = $("character-snapshot-version");
  version.textContent = status.text;
  version.classList.toggle("updated", status.updated);
  const card = node("div", "snapshot-card");
  for (const field of characterFields) card.append(snapshotField(characterLabels[field], profile[field]));
  card.append(snapshotField("绑定声音", state.voices.find(voice => voice.id === profile.voice_id)?.name || profile.voice_id));
  if (profile.examples?.length) {
    const field = node("div", "snapshot-field");
    field.append(node("h3", "", "示例对话"));
    for (const example of profile.examples) field.append(node("p", "snapshot-example", `用户：${example.user}\n人物：${example.assistant}`));
    card.append(field);
  }
  $("character-snapshot").replaceChildren(card);
  return true;
}
$("character-settings").addEventListener("click", () => {
  if (!renderCharacterSnapshot(currentConversation())) return;
  $("character-settings-dialog").showModal();
});
$("edit-current-character").addEventListener("click", async event => {
  const button = event.currentTarget; button.disabled = true;
  try { openCharacter(await api(`characters/${currentConversation().character_id}`)); }
  catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});
$("start-latest-character").addEventListener("click", async () => {
  const selected = currentConversation()?.character_id;
  if (!selected) return;
  try { await newConversation(`c:${selected}`); $("character-settings-dialog").close(); }
  catch (error) { toast(error.message); }
});
function resetMemoryForm() { editingMemory = null; $("memory-form").reset(); $("memory-form-title").textContent = "添加记忆"; $("memory-error").textContent = ""; }
function showMemoryPanel(pending) {
  $("confirmed-memory").hidden = pending;
  $("pending-memory").hidden = !pending;
  $("show-confirmed-memory").setAttribute("aria-pressed", String(!pending));
  $("show-pending-memory").setAttribute("aria-pressed", String(pending));
}
async function openMemories(value) {
  memoryCharacter = value; memorySignature = ""; resetMemoryForm();
  candidateRows.clear();
  for (const id of ["memory-list", "candidate-list", "memory-refresh-error", "confirmed-list-error", "candidate-list-error"]) $(id).replaceChildren();
  $("candidate-empty").hidden = true;
  $("show-confirmed-memory").textContent = "已记住";
  $("show-pending-memory").textContent = "待确认";
  $("memory-status").textContent = "正在读取记忆…";
  $("refresh-memory").disabled = true;
  showMemoryPanel(false);
  $("memory-title").textContent = `${state.characters.find(c => c.id === value)?.name || "人物"}的记忆`;
  if (!$("memory-dialog").open) $("memory-dialog").showModal();
  await loadMemories();
}
function renderConfirmedMemories(selected, data) {
  $("show-confirmed-memory").textContent = `已记住（${data.items.length}/30）`;
  $("memory-status").textContent = ({idle: "已就绪", updating: "正在提炼候选…", warning: "需要检查记忆", failed: "提炼失败"}[data.status] || data.status) + (data.error ? `：${data.error}` : "");
  $("refresh-memory").disabled = data.busy;
  const signature = JSON.stringify(data.items);
  if (signature === memorySignature) return;
  memorySignature = signature;
  $("memory-list").replaceChildren();
  if (!data.items.length) $("memory-list").append(node("p", "dialog-intro", "暂无长期记忆。可以手动添加，或在「待确认」中查看提炼建议。"));
  for (const item of data.items) {
    const row = node("section", "memory-row");
    row.append(node("h3", "", item.key), node("p", "profile-text", item.content), node("p", "memory-origin", `${item.origin === "manual" ? "手动" : "来自对话"} · ${new Date(item.updated_at).toLocaleString("zh-CN")}`));
    const sourceMessage = item.source_id && state.messages.find(message => message.id === item.source_id);
    if (sourceMessage) row.append(node("small", "", `来源消息 · ${new Date(sourceMessage.created_at).toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"})}`));
    const edit = node("button", "", "修改"), remove = node("button", "", "删除长期记忆"); edit.type = remove.type = "button";
    edit.addEventListener("click", () => { editingMemory = item.id; $("memory-key").value = item.key; $("memory-content").value = item.content; $("memory-form-title").textContent = "修改记忆"; $("memory-content").focus(); });
    remove.addEventListener("click", async () => {
      if (!confirm("删除这条长期记忆？原始聊天记录仍在会话中。")) return;
      try { await api(`characters/${selected}/memories/${item.id}`, {method: "DELETE"}); if (editingMemory === item.id) resetMemoryForm(); await loadMemories(); } catch (error) { $("memory-error").textContent = error.message; }
    }); row.append(edit, remove); $("memory-list").append(row);
  }
}
function candidateRow(selected, item, draft) {
  const row = node("section", "memory-row");
  const comparison = node("div", "memory-compare");
  const oldValue = node("div");
  oldValue.append(node("span", "memory-value-label", "原有记忆"), node("p", "profile-text", item.old_content ?? "首次记录"));
  const suggestion = node("div");
  const label = node("label", "", "建议记忆（可编辑）"), input = node("textarea");
  input.id = `memory-candidate-${item.id}`;
  input.rows = 3;
  input.maxLength = 200;
  input.value = draft ?? item.content;
  label.htmlFor = input.id;
  suggestion.append(label, input);
  comparison.append(oldValue, suggestion);

  const source = node("div", "memory-source");
  const sourceTime = item.source_created_at || item.created_at;
  const time = node("time", "", new Date(sourceTime).toLocaleString("zh-CN"));
  time.dateTime = sourceTime;
  const sourceLabel = node("small", "", "来源消息 · ");
  sourceLabel.append(time);
  source.append(sourceLabel, node("blockquote", "profile-text", item.source_text));
  const error = node("p", "error");
  error.setAttribute("role", "alert");
  const approve = node("button", "", "确认"), reject = node("button", "", "驳回"), review = node("button", "", "重新审阅");
  approve.type = reject.type = review.type = "button";
  let busy = false, stale = false;
  function update() {
    approve.textContent = input.value === item.content ? "确认" : "按修改确认";
    approve.disabled = busy || stale || !input.value.trim();
    input.disabled = reject.disabled = review.disabled = busy;
    review.hidden = !stale;
  }
  async function submit(action) {
    busy = true;
    error.textContent = "";
    update();
    const body = {action};
    if (action === "approve") {
      body.review_token = item.review_token;
      if (input.value !== item.content) body.content = input.value;
    }
    try {
      const data = await post(`characters/${selected}/memory-candidates/${item.id}`, body);
      if (selected !== memoryCharacter) return;
      ++memoryRequest;
      renderMemoryCandidates(selected, data.items);
      if (action === "review") candidateRows.get(item.id)?.input.focus();
      else $("show-pending-memory").focus();
      await loadMemories();
    } catch (exception) {
      if (action === "approve" && exception.memoryConflict === "stale") stale = true;
      error.textContent = exception.message;
    } finally {
      busy = false;
      update();
    }
  }
  input.addEventListener("input", update);
  approve.addEventListener("click", () => submit("approve"));
  reject.addEventListener("click", () => submit("reject"));
  review.addEventListener("click", () => submit("review"));
  row.append(node("h3", "", item.key), comparison, source, error, approve, reject, review);
  update();
  return {row, input, item};
}
function renderMemoryCandidates(selected, items) {
  const pendingIds = new Set(items.map(item => item.id));
  for (const [id, entry] of candidateRows) {
    if (pendingIds.has(id)) continue;
    entry.row.remove();
    candidateRows.delete(id);
  }
  for (const item of items) {
    const previous = candidateRows.get(item.id);
    if (previous && JSON.stringify(previous.item) === JSON.stringify(item)) continue;
    const draft = previous && previous.input.value !== previous.item.content ? previous.input.value : undefined;
    const entry = candidateRow(selected, item, draft);
    if (previous) previous.row.replaceWith(entry.row);
    else $("candidate-list").append(entry.row);
    candidateRows.set(item.id, entry);
  }
  $("show-pending-memory").textContent = `待确认（${items.length}）`;
  $("candidate-empty").hidden = items.length > 0;
}
async function loadMemories() {
  const selected = memoryCharacter, request = ++memoryRequest;
  const [confirmed, candidates] = await Promise.allSettled([
    api(`characters/${selected}/memories`), api(`characters/${selected}/memory-candidates`)
  ]);
  if (selected !== memoryCharacter || request !== memoryRequest) return;
  $("confirmed-list-error").textContent = confirmed.status === "rejected" ? confirmed.reason.message : "";
  $("candidate-list-error").textContent = candidates.status === "rejected" ? candidates.reason.message : "";
  if (confirmed.status === "fulfilled") renderConfirmedMemories(selected, confirmed.value);
  else $("memory-status").textContent = confirmed.reason.message;
  if (candidates.status === "fulfilled") renderMemoryCandidates(selected, candidates.value.items);
}
$("character-memories").addEventListener("click", () => openMemories(currentConversation().character_id));
$("show-confirmed-memory").addEventListener("click", () => showMemoryPanel(false));
$("show-pending-memory").addEventListener("click", () => showMemoryPanel(true));
$("refresh-memory").addEventListener("click", async () => {
  const selected = memoryCharacter;
  $("memory-refresh-error").textContent = "";
  try { await post(`characters/${selected}/memories/refresh`, {}); await loadMemories(); }
  catch (error) { if (selected === memoryCharacter) $("memory-refresh-error").textContent = error.message; }
});
$("reset-memory-form").addEventListener("click", resetMemoryForm);
$("memory-form").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.target.querySelector("button[type=submit]"); button.disabled = true;
  try {
    await api(`characters/${memoryCharacter}/memories${editingMemory ? "/" + editingMemory : ""}`, {method: editingMemory ? "PUT" : "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({key: $("memory-key").value, content: $("memory-content").value})});
    resetMemoryForm(); await loadMemories();
  } catch (error) { $("memory-error").textContent = error.message; }
  finally { button.disabled = false; }
});
let summaryConversation = null, summaryData = null, summaryBase = null, summarySignature = "";
let summaryDirty = false, summaryConflict = false, summaryBusy = false, summaryRequest = 0;
let summaryRevisionRequest = 0, summaryNextOffset = null;
const summaryEditors = [];
const summaryKinds = {user_statement: "用户陈述", assistant_suggestion: "人物建议", open_thread: "待继续讨论"};
const profileLabels = {speech_style: "说话习惯", personality: "性格"};
const characterLabels = {name: "姓名", background: "背景", personality: "性格", relationship: "与你的关系", speech_style: "说话习惯"};
const revisionOrigins = {auto: "自动整理", manual: "手动修改", rollback: "恢复版本", clear: "清空", initial: "初始档案", migration: "历史档案", growth: "确认建议"};
function backgroundStatus(data) {
  return ({idle: "已就绪", queued: "等待后台整理", updating: "正在后台整理…", failed: "整理失败", warning: "需要检查"}[data.status] || data.status || "已就绪") + (data.error ? `：${data.error}` : "");
}
function sourceDetails(sources) {
  const details = node("details", "memory-source");
  details.append(node("summary", "", `查看来源（${sources.length}）`));
  for (const source of sources) {
    const role = source.role === "assistant" ? "人物" : "用户";
    details.append(node("small", "", `${role} · ${source.created_at ? new Date(source.created_at).toLocaleString("zh-CN") : source.source_id || source.id}`),
      node("blockquote", "profile-text", source.text ?? source.content ?? "来源原文暂不可用"));
  }
  return details;
}
function summarySources(item, data = summaryData) {
  if (Array.isArray(item.sources)) return item.sources;
  const available = Array.isArray(data?.sources) ? data.sources : [];
  return item.source_message_ids.map(id => available.find(source => (source.source_id || source.id) === id)
    || state.messages.find(message => message.id === id) || {id});
}
function updateSummaryControls() {
  $("save-summary").disabled = summaryBusy || !summaryDirty || summaryConflict;
  $("reload-summary").hidden = !summaryDirty && !summaryConflict;
  $("reload-summary").disabled = summaryBusy;
  $("clear-summary").disabled = summaryBusy || !summaryData;
  $("refresh-summary").disabled = summaryBusy || !summaryData || ["queued", "updating"].includes(summaryData.status);
  for (const entry of summaryEditors) {
    entry.input.disabled = summaryBusy;
    entry.row.querySelector("button").disabled = summaryBusy;
  }
}
function renderSummary(data) {
  summaryData = data;
  $("summary-status").textContent = backgroundStatus(data);
  const covered = state.messages.find(message => message.id === data.covered_until_assistant_id);
  $("summary-coverage").textContent = data.covered_until_assistant_id
    ? `已整理至 ${covered?.created_at ? new Date(covered.created_at).toLocaleString("zh-CN") : data.covered_until_assistant_id}${data.pending_pairs ? ` · ${data.pending_pairs} 组问答待整理` : ""}`
    : data.origin === "clear" ? "摘要已清空，可以手动重新整理历史。" : "尚未生成摘要，达到 20 组有效问答后开始整理较早内容。";
  if (summaryDirty) {
    if (summaryBase !== data.current_revision_id) {
      summaryConflict = true;
      $("summary-error").textContent = "摘要已有新版本，草稿已保留。请重新读取后审阅再保存。";
    }
  } else {
    summaryBase = data.current_revision_id;
    const signature = JSON.stringify([summaryBase, data.items]);
    if (signature !== summarySignature) {
      summarySignature = signature;
      summaryEditors.length = 0;
      $("summary-items").replaceChildren();
      if (!data.items.length) $("summary-items").append(node("p", "dialog-intro", "本会话还没有摘要条目。"));
      for (const [index, item] of data.items.entries()) {
        const row = node("section", "memory-row"), input = node("textarea");
        input.id = `summary-item-${index}`; input.rows = 3; input.maxLength = 160; input.value = item.text; input.required = true;
        const label = node("label", "", summaryKinds[item.kind] || item.kind); label.htmlFor = input.id;
        const entry = {item, input, row}; summaryEditors.push(entry);
        const remove = node("button", "", "删除条目"); remove.type = "button";
        input.addEventListener("input", () => { summaryDirty = true; updateSummaryControls(); });
        remove.addEventListener("click", () => { summaryEditors.splice(summaryEditors.indexOf(entry), 1); row.remove(); summaryDirty = true; updateSummaryControls(); });
        row.append(label, input, sourceDetails(summarySources(item, data)), remove); $("summary-items").append(row);
      }
    }
  }
  updateSummaryControls();
}
async function loadSummary() {
  if (!summaryConversation || summaryBusy) return;
  const selected = summaryConversation, request = ++summaryRequest;
  try {
    const data = await api(`conversations/${selected}/summary`);
    if (selected !== summaryConversation || request !== summaryRequest) return;
    const changed = summaryData?.current_revision_id !== data.current_revision_id;
    renderSummary(data);
    if (changed) await loadSummaryRevisions(true);
  } catch (error) { if (selected === summaryConversation && request === summaryRequest) $("summary-error").textContent = error.message; }
}
async function openSummary() {
  const conversation = currentConversation(); if (!conversation) return;
  summaryConversation = conversation.id; summaryData = null; summaryBase = null; summarySignature = "";
  summaryDirty = summaryConflict = summaryBusy = false; summaryEditors.length = 0; ++summaryRevisionRequest;
  $("summary-title").textContent = `${displayName(conversation)} · 会话摘要`;
  for (const id of ["summary-items", "summary-revisions", "summary-error", "summary-revisions-error", "summary-coverage"]) $(id).replaceChildren();
  $("summary-status").textContent = "正在读取摘要…"; $("more-summary-revisions").hidden = true; updateSummaryControls();
  if (!$("summary-dialog").open) $("summary-dialog").showModal();
  await loadSummary();
}
async function summaryMutation(method, suffix, body) {
  const selected = summaryConversation; if (!selected || summaryBusy) return;
  const request = ++summaryRequest;
  summaryBusy = true; $("summary-error").textContent = ""; updateSummaryControls();
  try {
    const data = await api(`conversations/${selected}/summary${suffix}`, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    if (selected !== summaryConversation || request !== summaryRequest) return;
    summaryDirty = summaryConflict = false; summarySignature = ""; renderSummary(data);
    await loadSummaryRevisions(true);
    toast(suffix === "/rollback" ? "已恢复摘要，历史版本仍保留。" : method === "DELETE" ? "摘要已清空，原始聊天记录保留。" : "摘要已保存。");
  } catch (error) {
    if (selected !== summaryConversation || request !== summaryRequest) return;
    if (error.status === 409) summaryConflict = true;
    $("summary-error").textContent = error.message;
  } finally { if (selected === summaryConversation && request === summaryRequest) { summaryBusy = false; updateSummaryControls(); } }
}
async function loadSummaryRevisions(reset = false) {
  const selected = summaryConversation, request = ++summaryRevisionRequest;
  const offset = reset ? 0 : summaryNextOffset; if (!selected || offset === null) return;
  try {
    const data = await api(`conversations/${selected}/summary/revisions?offset=${offset}`);
    if (selected !== summaryConversation || request !== summaryRevisionRequest) return;
    $("summary-revisions-error").textContent = "";
    if (reset) $("summary-revisions").replaceChildren();
    for (const revision of data.items) {
      const row = node("section", "memory-row"), details = node("details");
      details.append(node("summary", "", `${revisionOrigins[revision.origin] || revision.origin} · ${new Date(revision.created_at).toLocaleString("zh-CN")}`), node("small", "", revision.id));
      for (const item of revision.items) details.append(node("p", "profile-text", `${summaryKinds[item.kind] || item.kind}：${item.text}`), sourceDetails(summarySources(item, revision)));
      const restore = node("button", "", "恢复此版本"); restore.type = "button";
      restore.disabled = revision.id === summaryData?.current_revision_id || revision.valid === false;
      if (revision.valid === false) details.append(node("p", "error", "来源已变更，此版本不能恢复。"));
      restore.addEventListener("click", () => {
        if (summaryDirty) { $("summary-error").textContent = "请先保存修改，或重新读取以放弃草稿，再恢复版本。"; return; }
        summaryMutation("POST", "/rollback", {base_revision_id: summaryBase, target_revision_id: revision.id});
      });
      row.append(details, restore); $("summary-revisions").append(row);
    }
    summaryNextOffset = data.next_offset ?? null; $("more-summary-revisions").hidden = summaryNextOffset === null;
  } catch (error) { if (selected === summaryConversation && request === summaryRevisionRequest) $("summary-revisions-error").textContent = error.message; }
}
$("conversation-summary").addEventListener("click", openSummary);
$("summary-dialog").addEventListener("close", () => { summaryConversation = null; ++summaryRequest; ++summaryRevisionRequest; });
$("summary-form").addEventListener("submit", async event => {
  event.preventDefault(); if (!summaryDirty || summaryConflict) return;
  const items = summaryEditors.map(({item, input}) => ({kind: item.kind, text: input.value.trim(), source_message_ids: item.source_message_ids}));
  if (items.some(item => !item.text) || items.reduce((sum, item) => sum + [...item.text].length, 0) > 1200) {
    $("summary-error").textContent = "每条摘要不能为空，全部条目合计最多 1200 字。"; return;
  }
  await summaryMutation("PUT", "", {base_revision_id: summaryBase, items});
});
$("reload-summary").addEventListener("click", () => { summaryDirty = summaryConflict = false; summarySignature = ""; $("summary-error").textContent = ""; loadSummary(); });
$("clear-summary").addEventListener("click", () => summaryMutation("DELETE", "", {base_revision_id: summaryBase}));
$("refresh-summary").addEventListener("click", async () => {
  const selected = summaryConversation; $("summary-error").textContent = "";
  try { await post(`conversations/${selected}/summary/refresh`, {}); if (selected === summaryConversation) await loadSummary(); }
  catch (error) { if (selected === summaryConversation) $("summary-error").textContent = error.message; }
});
$("more-summary-revisions").addEventListener("click", () => loadSummaryRevisions());

let evolutionCharacter = null, evolutionProfile = null, evolutionRequest = 0, evolutionBusy = false;
let profileRevisionRequest = 0, profileNextOffset = null;
const growthRows = new Map();
function growthCandidateRow(selected, item, draft) {
  const row = node("section", "memory-row"), comparison = node("div", "memory-compare"), before = node("div"), after = node("div");
  before.append(node("span", "memory-value-label", "当前设定"), node("p", "profile-text", item.old_value || "未填写"));
  const label = node("label", "", "建议设定（可编辑）"), input = node("textarea");
  input.id = `growth-${item.id}`; input.rows = 4; input.maxLength = 1000; input.value = draft ?? item.new_value; label.htmlFor = input.id;
  after.append(label, input); comparison.append(before, after);
  const error = node("p", "error"); error.setAttribute("role", "alert");
  const approve = node("button", "", "确认"), reject = node("button", "", "驳回"), review = node("button", "", "重新审阅");
  approve.type = reject.type = review.type = "button";
  let busy = false, stale = false;
  function update() {
    approve.textContent = input.value === item.new_value ? "确认" : "按修改确认";
    approve.disabled = busy || stale || !input.value.trim(); input.disabled = reject.disabled = review.disabled = busy; review.hidden = !stale;
  }
  async function submit(action) {
    if (evolutionBusy) return;
    const request = ++evolutionRequest;
    evolutionBusy = busy = true; error.textContent = ""; update();
    const body = {action};
    if (action === "approve") Object.assign(body, {review_token: item.review_token, base_revision_id: item.base_revision_id, new_value: input.value});
    try {
      const data = await post(`characters/${selected}/profile-candidates/${item.id}`, body);
      if (selected !== evolutionCharacter || request !== evolutionRequest) return;
      renderGrowthCandidates(selected, data.items);
      if (action === "review") growthRows.get(item.id)?.input.focus();
      else $("refresh-evolution").focus();
    } catch (exception) { if (action === "approve" && exception.status === 409) stale = true; error.textContent = exception.message; }
    finally {
      busy = false; update();
      if (selected === evolutionCharacter && request === evolutionRequest) { evolutionBusy = false; await loadEvolution(); }
    }
  }
  input.addEventListener("input", update); approve.addEventListener("click", () => submit("approve")); reject.addEventListener("click", () => submit("reject")); review.addEventListener("click", () => submit("review"));
  row.append(node("h3", "", profileLabels[item.field] || item.field), comparison, node("p", "profile-text", item.reason),
    node("small", "", `建议时间：${new Date(item.created_at).toLocaleString("zh-CN")}`), sourceDetails(item.sources || []), error, approve, reject, review);
  update(); return {row, input, item};
}
function renderGrowthCandidates(selected, items) {
  const ids = new Set(items.map(item => item.id));
  for (const [id, entry] of growthRows) if (!ids.has(id)) { entry.row.remove(); growthRows.delete(id); }
  $("evolution-candidates").querySelector(".growth-empty")?.remove();
  for (const item of items) {
    const previous = growthRows.get(item.id);
    if (previous && JSON.stringify(previous.item) === JSON.stringify(item)) continue;
    const draft = previous && previous.input.value !== previous.item.new_value ? previous.input.value : undefined;
    const entry = growthCandidateRow(selected, item, draft);
    if (previous) previous.row.replaceWith(entry.row); else $("evolution-candidates").append(entry.row);
    growthRows.set(item.id, entry);
  }
  if (!items.length) $("evolution-candidates").append(node("p", "dialog-intro growth-empty", "暂无待确认建议。需要跨会话的多次明确反馈作为依据。"));
}
async function loadEvolution() {
  if (!evolutionCharacter || evolutionBusy) return;
  const selected = evolutionCharacter, request = ++evolutionRequest;
  try {
    const [profile, data] = await Promise.all([api(`characters/${selected}`), api(`characters/${selected}/profile-candidates`)]);
    if (selected !== evolutionCharacter || request !== evolutionRequest) return;
    const changed = evolutionProfile?.profile_revision_id !== profile.profile_revision_id; evolutionProfile = profile;
    $("evolution-status").textContent = backgroundStatus(data);
    $("refresh-evolution").disabled = ["queued", "updating"].includes(data.status) || data.items.length > 0;
    renderGrowthCandidates(selected, data.items);
    if (changed) await loadProfileRevisions(true);
  } catch (error) { if (selected === evolutionCharacter && request === evolutionRequest) $("evolution-error").textContent = error.message; }
}
async function openEvolution(value) {
  evolutionCharacter = value; evolutionProfile = null; evolutionBusy = false; growthRows.clear(); ++profileRevisionRequest;
  $("evolution-title").textContent = `${state.characters.find(character => character.id === value)?.name || "人物"} · 交流风格与版本`;
  for (const id of ["evolution-candidates", "profile-revisions", "evolution-error", "profile-revisions-error"]) $(id).replaceChildren();
  $("evolution-status").textContent = "正在读取建议与版本…"; $("refresh-evolution").disabled = true; $("more-profile-revisions").hidden = true;
  if (!$("evolution-dialog").open) $("evolution-dialog").showModal(); await loadEvolution();
}
async function loadProfileRevisions(reset = false) {
  const selected = evolutionCharacter, request = ++profileRevisionRequest, offset = reset ? 0 : profileNextOffset;
  if (!selected || offset === null) return;
  try {
    const data = await api(`characters/${selected}/profile-revisions?offset=${offset}`);
    if (selected !== evolutionCharacter || request !== profileRevisionRequest) return;
    $("profile-revisions-error").textContent = ""; if (reset) $("profile-revisions").replaceChildren();
    for (const revision of data.items) {
      const row = node("section", "memory-row"), details = node("details");
      details.append(node("summary", "", `${revisionOrigins[revision.origin] || revision.origin} · ${new Date(revision.created_at).toLocaleString("zh-CN")}`),
        node("small", "", revision.id), node("p", "profile-text", revision.reason || ""));
      for (const field of characterFields) details.append(node("p", "profile-text", `${characterLabels[field]}：${revision.profile[field] || "未填写"}`));
      details.append(node("p", "profile-text", `声音：${state.voices.find(voice => voice.id === revision.profile.voice_id)?.name || revision.profile.voice_id}`));
      for (const example of revision.profile.examples || []) details.append(node("p", "profile-text", `用户：${example.user}\n人物：${example.assistant}`));
      if (revision.sources?.length) details.append(sourceDetails(revision.sources));
      const restore = node("button", "", "恢复此版本"); restore.type = "button"; restore.disabled = revision.id === evolutionProfile?.profile_revision_id;
      restore.addEventListener("click", async () => {
        if (evolutionBusy) return;
        evolutionBusy = true; ++evolutionRequest; restore.disabled = true; $("evolution-error").textContent = "";
        try {
          await post(`characters/${selected}/profile-rollback`, {base_revision_id: evolutionProfile.profile_revision_id, target_revision_id: revision.id});
          if (selected === evolutionCharacter) { evolutionProfile = null; toast("已恢复人物设定，新会话生效。"); }
        } catch (error) { if (selected === evolutionCharacter) $("evolution-error").textContent = error.message; }
        finally { if (selected === evolutionCharacter) { evolutionBusy = false; restore.disabled = false; await loadEvolution(); } }
      });
      row.append(details, restore); $("profile-revisions").append(row);
    }
    profileNextOffset = data.next_offset ?? null; $("more-profile-revisions").hidden = profileNextOffset === null;
  } catch (error) { if (selected === evolutionCharacter && request === profileRevisionRequest) $("profile-revisions-error").textContent = error.message; }
}
$("character-evolution").addEventListener("click", () => openEvolution(currentConversation().character_id));
$("evolution-dialog").addEventListener("close", () => { evolutionCharacter = null; ++evolutionRequest; ++profileRevisionRequest; });
$("refresh-evolution").addEventListener("click", async () => {
  const selected = evolutionCharacter; $("evolution-error").textContent = "";
  try { await post(`characters/${selected}/profile-candidates/refresh`, {}); if (selected === evolutionCharacter) await loadEvolution(); }
  catch (error) { if (selected === evolutionCharacter) $("evolution-error").textContent = error.message; }
});
$("more-profile-revisions").addEventListener("click", () => loadProfileRevisions());
$("evolution-start-latest").addEventListener("click", async () => {
  const selected = evolutionCharacter; if (!selected) return;
  try { await newConversation(`c:${selected}`); $("evolution-dialog").close(); if ($("characters-dialog").open) $("characters-dialog").close(); }
  catch (error) { if (selected === evolutionCharacter) $("evolution-error").textContent = error.message; }
});
start();
