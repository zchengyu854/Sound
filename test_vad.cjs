"use strict";
const assert = require("assert");
const {VoiceVad} = require("./web/vad.js");

function frame(rms, length = 320) {
  const out = new Int16Array(length);
  if (rms > 0) {
    const value = Math.min(32767, Math.round(rms));
    for (let i = 0; i < length; i++) out[i] = i % 2 ? -value : value;
  }
  return out;
}

let passed = 0;
function check(name, fn) { fn(); passed++; console.log("ok -", name); }

check("RMS of silence is zero and tone matches amplitude", () => {
  assert.strictEqual(VoiceVad.rms(frame(0)), 0);
  const value = VoiceVad.rms(frame(1000));
  assert.ok(value > 900 && value < 1100, `rms=${value}`);
});

check("emits start after sustained voice and end after silence", () => {
  const vad = new VoiceVad({threshold: 180});
  let starts = 0, ends = 0;
  for (let i = 0; i < 10; i++) vad.push(frame(0)); // noise floor
  for (let i = 0; i < 40; i++) if (vad.push(frame(2000)) === "start") starts++;
  for (let i = 0; i < 20; i++) vad.push(frame(2000));
  for (let i = 0; i < 40; i++) if (vad.push(frame(0)) === "end") ends++;
  assert.strictEqual(starts, 1);
  assert.strictEqual(ends, 1);
  assert.ok(vad.samples().length > 16000 * 0.5);
});

check("pre-roll is kept before the start event", () => {
  const vad = new VoiceVad({threshold: 180, prerollMs: 200});
  for (let i = 0; i < 10; i++) vad.push(frame(120)); // quiet pre-roll
  for (let i = 0; i < 10; i++) vad.push(frame(2000));
  for (let i = 0; i < 10; i++) vad.push(frame(2000));
  for (let i = 0; i < 40; i++) vad.push(frame(0));
  assert.ok(vad.samples().length > 16000 * 0.5);
  const first = vad.samples()[0];
  assert.strictEqual(Math.abs(first), 120); // pre-roll quiet samples retained
});

check("retains the first and threshold-crossing speech frames", () => {
  const vad = new VoiceVad({threshold: 180, prerollMs: 20, startMs: 40, endMs: 40, minMs: 40});
  vad.push(frame(0));
  vad.push(frame(1000));
  assert.strictEqual(vad.push(frame(2000)), "start");
  vad.push(frame(0));
  assert.strictEqual(vad.push(frame(0)), "end");
  const samples = vad.samples();
  assert.strictEqual(Math.abs(samples[320]), 1000);
  assert.strictEqual(Math.abs(samples[640]), 2000);
});

check("short noise bursts do not emit end", () => {
  const vad = new VoiceVad({threshold: 180});
  for (let i = 0; i < 5; i++) vad.push(frame(2000));
  for (let i = 0; i < 6; i++) vad.push(frame(0)); // below start threshold
  let ends = 0;
  for (let i = 0; i < 60; i++) if (vad.push(frame(0)) === "end") ends++;
  assert.strictEqual(ends, 0);
});

check("maximum duration yields timeout and bounded buffer", () => {
  const vad = new VoiceVad({threshold: 180, maxMs: 1000, minMs: 100});
  for (let i = 0; i < 10; i++) vad.push(frame(0));
  let event = null;
  for (let i = 0; i < 200 && event !== "timeout"; i++) {
    const next = vad.push(frame(2000));
    if (next === "timeout") event = next;
  }
  assert.strictEqual(event, "timeout");
  assert.ok(vad.samples().length <= 50 * 320, "pre-roll is included in the maximum sent to the server");
});

check("reset clears state but keeps last result", () => {
  const vad = new VoiceVad({threshold: 180});
  for (let i = 0; i < 10; i++) vad.push(frame(2000));
  for (let i = 0; i < 40; i++) vad.push(frame(0));
  const before = vad.samples();
  vad.reset();
  assert.strictEqual(vad.phase, "idle");
  assert.strictEqual(vad.samples(), before);
});

console.log(`VAD checks passed: ${passed}`);
