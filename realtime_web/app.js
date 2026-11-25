"use strict";

(() => {
  const ACTIVE = new Set(["queued", "thinking", "replying", "synthesizing"]);

  function createRealtime(doc, win) {
    const $ = id => doc.getElementById(id);
    const state = {
      voices: [], characters: [], voiceId: "", characterId: "", conversationId: "",
      round: 0, listening: false, playing: false, busy: false, ended: false,
      activeMessageId: "", activeAttempt: 0, status: "未开始", lastText: "",
      interruption: "none", metrics: [],
    };
    let capture = null, captureVersion = 0, capturePending = false;
    let player = null, source = null, session = 0, starting = false;
    let cancelPending = null;
    let suspicion = null, playback = null;
    const now = () => (win.performance || globalThis.performance).now();
    const pauseWindow = () => $("pause-preset")?.value === "slow" ? 1100 : 700;

    function metric(kind, data = {}) {
      state.metrics.push({kind, at_ms: now(), ...data});
      if (state.metrics.length > 100) state.metrics.shift();
    }

    async function api(path, init = {}) {
      let response;
      try {
        response = await win.fetch(`/api/${path}`, {credentials: "same-origin", cache: "no-store", ...init});
      } catch (_) {
        const error = new Error("无法连接本机服务，请确认应用已启动。");
        error.network = true;
        throw error;
      }
      const data = await response.json().catch(() => null);
      if (!response.ok) throw new Error(typeof data?.detail === "string" ? data.detail : `请求失败（${response.status}）`);
      return data;
    }

    const json = (method, body, headers = {}) => ({
      method, headers: {"Content-Type": "application/json", ...headers}, body: JSON.stringify(body),
    });
    const uuid = () => win.crypto.randomUUID();

    function setStatus(text) {
      state.status = text;
      if ($("status")) $("status").textContent = text;
    }

    function notice(text) {
      if ($("setup-hint")) $("setup-hint").textContent = text;
    }

    function updateControls() {
      if ($("mic-toggle")) $("mic-toggle").textContent = state.listening ? "暂停监听" : "继续监听";
      if ($("stop-play")) $("stop-play").hidden = !state.playing;
      if ($("manual-send")) $("manual-send").disabled = state.ended || state.busy || !!state.activeMessageId || state.playing;
    }

    function message(role, text, id) {
      const box = $("conversation");
      const row = doc.createElement("div");
      row.className = `bubble ${role}`;
      if (id) row.dataset.messageId = id;
      row.textContent = text;
      box.append(row);
      box.scrollTop = box.scrollHeight;
      return row;
    }

    function selected() {
      const value = $("voice-select").value;
      state.characterId = value.startsWith("c:") ? value.slice(2) : "";
      state.voiceId = state.characterId ? "" : value;
      $("start").disabled = !value || starting;
      const character = state.characters.find(item => item.id === state.characterId);
      const voice = state.voices.find(item => item.id === state.voiceId);
      $("voice-name").textContent = character?.name || voice?.name || "选择人物或声音";
    }

    async function loadVoices() {
      [state.voices, state.characters] = await Promise.all([api("voices"), api("characters")]);
      const select = $("voice-select");
      select.replaceChildren();
      for (const character of state.characters) select.append(new win.Option(character.name, `c:${character.id}`));
      for (const voice of state.voices) select.append(new win.Option(`仅声音：${voice.name}`, voice.id));
      if (!state.voices.length && !state.characters.length) {
        select.append(new win.Option("请先添加声音或人物", ""));
      }
      const params = new win.URL(win.location.href).searchParams;
      const characterId = params.get("character_id");
      const voiceId = params.get("voice_id");
      let selection = "";
      if (characterId || voiceId) {
        const path = characterId ? `characters/${encodeURIComponent(characterId)}` : `voices/${encodeURIComponent(voiceId)}`;
        try {
          const match = await api(path);
          const exists = characterId
            ? state.characters.some(item => item.id === match.id)
            : state.voices.some(item => item.id === match.id);
          if (exists) selection = characterId ? `c:${match.id}` : match.id;
        } catch (_) {}
        if (!selection) notice("原来选择的人物或声音不可用，请重新选择。");
      }
      select.value = selection || (state.characters[0] ? `c:${state.characters[0].id}` : state.voices[0]?.id || "");
      selected();
    }

    function stopListening() {
      dismissInterruption();
      captureVersion++;
      const current = capture;
      capture = null;
      state.listening = false;
      updateControls();
      if (!current) return;
      current.node.port.onmessage = null;
      try { current.node.disconnect(); current.media.disconnect(); current.sink.disconnect(); } catch (_) {}
      current.stream.getTracks().forEach(track => track.stop());
      void current.context.close().catch(() => {});
    }

    async function startListening() {
      if (state.listening || capturePending || state.ended) return;
      if (!win.navigator.mediaDevices?.getUserMedia || !win.AudioContext || !win.AudioWorkletNode) {
        throw new Error("当前浏览器不支持实时录音，请使用支持麦克风的浏览器。");
      }
      capturePending = true;
      const version = ++captureVersion;
      const currentSession = session;
      let context, stream;
      try {
        context = new win.AudioContext();
        const access = win.navigator.mediaDevices.getUserMedia({
          audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true},
        });
        const [permission, resumed] = await Promise.allSettled([access, context.resume()]);
        if (permission.status === "fulfilled") stream = permission.value;
        if (permission.status === "rejected") throw new Error("无法使用麦克风，请检查浏览器权限。");
        if (resumed.status === "rejected") throw new Error("无法启动音频设备，请重试。");
        if (version !== captureVersion || currentSession !== session || state.ended) return;
        await context.audioWorklet.addModule("/realtime-worklet.js");
        if (version !== captureVersion || currentSession !== session || state.ended) return;
        const node = new win.AudioWorkletNode(context, "realtime-capture");
        const media = context.createMediaStreamSource(stream);
        const sink = context.createGain();
        sink.gain.value = 0;
        media.connect(node);
        node.connect(sink).connect(context.destination);
        const current = {context, stream, node, media, sink,
          vad: new win.VoiceVad.VoiceVad({calibrationMs: 1000, endMs: pauseWindow()})};
        capture = current;
        context = null;
        stream = null;
        state.listening = true;
        updateControls();
        node.port.onmessage = event => {
          if (capture !== current || state.busy || state.ended) return;
          current.vad.configure({endMs: pauseWindow(), minMs: state.playing || state.activeMessageId ? 150 : 500});
          const wasCalibrating = current.vad.calibrating;
          const result = current.vad.push(event.data);
          if (wasCalibrating && !current.vad.calibrating && result !== "start" && !state.playing) setStatus("正在听…说完后会自动回复");
          if (result === "start") {
            if (state.activeMessageId || state.playing) suspectInterruption(current.vad);
            else setStatus("正在听你说话…");
          }
          if (suspicion) {
            // shortcut: 先按持续人声确认插话，中文语义检测通过录音集和真机验收后替换。
            if (current.vad.continuousSpeechMs >= 350) confirmInterruption();
            else if (current.vad.quietCount * (current.vad.frameMs || 20) >= 180 || result === "end" || result === "timeout") {
              dismissInterruption();
              current.vad.reset();
              return;
            }
          }
          if (result === "end" || result === "timeout") {
            const samples = current.vad.samples();
            metric("endpoint", {preset: current.vad.resultEndMs === 1100 ? "slow" : "normal",
              voiced_ms: current.vad.resultVoicedMs || 0, last_voice_at_ms: now() - (current.vad.resultSilenceMs || 0)});
            state.interruption = "none";
            if (samples?.length) void submitUtterance(samples, current.context.sampleRate);
          }
        };
      } finally {
        capturePending = false;
        if (stream) stream.getTracks().forEach(track => track.stop());
        if (context) void context.close().catch(() => {});
      }
    }

    function stopPlayback() {
      dismissInterruption(false);
      if (player) player.stop();
      playback = null;
    }

    function closeSource() {
      if (source) source.close();
      source = null;
    }

    async function waitIdle(currentSession) {
      for (let index = 0; index < 40 && currentSession === session && !state.ended; index++) {
        try {
          const health = await api("health");
          if (!health.busy) return true;
        } catch (_) {}
        await new Promise(resolve => win.setTimeout(resolve, 300));
      }
      return false;
    }

    function suspectInterruption(vad) {
      if (suspicion || (!state.activeMessageId && !state.playing)) return;
      const current = {session, round: state.round, player,
        id: playback?.id || state.activeMessageId, attempt: playback?.attempt || state.activeAttempt,
        started: now(), onset: now() - (vad.continuousSpeechMs || 160), timer: null};
      suspicion = current;
      state.interruption = "suspected";
      setStatus("听到你的声音，正在确认…");
      metric("suspected", {message_id: current.id, attempt: current.attempt});
      if (current.player) void current.player.pause(current.id, current.attempt).then(paused => {
        if (paused && suspicion === current) metric("pause", {response_ms: now() - current.onset});
      });
      current.timer = win.setTimeout(() => { if (suspicion === current) confirmInterruption(); }, 2000);
    }

    function dismissInterruption(resume = true) {
      const current = suspicion;
      if (!current) return;
      suspicion = null;
      win.clearTimeout(current.timer);
      state.interruption = "dismissed";
      metric("dismissed", {elapsed_ms: now() - current.started});
      const dismissedAt = now();
      if (resume && current.session === session && current.round === state.round && player === current.player && player) {
        void player.resume(current.id, current.attempt).then(resumed => {
          if (resumed && current.session === session && current.round === state.round) metric("resume", {response_ms: now() - dismissedAt});
        });
      }
      if (resume && !state.ended) setStatus(state.playing ? "继续说话…可以开口打断" : state.listening ? "正在听…" : "已暂停监听");
    }

    function confirmInterruption() {
      const current = suspicion;
      if (!current || current.session !== session || current.round !== state.round) return;
      suspicion = null;
      win.clearTimeout(current.timer);
      state.interruption = "confirmed";
      metric("confirmed", {elapsed_ms: now() - current.started});
      const active = current.id;
      state.round++;
      state.activeMessageId = "";
      closeSource();
      stopPlayback();
      updateControls();
      setStatus("正在打断，请继续说…");
      const currentSession = session;
      cancelPending = (async () => {
        if (active) {
          try { await api(`messages/${active}/cancel?attempt=${current.attempt}`, json("POST", {})); } catch (_) {}
        }
        return waitIdle(currentSession);
      })();
    }

    async function start() {
      if (starting || !$("voice-select").value) return;
      starting = true;
      $("start").disabled = true;
      state.ended = false;
      state.busy = true;
      state.activeMessageId = "";
      state.activeAttempt = 0;
      state.interruption = "none";
      state.round++;
      cancelPending = null;
      const currentSession = ++session;
      try {
        await startListening();
        if (currentSession !== session || state.ended || !state.listening) return;
        const health = await api("health");
        if (currentSession !== session || state.ended) return;
        if (health.model_status === "loading" || health.stt_status === "loading") {
          throw new Error("语音模型正在加载，请稍后再试。");
        }
        if (health.model_status === "failed") throw new Error(health.model_error || "语音模型加载失败。");
        if (health.stt_status === "failed") throw new Error(health.stt_error || "语音识别模型加载失败。");
        if (health.llm_configured === false) throw new Error("请先在首页设置聊天模型。");
        if (health.busy) throw new Error("本地模型正在处理其他任务，请稍后再试。");
        const selection = state.characterId ? {character_id: state.characterId} : {voice_id: state.voiceId};
        const conversation = await api("conversations", json("POST", selection));
        if (currentSession !== session || state.ended) return;
        state.conversationId = conversation.id;
        $("conversation").replaceChildren();
        $("setup").hidden = true;
        $("stage").hidden = false;
        $("mic-toggle").disabled = false;
        state.busy = false;
        updateControls();
        setStatus(capture?.vad.calibrating ? "正在校准环境…可以随时开始说话" : "正在听…说完后会自动回复");
      } catch (error) {
        stopListening();
        state.busy = false;
        notice(error.message || "无法开始对话，请重试。");
      } finally {
        starting = false;
        $("start").disabled = !$("voice-select").value;
      }
    }

    async function end(keepalive = false) {
      const active = state.activeMessageId;
      const attempt = state.activeAttempt;
      state.ended = true;
      state.round++;
      session++;
      state.activeMessageId = "";
      state.busy = false;
      closeSource();
      stopListening();
      stopPlayback();
      $("stage").hidden = true;
      $("setup").hidden = false;
      $("start").disabled = !$("voice-select").value;
      notice("对话已结束。可以选择人物或声音，再开始一段对话。");
      updateControls();
      if (active) {
        try { await api(`messages/${active}/cancel?attempt=${attempt}`, {...json("POST", {}), keepalive}); } catch (_) {}
      }
    }

    async function to16k(samples, rate) {
      if (rate === 16000) return samples;
      const offline = new win.OfflineAudioContext(1, Math.round(samples.length * 16000 / rate), 16000);
      const buffer = offline.createBuffer(1, samples.length, rate);
      buffer.getChannelData(0).set(Array.from(samples, sample => sample / 32768));
      const sourceNode = offline.createBufferSource();
      sourceNode.buffer = buffer;
      sourceNode.connect(offline.destination);
      sourceNode.start();
      const rendered = await offline.startRendering();
      const data = rendered.getChannelData(0);
      const result = new Int16Array(data.length);
      for (let index = 0; index < data.length; index++) {
        const sample = Math.max(-1, Math.min(1, data[index]));
        result[index] = Math.round(sample * (sample < 0 ? 32768 : 32767));
      }
      return result;
    }

    async function submitUtterance(samples, rate = 16000) {
      if (state.busy || state.ended) return;
      const currentSession = session;
      const round = ++state.round;
      state.busy = true;
      updateControls();
      try {
        if (cancelPending) {
          const pending = cancelPending;
          cancelPending = null;
          setStatus("等待上一句停止…");
          if (!(await pending)) throw new Error("模型仍在处理上一句，请稍后再试。");
        }
        if (currentSession !== session || state.ended || round !== state.round) return;
        setStatus("正在识别…");
        const pcm = await to16k(samples, rate);
        if (currentSession !== session || state.ended || round !== state.round) return;
        const floats = Float32Array.from(pcm, sample => sample / 32768);
        const body = new win.FormData();
        body.append("recording", new win.Blob([win.VoiceAudio.encodeWav(floats)], {type: "audio/wav"}), "utterance.wav");
        const transcript = await api("transcriptions", {method: "POST", body});
        if (currentSession !== session || state.ended || round !== state.round) return;
        const text = (transcript.text || "").trim();
        state.lastText = text;
        if (!text) {
          setStatus("没有听清，请再说一次。");
          return;
        }
        message("user", text);
        await send(text, round);
      } catch (error) {
        if (currentSession === session && !state.ended && round === state.round) {
          setStatus(error.message || "识别失败，请重试或输入文字。");
        }
      } finally {
        if (currentSession === session && !state.ended && round === state.round) {
          state.busy = false;
          updateControls();
        }
      }
    }

    async function send(text, round) {
      const currentSession = session;
      const conversationId = state.conversationId;
      const key = uuid();
      const request = json("POST", {content: text, mode: "realtime"}, {"Idempotency-Key": key});
      setStatus("正在回复…");
      let created;
      try {
        created = await api(`conversations/${conversationId}/messages`, request);
      } catch (error) {
        if (!error.network || currentSession !== session || state.ended) throw error;
        try {
          created = await api(`conversations/${conversationId}/messages`, request);
        } catch (retryError) {
          try {
            const messages = await api(`conversations/${conversationId}/messages`);
            created = messages.find(item => item.request_key === key);
          } catch (_) {}
          if (!created) {
            if (currentSession !== session || state.ended || round !== state.round) return;
            stopListening();
            throw new Error("无法确认消息是否送达，请返回回忆查看后再试。");
          }
        }
      }
      if (currentSession !== session || state.ended || round !== state.round) {
        try { await api(`messages/${created.id}/cancel?attempt=${created.attempt || 1}`, {...json("POST", {}), keepalive: true}); } catch (_) {}
        return;
      }
      state.activeMessageId = created.id;
      state.activeAttempt = created.attempt || 1;
      updateControls();
      const row = message("assistant", "", created.id);
      follow(created.id, created.attempt || 1, row, round);
    }

    function follow(messageId, attempt, row, round) {
      let started = false;
      const currentSession = session;
      const valid = () => currentSession === session && !state.ended && round === state.round;
      function apply(data) {
        if (!valid()) return true;
        if (data.attempt != null && data.attempt !== attempt) {
          stopPlayback();
          if (state.activeMessageId === messageId && state.activeAttempt === attempt) state.activeMessageId = "";
          setStatus("回复已在其他页面重试，请等待模型空闲后继续。");
          updateControls();
          return true;
        }
        if (typeof data.content === "string") row.textContent = data.content;
        if (!started && data.mode === "realtime" && data.status === "synthesizing") {
          started = true;
          playAudio(messageId, data.attempt || attempt, round);
        }
        if (ACTIVE.has(data.status)) return false;
        if (state.activeMessageId === messageId) state.activeMessageId = "";
        updateControls();
        if (data.status === "complete" && !started) playAudio(messageId, data.attempt || attempt, round);
        else if (data.status === "interrupted") {
          stopPlayback();
          setStatus("本轮已打断，可以继续说。");
        } else if (data.status !== "complete") {
          stopPlayback();
          setStatus(data.error || "回复未完成，请重试或开始新对话。");
        } else if (!state.playing) setStatus(state.listening ? "正在听…" : "已暂停监听");
        return true;
      }

      async function poll() {
        while (valid()) {
          try {
            if (apply(await api(`messages/${messageId}`))) return;
          } catch (_) {
            if (valid()) setStatus("连接中断，正在重新获取回复…");
          }
          await new Promise(resolve => win.setTimeout(resolve, 800));
        }
      }

      if (!("EventSource" in win)) { void poll(); return; }
      const current = new win.EventSource(`/api/messages/${messageId}/events`);
      source = current;
      current.onmessage = event => {
        if (source !== current || !valid()) { current.close(); return; }
        let data;
        try { data = JSON.parse(event.data); } catch (_) { return; }
        if (apply(data)) {
          current.close();
          if (source === current) source = null;
        }
      };
      current.onerror = () => {
        current.close();
        if (source !== current) return;
        source = null;
        void poll();
      };
    }

    function playAudio(messageId, attempt, round) {
      if (round !== state.round || state.ended) return;
      state.playing = true;
      setStatus(state.listening ? "正在说话…可以开口打断" : "正在说话 · 监听已暂停");
      updateControls();
      const current = new win.VoiceAudio.Player(() => {
        if (player !== current || current.playing(messageId)) return;
        player = null;
        state.playing = false;
        updateControls();
        if (!state.ended && round === state.round && !state.activeMessageId) {
          setStatus(state.listening ? "正在听…" : "已暂停监听");
        }
      }, error => {
        if (error && round === state.round && !state.ended) setStatus(error);
      });
      player = current;
      playback = {id: messageId, attempt, round, session};
      const playing = current.play(messageId, attempt);
      if (suspicion && suspicion.id === messageId && suspicion.round === round) {
        suspicion.player = current;
        void current.pause(messageId, attempt);
      }
      void playing.then(() => {
        if (player === current && !current.playing(messageId)) {
          player = null;
          state.playing = false;
          updateControls();
        }
      });
    }

    function bind() {
      $("voice-select").addEventListener("change", selected);
      $("start").addEventListener("click", () => { void start(); });
      $("mic-toggle").addEventListener("click", () => {
        if (state.listening) {
          stopListening();
          setStatus("已暂停监听");
        } else {
          void startListening().then(() => {
            if (state.listening) setStatus(state.playing ? "正在说话…可以开口打断" : "正在听…");
          }).catch(error => setStatus(error.message));
        }
      });
      $("stop-play").addEventListener("click", () => {
        stopPlayback();
        capture?.vad.reset();
        setStatus(state.activeMessageId ? "已停止播放，回复仍在生成…" : state.listening ? "正在听…" : "已暂停监听");
      });
      $("end").addEventListener("click", () => { void end(); });
      $("manual-send").addEventListener("click", () => {
        const input = $("manual-input");
        const text = input.value.trim();
        if (!text || state.busy || state.ended || state.activeMessageId || state.playing) return;
        input.value = "";
        state.busy = true;
        updateControls();
        const round = ++state.round;
        const currentSession = session;
        message("user", text);
        void send(text, round).catch(error => {
          if (currentSession === session && !state.ended && round === state.round) setStatus(error.message);
        }).finally(() => {
          if (currentSession === session && !state.ended && round === state.round) {
            state.busy = false;
            updateControls();
          }
        });
      });
      win.addEventListener?.("pagehide", () => { if (!state.ended) void end(true); });
    }

    async function init() {
      bind();
      try { await loadVoices(); } catch (error) { notice(error.message); }
    }

    return {state, init, start, end, startListening, stopListening, send, submitUtterance, stopPlayback};
  }

  globalThis.createRealtime = createRealtime;
  if (typeof module !== "undefined" && module.exports) module.exports = {createRealtime};
  if (typeof document !== "undefined") {
    const boot = () => { const app = createRealtime(document, window); window.__realtime = app; void app.init(); };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
    else boot();
  }
})();
