"use strict";
class VoiceCapture extends AudioWorkletProcessor {
  constructor() {
    super(); this.samples = new Float32Array(4096); this.length = 0;
    this.port.onmessage = event => {
      if (event.data === "flush") { this.flush(); this.port.postMessage("flushed"); }
    };
  }
  flush() {
    if (!this.length) return;
    const output = this.samples.slice(0, this.length);
    this.port.postMessage(output, [output.buffer]); this.length = 0;
  }
  process(inputs) {
    const channels = inputs[0];
    if (channels?.length) {
      for (let index = 0; index < channels[0].length; index++) {
        let value = 0;
        for (const channel of channels) value += channel[index] / channels.length;
        this.samples[this.length++] = value;
        if (this.length === this.samples.length) this.flush();
      }
    }
    return true;
  }
}
registerProcessor("voice-capture", VoiceCapture);
