const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
require("../web/audio.js");
const ROOT = path.join(__dirname, "..");

const encoded = new Uint8Array(VoiceAudio.encodeWav(new Float32Array([-1, -.5, 0, .5, 1])));
const header = new DataView(encoded.buffer);
assert.equal(new TextDecoder().decode(encoded.subarray(0, 4)), "RIFF");
assert.equal(header.getUint32(24, true), 16000);
assert.equal(header.getUint16(22, true), 1);
assert.equal(header.getUint32(40, true), 10);
for (let split = 0; split <= 10; split++) {
  const decoder = new VoiceAudio.PCMDecoder();
  const data = encoded.subarray(44);
  const samples = [...decoder.decode(data.subarray(0, split)), ...decoder.decode(data.subarray(split))];
  assert.deepEqual(samples, [-1, -.5, 0, .5, 32767 / 32768]);
  assert.equal(decoder.tail, null);
}

const starts = [], contexts = [], failures = [];
global.AudioContext = class {
  constructor() { this.currentTime = 0; contexts.push(this); }
  async resume() {}
  async close() { this.closed = true; }
  createBuffer(channels, length, rate) { return {getChannelData: () => new Float32Array(length), duration: length / rate}; }
  createBufferSource() { return {connect() {}, start(time) { starts.push(time); }}; }
};

async function main() {
  let sent = false, streamReads = 0, finish;
  const player = new VoiceAudio.Player(() => {}, error => failures.push(error));
  assert.equal(contexts.length, 0);
  global.fetch = async path => {
    if (path.includes("/stream")) {
      streamReads++;
      return {ok: true, headers: new Map([["X-Audio-Sample-Rate", "8000"], ["X-Audio-Channels", "1"], ["X-Audio-Format", "pcm_s16le"]]),
        body: {getReader: () => ({read: async () => {
          if (!sent) { sent = true; return {value: new Uint8Array(3200), done: false}; }
          return new Promise(resolve => { finish = () => resolve({done: true}); });
        }})}};
    }
    return {json: async () => ({status: "complete", attempt: 1})};
  };
  const playing = player.play("message", 1);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(starts.length, 1, "first audio must be scheduled before stream EOF");
  assert.equal(player.playing("message"), true);
  assert.equal(streamReads, 1);
  assert.equal(contexts[0].closed, undefined);
  finish(); await playing;
  assert.equal(player.playing("message"), true, "completion must not cut off scheduled audio");
  player.stop();
  assert.equal(contexts[0].closed, true);
  assert.equal(player.playing("message"), false);
  assert.deepEqual(failures, []);

  const recorderSource = fs.readFileSync(path.join(ROOT, "web/recorder.js"), "utf8") + "\nglobalThis.TestRecorder = VoiceRecorder;";
  let grant, stopped = 0, transcripts = 0;
  const recorderContext = vm.createContext({navigator: {mediaDevices: {getUserMedia: () => new Promise(resolve => { grant = resolve; })}},
    window: {AudioWorkletNode: class {}, AudioContext, OfflineAudioContext: class {}}, AudioContext, AbortController,
    setTimeout, clearTimeout});
  vm.runInContext(recorderSource, recorderContext);
  const recorder = new recorderContext.TestRecorder(() => {}, () => transcripts++, error => failures.push(error));
  const requesting = recorder.start();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(recorder.phase, "requesting");
  recorder.cancel();
  grant({getTracks: () => [{stop: () => stopped++}]});
  await requesting;
  assert.equal(stopped, 1, "cancel during permission request must release late microphone");
  assert.equal(recorder.phase, "idle");
  assert.equal(transcripts, 0);

  let capturedNode, finishFetch, draft = "", requests = 0;
  class RecordingContext extends AudioContext {
    constructor() { super(); this.sampleRate = 16000; this.audioWorklet = {addModule: async () => {}}; }
    createMediaStreamSource() { return {connect: target => target, disconnect() {}}; }
    createGain() { return {gain: {}, connect() {}}; }
  }
  class CaptureNode {
    constructor() {
      capturedNode = this;
      this.port = {postMessage: () => this.port.onmessage({data: "flushed"})};
    }
    connect(target) { return target; }
    disconnect() {}
  }
  class OfflineContext {
    constructor(channels, length) { this.samples = new Float32Array(length); }
    createBuffer(channels, length) { const data = new Float32Array(length); return {getChannelData: () => data}; }
    createBufferSource() { return {connect() {}, start() {}}; }
    async startRendering() { return {getChannelData: () => this.samples}; }
  }
  const flowContext = vm.createContext({navigator: {mediaDevices: {getUserMedia: async () => ({getTracks: () => [{stop() {}}]})}},
    window: {AudioWorkletNode: CaptureNode, AudioContext: RecordingContext, OfflineAudioContext: OfflineContext},
    AudioContext: RecordingContext, AudioWorkletNode: CaptureNode, OfflineAudioContext: OfflineContext,
    AbortController, setTimeout, clearTimeout, FormData, Blob, Float32Array, VoiceAudio,
    fetch: async () => { requests++; return new Promise(resolve => { finishFetch = () => resolve({ok: true, json: async () => ({text: "识别草稿"})}); }); }});
  vm.runInContext(recorderSource, flowContext);
  const flow = new flowContext.TestRecorder(() => {}, text => { draft = text; }, error => failures.push(error));
  await flow.start();
  capturedNode.port.onmessage({data: new Float32Array(16000)});
  const transcribing = flow.finish();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(flow.phase, "transcribing");
  assert.equal(requests, 1);
  flow.cancel(); finishFetch(); await transcribing;
  assert.equal(draft, "", "late transcription must not overwrite another conversation");
  await flow.start();
  capturedNode.port.onmessage({data: new Float32Array(16000)});
  const accepted = flow.finish();
  await new Promise(resolve => setImmediate(resolve));
  finishFetch(); await accepted;
  assert.equal(draft, "识别草稿");
  assert.equal(requests, 2, "recording must only call transcription, never submit chat");
  assert.equal(flow.phase, "idle");
  assert.deepEqual(failures, []);

  let capture;
  class AudioWorkletProcessor { constructor() { this.port = {postMessage: data => capture.push(data)}; } }
  const workletContext = vm.createContext({AudioWorkletProcessor, Float32Array, registerProcessor: (name, processor) => { workletContext.Processor = processor; }});
  vm.runInContext(fs.readFileSync(path.join(ROOT, "web/recorder-worklet.js"), "utf8"), workletContext);
  capture = [];
  const worklet = new workletContext.Processor();
  worklet.process([[new Float32Array([1, -1]), new Float32Array([0, 0])]]);
  worklet.port.onmessage({data: "flush"});
  assert.deepEqual([...capture[0]], [.5, -.5]);
  assert.equal(capture[1], "flushed");
  console.log("PCM decoding, live playback, microphone cancellation, and worklet checks passed");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
