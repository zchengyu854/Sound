"use strict";
class VoiceRecorder {
  constructor(changed, transcript, failed) {
    this.changed = changed; this.transcript = transcript; this.failed = failed;
    this.current = null; this.phase = "idle";
  }
  get supported() { return !!(navigator.mediaDevices?.getUserMedia && window.AudioWorkletNode && window.AudioContext && window.OfflineAudioContext); }
  state(phase) { this.phase = phase; this.changed(); }
  release(current) {
    clearTimeout(current.timer);
    current.stream?.getTracks().forEach(track => track.stop());
    current.node?.disconnect(); current.source?.disconnect(); current.context?.close().catch(() => {});
  }
  cancel() {
    const current = this.current; this.current = null;
    if (current) { current.abort.abort(); this.release(current); }
    this.state("idle");
  }
  async start() {
    if (this.current) return;
    const current = {abort: new AbortController(), chunks: [], frames: 0};
    this.current = current; this.state("requesting");
    try {
      current.context = new AudioContext(); await current.context.resume();
      current.stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true}});
      if (this.current !== current) { this.release(current); return; }
      await current.context.audioWorklet.addModule("/recorder-worklet.js");
      if (this.current !== current) return;
      current.node = new AudioWorkletNode(current.context, "voice-capture");
      current.source = current.context.createMediaStreamSource(current.stream);
      current.node.port.onmessage = event => {
        if (event.data === "flushed") { current.flushed?.(); return; }
        if (this.current !== current) return;
        const remaining = Math.max(0, Math.floor(current.context.sampleRate * 30) - current.frames);
        const part = event.data.subarray(0, remaining);
        current.chunks.push(part); current.frames += part.length;
      };
      const silence = current.context.createGain(); silence.gain.value = 0;
      current.source.connect(current.node).connect(silence).connect(current.context.destination);
      current.timer = setTimeout(() => this.finish(), 30000); this.state("recording");
    } catch (error) { this.error(current, error); }
  }
  error(current, error) {
    if (this.current !== current) return;
    this.cancel();
    const messages = {NotAllowedError: "麦克风权限被拒绝，请在浏览器设置中允许使用。", NotFoundError: "未找到麦克风，请连接设备后重试。", NotReadableError: "麦克风不可用，可能正被其他应用占用。", AbortError: "识别请求已取消或超时，请稍后重试。"};
    this.failed(messages[error.name] || error.message || "录音或识别失败，请重试。");
  }
  async finish() {
    const current = this.current;
    if (!current || this.phase === "transcribing") return;
    if (this.phase === "requesting") { this.cancel(); return; }
    this.state("transcribing"); clearTimeout(current.timer);
    try {
      await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error("录音采集未响应，请重新录制。")), 1000);
        current.flushed = () => { clearTimeout(timeout); resolve(); };
        current.node.port.postMessage("flush");
      });
      const rate = current.context.sampleRate;
      this.release(current);
      if (this.current !== current) return;
      if (current.frames < rate * 0.5) throw new Error("请至少录制 0.5 秒。");
      const offline = new OfflineAudioContext(1, Math.round(current.frames * 16000 / rate), 16000);
      const buffer = offline.createBuffer(1, current.frames, rate);
      let offset = 0;
      for (const chunk of current.chunks) { buffer.getChannelData(0).set(chunk, offset); offset += chunk.length; }
      current.chunks = [];
      const source = offline.createBufferSource(); source.buffer = buffer;
      source.connect(offline.destination); source.start();
      const audio = await offline.startRendering();
      if (this.current !== current) return;
      const body = new FormData();
      body.append("recording", new Blob([VoiceAudio.encodeWav(audio.getChannelData(0))], {type: "audio/wav"}), "recording.wav");
      current.timer = setTimeout(() => current.abort.abort(), 65000);
      const response = await fetch("/api/transcriptions", {method: "POST", body, signal: current.abort.signal});
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "识别失败，请重试。");
      if (this.current !== current) return;
      this.cancel(); this.transcript(result.text);
    } catch (error) { this.error(current, error); }
  }
}
