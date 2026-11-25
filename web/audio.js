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
    async pause(id, attempt) {
      const current = this.current;
      if (!current || current.id !== id || current.attempt !== attempt) return false;
      current.paused = true;
      clearTimeout(current.timer);
      current.transition = current.transition.then(async () => {
        if (this.current === current && current.paused) await current.context.suspend();
      });
      try { await current.transition; }
      catch (_) { if (this.current === current) { this.stop(); this.failed("无法暂停语音，请重新开始对话。"); } return false; }
      return this.current === current && current.paused;
    }
    async resume(id, attempt) {
      const current = this.current;
      if (!current || current.id !== id || current.attempt !== attempt) return false;
      current.paused = false;
      current.transition = current.transition.then(async () => {
        if (this.current === current && !current.paused) await current.context.resume();
      });
      try { await current.transition; }
      catch (_) { if (this.current === current) { this.stop(); this.failed("无法恢复语音，请重新开始对话。"); } return false; }
      if (this.current !== current || current.paused) return false;
      this.finishWhenPlayed(current);
      return true;
    }
    finishWhenPlayed(current) {
      clearTimeout(current.timer);
      if (this.current !== current || current.paused || !current.complete) return;
      current.timer = setTimeout(() => {
        if (this.current !== current || current.paused) return;
        if (current.nextTime > current.context.currentTime + 0.01) this.finishWhenPlayed(current);
        else this.stop();
      }, Math.max(0, current.nextTime - current.context.currentTime) * 1000 + 50);
    }
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
      const current = {id, attempt, context, abort: new AbortController(), timer: null,
        paused: false, complete: false, nextTime: 0, transition: context.resume()};
      this.current = current; this.changed();
      try {
        await current.transition;
        if (this.current !== current) return;
        const response = await fetch(`/api/audio/${id}/stream?attempt=${attempt}`, {signal: current.abort.signal});
        if (!response.ok) throw new Error((await response.json()).detail || "试听连接失败，请等待完整 WAV。");
        const rate = Number(response.headers.get("X-Audio-Sample-Rate"));
        if (response.headers.get("X-Audio-Format") !== "pcm_s16le" || response.headers.get("X-Audio-Channels") !== "1" || !Number.isFinite(rate) || rate < 8000 || rate > 192000) throw new Error("音频格式不受支持，请等待完整 WAV。");
        const reader = response.body.getReader(), decoder = new PCMDecoder();
        let parts = [], length = 0;
        const schedule = () => {
          if (!length) return;
          const buffer = context.createBuffer(1, length, rate), samples = buffer.getChannelData(0);
          let offset = 0;
          for (const part of parts) { samples.set(part, offset); offset += part.length; }
          const source = context.createBufferSource(); source.buffer = buffer; source.connect(context.destination);
          current.nextTime = Math.max(current.nextTime, context.currentTime + 0.02);
          source.start(current.nextTime); current.nextTime += length / rate; parts = []; length = 0;
        };
        while (this.current === current) {
          while (current.nextTime - context.currentTime > 2 && this.current === current) await new Promise(resolve => setTimeout(resolve, 20));
          if (this.current !== current) return;
          const {value, done} = await reader.read();
          if (done) break;
          // Decode bounded pieces so a large network chunk cannot schedule minutes of audio while paused.
          if (value.byteLength > 1024 * 1024) throw new Error("音频流分块过大，请等待完整 WAV。");
          const bytesPerPiece = Math.floor(rate * 0.2) * 2;
          for (let offset = 0; offset < value.length; offset += bytesPerPiece) {
            while (current.nextTime - context.currentTime > 2 && this.current === current) await new Promise(resolve => setTimeout(resolve, 20));
            if (this.current !== current) return;
            const samples = decoder.decode(value.subarray(offset, offset + bytesPerPiece));
            parts.push(samples); length += samples.length;
            if (length >= rate * 0.2) schedule();
          }
        }
        if (this.current !== current) return;
        if (decoder.tail !== null) throw new Error("音频流不完整，请等待完整 WAV。");
        const status = await fetch(`/api/messages/${id}`, {signal: current.abort.signal}).then(result => result.json());
        if (status.status !== "complete" || status.attempt !== attempt) throw new Error("语音生成未完成，试听已停止；可重试合成。");
        schedule();
        current.complete = true;
        this.finishWhenPlayed(current);
      } catch (error) {
        if (this.current !== current) return;
        this.stop();
        if (error.name !== "AbortError") this.failed(error.message || "试听中断，请等待完整 WAV。");
      }
    }
  }
  globalThis.VoiceAudio = {PCMDecoder, encodeWav, Player};
})();
