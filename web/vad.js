"use strict";
// Energy-based voice activity detector for automatic turn taking.
// Consumes ~20ms PCM16 frames and reports speech start/end using an adaptive
// noise floor. Keeps a bounded pre-roll so the beginning of a word is not lost.
// Browser audio context, microphone and playback are intentionally outside this
// module so the boundary logic can be tested with synthetic frames in Node.
(() => {
  const FRAME_MS = 20;
  const START_MS = 150;
  const END_MS = 700;
  const PREROLL_MS = 200;
  const MAX_MS = 30000;
  const MIN_MS = 500;

  class VoiceVad {
    constructor(options = {}) {
      this.frameMs = options.frameMs ?? FRAME_MS;
      this.startMs = options.startMs ?? START_MS;
      this.endMs = options.endMs ?? END_MS;
      this.prerollMs = options.prerollMs ?? PREROLL_MS;
      this.maxMs = options.maxMs ?? MAX_MS;
      this.minMs = options.minMs ?? MIN_MS;
      this.threshold = options.threshold ?? 180; // initial RMS on 0..32767 scale
      this.calibrationMs = options.calibrationMs ?? 0;
      this.calibrating = this.calibrationMs > 0;
      this.calibrationFrames = 0;
      this.calibrationTotal = 0;
      this.pendingOptions = null;
      this.result = null;
      this.reset();
    }

    configure(options) {
      for (const [key, value] of Object.entries(options)) {
        if (!["endMs", "minMs"].includes(key) || !Number.isFinite(value) || value <= 0) throw new Error("无效的语音停顿参数");
      }
      if (this.phase === "idle") Object.assign(this, options);
      else this.pendingOptions = {...this.pendingOptions, ...options};
    }

    reset() {
      this.phase = "idle"; // idle | starting | speaking | ending
      this.frames = 0;
      this.noise ??= this.threshold * 0.6;
      this.loudCount = 0;
      this.quietCount = 0;
      this.voicedMs = 0;
      this.continuousSpeechMs = 0;
      this.preroll = [];
      this.buffer = [];
      if (this.pendingOptions) Object.assign(this, this.pendingOptions);
      this.pendingOptions = null;
    }

    static rms(frame) {
      if (!frame || !frame.length) return 0;
      let sum = 0;
      for (let index = 0; index < frame.length; index++) {
        const sample = frame[index] / 32768;
        sum += sample * sample;
      }
      return Math.sqrt(sum / frame.length) * 32768;
    }

    // Feed one ~20ms Int16 frame. Returns an event string or null:
    // "start" when speech begins, "end" when a complete utterance is ready,
    // "timeout" when the maximum duration is reached.
    push(frame) {
      const rms = VoiceVad.rms(frame);
      const prerollSize = Math.max(1, Math.round(this.prerollMs / this.frameMs));
      const startFrames = Math.max(1, Math.round(this.startMs / this.frameMs));
      const endFrames = Math.max(1, Math.round(this.endMs / this.frameMs));
      const maxFrames = Math.max(1, Math.floor(this.maxMs / this.frameMs));
      if (this.calibrating) {
        if (rms > Math.max(this.threshold, this.noise * 3)) this.calibrating = false;
        else {
          this.calibrationTotal += rms;
          this.calibrationFrames++;
          this.noise = this.calibrationTotal / this.calibrationFrames;
          if (this.calibrationFrames * this.frameMs >= this.calibrationMs) this.calibrating = false;
        }
      }
      // Adapt the noise floor slowly toward quiet input only.
      if (rms < this.threshold * 1.5) this.noise = this.noise * 0.98 + rms * 0.02;
      const active = rms > Math.max(this.threshold, this.noise * 3);
      this.lastActive = active;
      this.continuousSpeechMs = active ? this.continuousSpeechMs + this.frameMs : 0;
      if (active) this.voicedMs += this.frameMs;

      if (this.phase === "idle") {
        if (active) {
          this.phase = "starting";
          this.buffer = this.preroll.slice();
          this.buffer.push(frame);
          this.preroll = [];
          this.loudCount = 1;
          this.quietCount = 0;
          this.frames = 1;
        } else {
          this.preroll.push(frame);
          if (this.preroll.length > prerollSize) this.preroll.shift();
        }
        return null;
      }

      if (this.phase === "starting") {
        if (active) {
          this.loudCount++;
          this.quietCount = 0;
        } else {
          this.quietCount++;
          if (this.quietCount > startFrames) { this.reset(); return null; }
        }
        this.frames++;
        this.buffer.push(frame);
        if (this.loudCount >= startFrames) {
          this.phase = "speaking";
          return "start";
        }
        return null;
      }

      if (this.phase === "speaking") {
        this.buffer.push(frame);
        this.frames++;
        if (active) this.quietCount = 0;
        else this.quietCount++;
        if (this.buffer.length >= maxFrames) {
          const tooShort = this.frames * this.frameMs < this.minMs;
          this.result = tooShort ? null : this.flatten();
          this.resultVoicedMs = this.voicedMs;
          this.resultEndMs = this.endMs;
          this.resultSilenceMs = this.quietCount * this.frameMs;
          this.reset();
          return tooShort ? null : "timeout";
        }
        if (this.quietCount >= endFrames) {
          const tooShort = this.frames * this.frameMs < this.minMs;
          this.result = tooShort ? null : this.flatten();
          this.resultVoicedMs = this.voicedMs;
          this.resultEndMs = this.endMs;
          this.resultSilenceMs = this.quietCount * this.frameMs;
          this.reset();
          return tooShort ? null : "end";
        }
        return null;
      }
      return null;
    }

    flatten() {
      const total = this.buffer.reduce((sum, frame) => sum + frame.length, 0);
      const result = new Int16Array(total);
      let offset = 0;
      for (const frame of this.buffer) { result.set(frame, offset); offset += frame.length; }
      return result;
    }

    // Latest completed utterance (pre-roll included), consumed after "end".
    samples() { return this.result; }

    get durationMs() { return this.frames * this.frameMs; }
  }

  globalThis.VoiceVad = {VoiceVad, FRAME_MS, START_MS, END_MS, PREROLL_MS, MAX_MS, MIN_MS};
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {VoiceVad, FRAME_MS, START_MS, END_MS, PREROLL_MS, MAX_MS, MIN_MS};
  }
})();
