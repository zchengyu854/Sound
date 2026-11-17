"use strict";
(() => {
  class PCMDecoder {
    constructor() { this.tail = null; }
    decode(bytes) {
      if (this.tail !== null) {
        const joined = new Uint8Array(bytes.length + 1);
        joined[0] = this.tail; joined.set(bytes, 1); bytes = joined;
      }
      this.tail = bytes.length % 2 ? bytes[bytes.length - 1] : null;
      const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.length);
      const samples = new Float32Array(Math.floor(bytes.length / 2));
      for (let index = 0; index < samples.length; index++) samples[index] = view.getInt16(index * 2, true) / 32768;
      return samples;
    }
  }
  function encodeWav(samples) {
    const buffer = new ArrayBuffer(44 + samples.length * 2), view = new DataView(buffer);
    function word(offset, value) {
      for (let index = 0; index < value.length; index++) view.setUint8(offset + index, value.charCodeAt(index));
    }
    word(0, "RIFF"); view.setUint32(4, buffer.byteLength - 8, true); word(8, "WAVE");
    word(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
    view.setUint16(22, 1, true); view.setUint32(24, 16000, true);
    view.setUint32(28, 32000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    word(36, "data"); view.setUint32(40, samples.length * 2, true);
    for (let index = 0; index < samples.length; index++) {
      const value = Math.max(-1, Math.min(1, samples[index]));
      view.setInt16(44 + index * 2, Math.round(value * (value < 0 ? 32768 : 32767)), true);
    }
    return buffer;
  }
  class Player {
    constructor(changed, failed) { this.changed = changed; this.failed = failed; this.current = null; }
    playing(id) { return this.current?.id === id; }
    stop() {
      const current = this.current;
      if (!current) return;
      this.current = null; current.abort.abort(); clearTimeout(current.timer);
      current.context.close().catch(() => {}); this.changed();
    }
    async play(id, attempt) {
      this.stop();
      let context;
      try { context = new AudioContext(); }
      catch (_) { this.failed("无法启动音频播放，请等待完整 WAV。"); return; }
      const current = {id, attempt, context, abort: new AbortController(), timer: null};
      this.current = current; this.changed();
      try {
        await context.resume();
        const response = await fetch(`/api/audio/${id}/stream?attempt=${attempt}`, {signal: current.abort.signal});
        if (!response.ok) throw new Error((await response.json()).detail || "试听连接失败，请等待完整 WAV。");
        const rate = Number(response.headers.get("X-Audio-Sample-Rate"));
        if (response.headers.get("X-Audio-Format") !== "pcm_s16le" || response.headers.get("X-Audio-Channels") !== "1" || !Number.isFinite(rate) || rate < 8000 || rate > 192000) throw new Error("音频格式不受支持，请等待完整 WAV。");
        const reader = response.body.getReader(), decoder = new PCMDecoder();
        let nextTime = 0, parts = [], length = 0;
        const schedule = () => {
          if (!length) return;
          const buffer = context.createBuffer(1, length, rate), samples = buffer.getChannelData(0);
          let offset = 0;
          for (const part of parts) { samples.set(part, offset); offset += part.length; }
          const source = context.createBufferSource(); source.buffer = buffer; source.connect(context.destination);
          nextTime = Math.max(nextTime, context.currentTime + 0.02);
          source.start(nextTime); nextTime += length / rate; parts = []; length = 0;
        };
        while (this.current === current) {
          while (nextTime - context.currentTime > 2 && this.current === current) await new Promise(resolve => setTimeout(resolve, 20));
          if (this.current !== current) return;
          const {value, done} = await reader.read();
          if (done) break;
          const samples = decoder.decode(value); parts.push(samples); length += samples.length;
          if (length >= rate * 0.2) schedule();
        }
        if (this.current !== current) return;
        if (decoder.tail !== null) throw new Error("音频流不完整，请等待完整 WAV。");
        const status = await fetch(`/api/messages/${id}`, {signal: current.abort.signal}).then(result => result.json());
        if (status.status !== "complete" || status.attempt !== attempt) throw new Error("语音生成未完成，试听已停止；可重试合成。");
        schedule();
        current.timer = setTimeout(() => { if (this.current === current) this.stop(); }, Math.max(0, nextTime - context.currentTime) * 1000 + 50);
      } catch (error) {
        if (this.current !== current) return;
        this.stop();
        if (error.name !== "AbortError") this.failed(error.message || "试听中断，请等待完整 WAV。");
      }
    }
  }
  globalThis.VoiceAudio = {PCMDecoder, encodeWav, Player};
})();
