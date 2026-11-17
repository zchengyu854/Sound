"use strict";

// 独立的自动语音对话控制器。所有状态按会话/消息/轮次编号隔离；
// 播放期间暂停自动收音；网络失败先查原任务，不重复提交。
(() => {
  const ACTIVE = new Set(["queued", "thinking", "replying", "synthesizing"]);

  function createRealtime(doc, win, options = {}) {
    const $ = id => doc.getElementById(id);
    const state = {
      voices: [], voiceId: "", conversationId: "", round: 0,
      listening: false, playing: false, busy: false, ended: false,
      status: "未开始", lastText: "", source: options.source || null,
    };
    let capture = null, player = null, source = null, pollTimer = null, disposed = false;
    const apiBase = "/api";

    async function api(path, init = {}) {
      let response;
      try {
        response = await win.fetch(`${apiBase}/${path}`, {credentials: "same-origin", cache: "no-store", ...init});
      } catch {
        throw new Error("无法连接本机服务，请确认应用已启动。");
      }
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error((data && data.detail) || `请求失败（${response.status}）`);
      return data;
    }
    const json = (method, body, headers = {}) => ({method, headers: {"Content-Type": "application/json", ...headers}, body: JSON.stringify(body)});
    const uuid = () => (win.crypto && win.crypto.randomUUID ? win.crypto.randomUUID() : "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, c => {
      const r = Math.random() * 16 | 0; return (c === "x" ? r : (r & 3 | 8)).toString(16);
    }));

    function setStatus(text) { state.status = text; const el = $("status"); if (el) el.textContent = text; }
    function notice(text) { const el = $("setup-hint"); if (el) el.textContent = text; }
    function message(role, text, id) {
      const box = $("conversation"); if (!box) return null;
      const row = doc.createElement("div");
      row.className = `bubble ${role}`;
      if (id) row.dataset.messageId = id;
      row.textContent = text;
      box.append(row); box.scrollTop = box.scrollHeight;
      return row;
    }

    async function loadVoices() {
      state.voices = await api("voices");
      const select = $("voice-select");
      if (!select) return;
      select.replaceChildren();
      if (!state.voices.length) select.append(new win.Option("还没有声音，请先在训练工作台发布", ""));
      for (const voice of state.voices) select.append(new win.Option(voice.name, voice.id));
      const urlVoice = new win.URL(win.location.href).searchParams.get("voice_id");
      let match = null;
      if (urlVoice) {
        // Validate the voice id from the URL on the server: a browser-only lookup
        // would trust whatever id the link happens to carry.
        try { match = await api(`voices/${encodeURIComponent(urlVoice)}`); } catch { match = null; }
        if (!match) notice("链接中的声音不存在或已不可用，已改用可用声音。");
      }
      select.value = match ? match.id : (state.voices[0]?.id || "");
      state.voiceId = select.value;
      const start = $("start"); if (start) start.disabled = !state.voiceId;
      const name = $("voice-name"); if (name) name.textContent = match ? `${match.name} · 实时语音` : "本地语音 · 选择声音";
    }

    async function start() {
      if (!state.voiceId || state.ended) return;
      const conversation = await api("conversations", json("POST", {voice_id: state.voiceId}));
      state.conversationId = conversation.id;
      $("setup").hidden = true; $("stage").hidden = false;
      $("mic-toggle").disabled = false;
      setStatus("已连接 · 点击开始监听后说话");
    }

    async function end() {
      const active = state.activeMessageId;
      state.ended = true;
      stopListening();
      if (player) player.stop();
      clearTimeout(pollTimer);
      if (active) {
        // Release the model instead of leaving a reply running after the page ends.
        try { await api(`messages/${active}/cancel`, json("POST", {})); } catch (_) {}
      }
      $("stage").hidden = true; $("setup").hidden = false;
      $("start").disabled = false;
      setStatus("已结束对话");
    }

    // ---- capture + VAD ----
    async function startListening(options = {}) {
      const barge = Boolean(options.barge);
      if (state.listening || state.busy || state.ended || (state.playing && !barge)) return;
      const vad = new win.VoiceVad.VoiceVad();
      const context = new win.AudioContext();
      await context.resume();
      const stream = await win.navigator.mediaDevices.getUserMedia({
        audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true},
      });
      await context.audioWorklet.addModule("/realtime-worklet.js");
      const node = new win.AudioWorkletNode(context, "realtime-capture");
      const media = context.createMediaStreamSource(stream);
      media.connect(node);
      const sink = context.createGain(); sink.gain.value = 0;
      node.connect(sink).connect(context.destination);
      const current = {context, stream, node, media, vad, aborted: false, barge, interrupted: false};
      capture = current;
      state.listening = true;
      const toggle = $("mic-toggle"); if (toggle) toggle.textContent = "暂停监听";
      if (!barge) setStatus("正在听…");
      node.port.onmessage = event => {
        if (capture !== current || current.aborted || state.busy) return;
        if (state.playing && !current.barge) return;
        const result = current.vad.push(event.data);
        if (result === "start") {
          if (state.playing && current.barge) void bargeIn(current);
          else setStatus("正在听…（检测到说话）");
        }
        if (result === "end" || result === "timeout") {
          const samples = current.vad.samples();
          const rate = current.context.sampleRate;
          const interrupted = current.interrupted;
          stopListening();
          if (!samples || !samples.length) return;
          if (interrupted) void finishBarge(samples, rate);
          else void submitUtterance(samples, rate);
        }
      };
    }

    // Barge-in: stop local playback immediately, cancel the running reply, keep
    // capturing so the user's sentence is not lost.
    async function bargeIn(current) {
      if (current.interrupted) return;
      current.interrupted = true;
      setStatus("检测到开口，正在停止播放…");
      stopPlayback();
      const active = state.activeMessageId;
      if (active) {
        try { await api(`messages/${active}/cancel`, json("POST", {})); } catch (_) {}
      }
      if (!state.ended) setStatus("正在打断…（说完会继续）");
    }

    async function finishBarge(samples, rate) {
      state.busy = true;
      setStatus("等待模型释放…");
      try {
        if (!(await waitIdle())) { setStatus("模型仍被占用，请稍后再试"); return; }
        await submitUtterance(samples, rate);
      } finally {
        state.busy = false;
      }
    }

    // Poll health until the previous task really released the model; transcription
    // shares the same gate, so submitting too early would only fail.
    async function waitIdle(attempts = 40) {
      for (let index = 0; index < attempts; index++) {
        try {
          const health = await api("health");
          if (!health.busy) return true;
        } catch (_) { /* transient read failure: keep waiting */ }
        await new Promise(resolve => win.setTimeout(resolve, 300));
      }
      return false;
    }


    function stopListening() {
      const current = capture; capture = null; state.listening = false;
      const toggle = $("mic-toggle"); if (toggle) toggle.textContent = "开始监听";
      if (!current) return;
      current.aborted = true;
      try { current.node.port.onmessage = null; current.node.disconnect(); current.media.disconnect(); } catch (_) {}
      current.stream.getTracks().forEach(track => track.stop());
      current.context.close().catch(() => {});
    }

    async function to16k(samples, rate) {
      if (rate === 16000) return samples;
      const offline = new win.OfflineAudioContext(1, Math.round(samples.length * 16000 / rate), 16000);
      const buffer = offline.createBuffer(1, samples.length, rate);
      buffer.getChannelData(0).set(Array.from(samples, s => s / 32768));
      const sourceNode = offline.createBufferSource(); sourceNode.buffer = buffer;
      sourceNode.connect(offline.destination); sourceNode.start();
      const rendered = await offline.startRendering();
      const data = rendered.getChannelData(0), out = new Int16Array(data.length);
      for (let i = 0; i < data.length; i++) out[i] = Math.round(Math.max(-1, Math.min(1, data[i])) * (data[i] < 0 ? 32768 : 32767));
      return out;
    }

    async function submitUtterance(samples, rate = 16000) {
      const round = ++state.round;
      state.busy = true; setStatus("正在识别…");
      try {
        const pcm = await to16k(samples, rate);
        const body = new win.FormData();
        body.append("recording", new win.Blob([win.VoiceAudio.encodeWav(pcm)], {type: "audio/wav"}), "utterance.wav");
        const transcript = await api("transcriptions", {method: "POST", body});
        const text = (transcript.text || "").trim();
        state.lastText = text;
        if (!text) { setStatus("未识别到语音，请再说一次"); return; }
        message("user", text);
        await send(text, round);
      } catch (error) {
        setStatus(error.message || "识别失败，请重试或直接输入");
      } finally {
        state.busy = false;
        if (!state.playing && !state.ended) setStatus("正在听…（点击监听继续）");
      }
    }

    // ---- send + SSE + playback ----
    async function send(text, round) {
      state.round = round;
      state.interrupted = false;
      const key = uuid();
      setStatus("正在回复…");
      const created = await api(`conversations/${state.conversationId}/messages`,
        json("POST", {content: text, mode: "realtime"}, {"Idempotency-Key": key}));
      const messageId = created.id;
      state.activeMessageId = messageId;
      const row = message("assistant", "", messageId);
      await follow(messageId, created.attempt || 1, row, round);
    }

    function follow(messageId, attempt, row, round) {
      return new Promise(resolve => {
        if (!("EventSource" in win)) { void poll(messageId, attempt, row, round).then(resolve); return; }
        const source = new win.EventSource(`${apiBase}/messages/${messageId}/events`);
        let done = false, started = false;
        const finish = () => { if (done) return; done = true; source.close(); resolve(); };
        source.onmessage = event => {
          if (round !== state.round || state.ended) { finish(); return; }
          let data; try { data = JSON.parse(event.data); } catch { return; }
          applyMessage(row, data);
          // Realtime mode can start playback while text is still streaming:
          // the live PCM buffer is readable as soon as the first sentence is synthesized.
          if (!started && data.mode === "realtime" && data.status === "synthesizing") {
            started = true;
            void playAudio(messageId, data.attempt || attempt, round);
          }
          if (!ACTIVE.has(data.status)) {
            if (state.activeMessageId === messageId) state.activeMessageId = "";
            if (data.status === "interrupted") { state.interrupted = true; stopPlayback(); }
            finish();
            if (!started) void afterReply(messageId, data.attempt || attempt, data.status, round);
          }
        };
        source.onerror = () => { finish(); void poll(messageId, attempt, row, round).then(resolve); };
      });
    }

    async function poll(messageId, attempt, row, round) {
      let status;
      do {
        status = await api(`messages/${messageId}`);
        if (round !== state.round || state.ended) return;
        applyMessage(row, status);
        if (ACTIVE.has(status.status)) await new Promise(r => win.setTimeout(r, 800));
      } while (ACTIVE.has(status.status) && !state.ended);
      await afterReply(messageId, status.attempt || attempt, status.status, round);
    }

    function applyMessage(row, data) {
      if (!row || typeof data.content !== "string") return;
      row.textContent = data.content;
    }

    async function afterReply(messageId, attempt, status, round) {
      if (round !== state.round || state.ended) return;
      if (status === "interrupted") { setStatus("本轮已打断，可以继续说"); return; }
      if (status !== "complete") { setStatus(status === "text_failed" ? "文字生成失败，请重试" : "回复未完成"); return; }
      await playAudio(messageId, attempt, round);
    }

    function playAudio(messageId, attempt, round) {
      return new Promise(resolve => {
        stopListening();
        state.playing = true;
        setStatus("正在播放…");
        const stop = $("stop-play"); if (stop) stop.hidden = false;
        player = new win.VoiceAudio.Player(() => {}, error => { if (error) setStatus(error); });
        player.play(messageId, attempt).then(() => finish()).catch(() => finish());
        // Keep the microphone on while playing so the user can interrupt mid-reply.
        void startListening({barge: true}).catch(() => {});
        function finish() {
          if (round !== state.round) { resolve(); return; }
          state.playing = false; player = null;
          const stop2 = $("stop-play"); if (stop2) stop2.hidden = true;
          if (state.interrupted) {
            state.interrupted = false;
            setStatus("本轮已打断，可以继续说");
          } else {
            setStatus(state.ended ? "已结束对话" : "播放结束 · 点击监听继续");
          }
          resolve();
        }
      });
    }

    function stopPlayback() { if (player) player.stop(); }

    function bind() {
      const voiceSelect = $("voice-select");
      if (voiceSelect) voiceSelect.addEventListener("change", event => {
        state.voiceId = event.target.value;
        const start = $("start"); if (start) start.disabled = !state.voiceId;
      });
      const startBtn = $("start");
      if (startBtn) startBtn.addEventListener("click", () => start().catch(e => notice(e.message)));
      const mic = $("mic-toggle");
      if (mic) mic.addEventListener("click", () => {
        if (state.listening) { stopListening(); setStatus("已暂停监听"); }
        else startListening().catch(e => setStatus(e.message || "无法使用麦克风"));
      });
      const stop = $("stop-play");
      if (stop) stop.addEventListener("click", stopPlayback);
      const endBtn = $("end");
      if (endBtn) endBtn.addEventListener("click", end);
      const manual = $("manual-send");
      if (manual) manual.addEventListener("click", () => {
        const input = $("manual-input"); const text = (input?.value || "").trim();
        if (!text || state.busy || state.ended) return;
        if (input) input.value = "";
        message("user", text);
        void send(text, ++state.round).catch(e => setStatus(e.message));
      });
    }

    async function init() {
      bind();
      try { await loadVoices(); } catch (error) { notice(error.message); }
    }

    function dispose() { disposed = true; end(); }
    return {state, init, start, end, startListening, stopListening, send, submitUtterance, stopPlayback, dispose};
  }

  globalThis.createRealtime = createRealtime;
  if (typeof module !== "undefined" && module.exports) module.exports = {createRealtime};
  if (typeof document !== "undefined") {
    winRealtime();
  }
  function winRealtime() {
    const boot = () => { const app = createRealtime(document, window); window.__realtime = app; void app.init(); };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
    else boot();
  }
})();
