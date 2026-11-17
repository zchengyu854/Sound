"use strict";
// Captures microphone audio in ~20ms frames at the native context sample rate
// and posts Int16 frames to the main thread for the energy VAD. Never routed to
// the speakers, so browser echo cancellation can do its job without feedback.
class RealtimeCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.frameSize = Math.max(1, Math.round(sampleRate * 0.02));
    this.pending = new Int16Array(0);
  }
  process(inputs) {
    const input = inputs[0];
    if (input && input.length && this.port) {
      const channel = input[0];
      const merged = new Int16Array(this.pending.length + channel.length);
      merged.set(this.pending);
      for (let i = 0; i < channel.length; i++) {
        const value = Math.max(-1, Math.min(1, channel[i]));
        merged[this.pending.length + i] = Math.round(value * (value < 0 ? 32768 : 32767));
      }
      let offset = 0;
      while (offset + this.frameSize <= merged.length) {
        this.port.postMessage(merged.slice(offset, offset + this.frameSize));
        offset += this.frameSize;
      }
      this.pending = merged.slice(offset);
    }
    return true;
  }
}
registerProcessor("realtime-capture", RealtimeCapture);
