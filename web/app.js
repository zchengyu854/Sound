"use strict";
setTimeout(() => document.querySelector(".boot")?.remove(), 3100);
const $ = id => document.getElementById(id);
const SVG = "http://www.w3.org/2000/svg";
const ACTIVE = new Set(["queued", "thinking", "replying", "synthesizing"]);
const TERMINAL = new Set(["complete", "text_failed", "audio_failed"]);
const state = {
  characters: [], voices: [], conversations: [], current: null, health: null, messages: [],
  signatures: new Map(), thinking: new Map(), streams: new Map(), noStream: new Set(),
  listSignature: "", voiceSignature: "", online: false, pinned: true, stopping: null
};
const mobileQuery = matchMedia("(max-width: 900px)");
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
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "输入不符合要求，请检查后重试。");
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
function setView(view) {
  $("app").dataset.view = view;
  if (!mobileQuery.matches) return;
  const target = view === "list"
    ? $("conversations").querySelector(".conversation.active") || $("conversations").querySelector(".conversation") || $("new-chat")
    : $("timeline");
  target?.focus();
}
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
  document.querySelectorAll("[data-count]").forEach(element => { element.textContent = state.conversations.length; });
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
  $("voice-select").value = conversation ? (conversation.character_id ? `c:${conversation.character_id}` : `v:${conversation.voice_id}`) : (state.characters[0] ? `c:${state.characters[0].id}` : state.voices[0] ? `v:${state.voices[0].id}` : "");
  $("voice-select").disabled = !state.voices.length;
  $("chat-title").textContent = conversation ? displayName(conversation) : "创建一个有声音的人物";
  $("character-settings").hidden = $("character-memories").hidden = !conversation?.character_id;
  $("chat-subtitle").textContent = conversation ? `声音：${conversation.voice_name}${conversation.character_id ? " · 虚拟人物 · 本会话设定已固定" : ""}` : "添加参考录音，选择声音开始聊天";
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
  $("nav-dot").className = `nav-dot ${state.online ? (warning ? "warning" : "online") : ""}`;
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
  $("message-input").disabled = !state.current || recording;
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
  $("messages").replaceChildren();
  $("message-input").value = "";
  autosizeInput();
  try { localStorage.setItem("voice-current-conversation", id); } catch (_) { /* Storage can be disabled. */ }
  renderLists(); renderMessages(); renderHealth();
  if (mobileQuery.matches) setView("chat");
  await refresh();
}
async function newConversation(selection) {
  const [kind, id] = selection.includes(":") ? selection.split(":") : ["v", selection];
  const conversation = await post("conversations", kind === "c" ? {character_id: id} : {voice_id: id});
  state.conversations = await api("conversations");
  await selectConversation(conversation.id);
}
async function refresh() {
  const selected = state.current;
  try {
    const [health, voices, conversations, messages, characters] = await Promise.all([
      api("health"), api("voices"), api("conversations"), selected ? api(`conversations/${selected}/messages`) : Promise.resolve([]), api("characters")
    ]);
    state.health = health; state.characters = characters; state.voices = voices; state.conversations = conversations; state.online = true;
    if (selected === state.current) {
      state.messages = messages.map(message => state.streams.has(message.id)
        ? state.messages.find(current => current.id === message.id) || message
        : message);
      for (const message of state.messages) {
        if (message.role === "assistant" && ACTIVE.has(message.status)) openStream(message.id);
      }
    }
    renderLists(); renderMessages(); renderHealth();
    if ($("memory-dialog").open) await loadMemories();
  } catch (error) {
    state.online = false;
    renderHealth();
  }
}

function openVoice() {
  $("voice-error").textContent = "";
  if (!$("voice-dialog").open) $("voice-dialog").showModal();
}
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
$("nav-sessions").addEventListener("click", () => {
  if (mobileQuery.matches) setView("list"); else openChat();
});
$("nav-local").addEventListener("click", () => {
  renderHealth();
  if (!$("local-dialog").open) $("local-dialog").showModal();
});
$("nav-settings").addEventListener("click", () => {
  if ($("settings-dialog").open) return;
  $("settings-dialog").showModal();
  loadLlmSettings();
});
$("brand-home").addEventListener("click", () => {
  closeDialogs();
  if (mobileQuery.matches) setView("list");
});
$("mobile-back").addEventListener("click", () => setView("list"));
document.addEventListener("keydown", event => {
  if (event.key !== "Escape" || !mobileQuery.matches) return;
  if ($("app").dataset.view !== "chat" || document.querySelector("dialog[open]")) return;
  setView("list");
});
$("empty-add").addEventListener("click", () => state.voices.length ? openChat() : openVoice());
document.querySelectorAll(".close-dialog").forEach(button => button.addEventListener("click", () => button.closest("dialog").close()));
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
    const voice = await api("voices", {method: "POST", body: new FormData(event.target)});
    await refresh();
    $("voice-dialog").close(); event.target.reset();
    if ($("character-dialog").open) fillCharacterVoices(voice.id);
    else openCharacter(null, voice.id);
    toast("声音已保存，请绑定到人物。");
  } catch (error) {
    if ($("voice-dialog").open) $("voice-error").textContent = error.message; else toast(error.message);
  } finally { $("save-voice").disabled = false; $("save-voice").textContent = "保存声音"; }
});
function openAdapterImport() {
  if (!state.voices.length) {
    $("voice-error").textContent = "请先保存一段参考录音，再导入训练产物。";
    return;
  }
  const select = $("adapter-reference");
  select.replaceChildren();
  for (const voice of state.voices) select.add(new Option(voice.name, voice.id));
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
$("composer").addEventListener("submit", async event => {
  event.preventDefault();
  if ($("send").disabled) return;
  const selected = state.current, content = $("message-input").value.trim();
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
  return state.health?.busy || activeMessage() || recorder.phase !== "idle" ? POLL_ACTIVE : POLL_IDLE;
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
async function start() {
  await refresh();
  let saved;
  try { saved = localStorage.getItem("voice-current-conversation"); } catch (_) { /* Storage can be disabled. */ }
  const conversation = state.conversations.find(c => c.id === saved) || state.conversations[0];
  if (conversation) await selectConversation(conversation.id);
  else setView("list");
  startPolling();
}

let editingCharacter = null, memoryCharacter = null, editingMemory = null, memorySignature = "";
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
    row.append(node("h3", "", character.name), node("p", "", character.personality || "尚未填写性格"));
    for (const [label, action] of [["开始对话", async () => { await newConversation(`c:${character.id}`); $("characters-dialog").close(); }], ["编辑", () => openCharacter(character)], ["记忆", () => openMemories(character.id)]]) {
      const button = node("button", "", label); button.type = "button";
      button.addEventListener("click", () => Promise.resolve(action()).catch(error => toast(error.message))); row.append(button);
    }
    $("character-library").append(row);
  }
  if (!$("characters-dialog").open) $("characters-dialog").showModal();
}
$("nav-characters").addEventListener("click", openLibrary);
$("create-character").addEventListener("click", () => openCharacter());
$("character-add-voice").addEventListener("click", openVoice);
$("character-form").addEventListener("submit", async event => {
  event.preventDefault(); const button = event.target.querySelector("button[type=submit]"); button.disabled = true;
  try {
    const body = Object.fromEntries(characterFields.map(field => [field, $("character-" + field).value]));
    body.voice_id = $("character-voice").value; body.examples = [];
    for (let i = 0; i < 3; i++) {
      const user = $(`example-user-${i}`).value.trim(), assistant = $(`example-assistant-${i}`).value.trim();
      if (user || assistant) { if (!user || !assistant) throw new Error("示例需同时填写用户和人物发言。"); body.examples.push({user, assistant}); }
    }
    const character = await api(editingCharacter ? `characters/${editingCharacter}` : "characters", {method: editingCharacter ? "PUT" : "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    const wasEditing = !!editingCharacter;
    $("character-dialog").close(); if ($("characters-dialog").open) $("characters-dialog").close();
    await refresh();
    if (!wasEditing) await newConversation(`c:${character.id}`);
    toast(wasEditing ? "人物已保存，新会话生效。" : "人物已创建，可以开始聊天。");
  } catch (error) { $("character-error").textContent = error.message; }
  finally { button.disabled = false; }
});
$("character-settings").addEventListener("click", () => {
  const profile = profileOf(currentConversation()); if (!profile) return;
  $("character-snapshot").replaceChildren();
  const labels = {name: "姓名", background: "背景", personality: "性格", relationship: "与你的关系", speech_style: "说话习惯"};
  for (const field of characterFields) $("character-snapshot").append(node("h3", "", labels[field]), node("p", "profile-text", profile[field] || "未填写"));
  for (const example of profile.examples) $("character-snapshot").append(node("p", "profile-text", `用户：${example.user}\n人物：${example.assistant}`));
  $("character-settings-dialog").showModal();
});
$("edit-current-character").addEventListener("click", async () => {
  try { openCharacter(await api(`characters/${currentConversation().character_id}`)); } catch (error) { toast(error.message); }
});
function resetMemoryForm() { editingMemory = null; $("memory-form").reset(); $("memory-form-title").textContent = "添加记忆"; $("memory-error").textContent = ""; }
async function openMemories(value) {
  memoryCharacter = value; memorySignature = ""; resetMemoryForm();
  $("memory-title").textContent = `${state.characters.find(c => c.id === value)?.name || "人物"}的记忆`;
  if (!$("memory-dialog").open) $("memory-dialog").showModal();
  await loadMemories();
}
async function loadMemories() {
  const selected = memoryCharacter;
  try {
    const data = await api(`characters/${selected}/memories`);
    if (selected !== memoryCharacter) return;
    $("memory-status").textContent = ({idle: "记忆已同步", updating: "正在提炼记忆…", warning: "需要检查记忆", failed: "提炼失败"}[data.status] || data.status) + (data.error ? `：${data.error}` : "") + `（${data.items.length}/30 条）`;
    $("refresh-memory").disabled = data.busy;
    const signature = JSON.stringify(data.items); if (signature === memorySignature) return; memorySignature = signature;
    $("memory-list").replaceChildren();
    if (!data.items.length) $("memory-list").append(node("p", "dialog-intro", "暂无长期记忆。可以手动添加，或聊天后点击更新记忆。"));
    for (const item of data.items) {
      const row = node("section", "memory-row");
      row.append(node("h3", "", item.key), node("p", "profile-text", item.content), node("p", "memory-origin", `${item.origin === "manual" ? "手动" : "自动提炼"} · ${new Date(item.updated_at).toLocaleString("zh-CN")}`));
      const sourceMessage = item.source_id && state.messages.find(message => message.id === item.source_id);
      if (sourceMessage) row.append(node("small", "", `来源消息 · ${new Date(sourceMessage.created_at).toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit"})}`));
      const edit = node("button", "", "修改"), remove = node("button", "", "删除"); edit.type = remove.type = "button";
      edit.addEventListener("click", () => { editingMemory = item.id; $("memory-key").value = item.key; $("memory-content").value = item.content; $("memory-form-title").textContent = "修改记忆（保存后优先于自动提炼）"; $("memory-content").focus(); });
      remove.addEventListener("click", async () => {
        if (!confirm("删除这条记忆？已处理的历史不会重新提炼它。")) return;
        try { await api(`characters/${selected}/memories/${item.id}`, {method: "DELETE"}); if (editingMemory === item.id) resetMemoryForm(); await loadMemories(); } catch (error) { $("memory-error").textContent = error.message; }
      }); row.append(edit, remove); $("memory-list").append(row);
    }
  } catch (error) { $("memory-status").textContent = error.message; }
}
$("character-memories").addEventListener("click", () => openMemories(currentConversation().character_id));
$("refresh-memory").addEventListener("click", async () => {
  try { await post(`characters/${memoryCharacter}/memories/refresh`, {}); await loadMemories(); } catch (error) { $("memory-error").textContent = error.message; }
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
start();
