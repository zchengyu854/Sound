"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {randomUUID} = require("node:crypto");
const {createRealtime} = require("../realtime_web/app.js");
const ROOT = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "realtime_web/index.html"), "utf8");

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {}; this.dataset = {};
    this.className = ""; this.hidden = false; this.disabled = false; this.value = ""; this._text = ""; this.href = "";
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ""; this.children = children; }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  async emit(name, options = {}) { for (const fn of this.listeners[name] || []) await fn({target: this, preventDefault() {}, ...options}); }
}
function fakeDocument() {
  const elements = new Map([...html.matchAll(/<([a-z-]+)[^>]*\bid="([^"]+)"[^>]*>/g)].map(match => {
    const element = new Element(match[1]);
    element.hidden = /\bhidden\b/.test(match[0]);
    element.disabled = /\bdisabled\b/.test(match[0]);
    return [match[2], element];
  }));
  return {getElementById: id => elements.get(id) || null, createElement: tag => new Element(tag), elements};
}
class FakeEventSource {
  constructor(url) { this.url = url; FakeEventSource.instances.push(this); }
  emit(data) { if (this.onmessage) this.onmessage({data: JSON.stringify(data)}); }
  close() { this.closed = true; }
}
FakeEventSource.instances = [];

class FakeVad {
  constructor(options) { this.queue = []; this.options = options; this.frameMs = 20; this.continuousSpeechMs = 0; this.quietCount = 0; FakeVad.last = this; }
  configure(options) { Object.assign(this.options, options); }
  reset() { this.continuousSpeechMs = 0; this.quietCount = 0; }
  push() {
    const next = this.queue.shift();
    if (typeof next === "object" && next) { const {event, ...fields} = next; Object.assign(this, fields); return event || null; }
    if (next === "start") { this.continuousSpeechMs = 160; this.quietCount = 0; }
    return next ?? null;
  }
  samples() { return Int16Array.from([0, 16384, -16384, 32767, -32768]); }
}
class FakeNode {
  constructor() { this.port = {onmessage: null}; FakeNode.last = this; }
  connect() { return {connect() {}}; }
  disconnect() {}
}
class FakeContext {
  constructor() { this.sampleRate = 16000; this.destination = {}; this.audioWorklet = {addModule: async () => {}}; }
  resume() { return Promise.resolve(); }
  createMediaStreamSource() { return {connect() {}, disconnect() {}}; }
  createGain() { return {gain: {value: 0}, connect() { return {connect() {}}; }, disconnect() {}}; }
  close() { return Promise.resolve(); }
}

function fixture(voiceId, characters = [], characterId = null) {
  FakeEventSource.instances.length = 0;
  FakeNode.last = null;
  FakeVad.last = null;
  const doc = fakeDocument();
  const calls = [];
  const messages = new Map();
  const timers = new Map();
  let timerId = 0;
  const voices = [{id: "v1", name: "声音一"}, {id: "v2", name: "声音二"}];
  const win = {
    crypto: {randomUUID}, FormData: class { append() {} }, Blob: class { constructor(p) { this.parts = p; } },
    URL: class { constructor(u) { this.href = u; } searchParams = {get: key => key === "voice_id" ? voiceId : key === "character_id" ? characterId : null}; },
    Option: class { constructor(text, value) { this.text = text; this.value = value; } },
    location: {href: `http://localhost/realtime/?voice_id=${voiceId || ""}`},
    setTimeout: (fn, delay) => { const id = ++timerId; timers.set(id, {fn, delay}); return id; }, clearTimeout: id => timers.delete(id),
    EventSource: FakeEventSource,
    AudioContext: FakeContext, AudioWorkletNode: FakeNode,
    navigator: {mediaDevices: {getUserMedia: async () => ({getTracks: () => [{stop() {}}]})}},
    VoiceAudio: {Player: class {
      constructor(changed) { this.changed = changed; this.active = false; }
      playing() { return this.active; }
      play() { this.active = true; calls.push(["play"]); this.changed(); return Promise.resolve(); }
      pause(id, attempt) { this.paused = true; calls.push(["pause", id, attempt]); return Promise.resolve(true); }
      resume(id, attempt) { this.paused = false; calls.push(["resume", id, attempt]); return Promise.resolve(this.active); }
      stop() { calls.push(["stop"]); this.active = false; this.changed(); }
    }, encodeWav: samples => { calls.push(["encoded", [...samples]]); return new ArrayBuffer(44); }},
    VoiceVad: {VoiceVad: FakeVad},
    async fetch(url, options = {}) {
      const method = options.method || "GET";
      calls.push([method, url, options.body && typeof options.body === "string" ? JSON.parse(options.body) : null, options.headers?.["Idempotency-Key"]]);
      if (url === "/api/health") return {ok: true, json: async () => ({busy: false})};
      if (url === "/api/voices") return {ok: true, json: async () => voices};
      if (url === "/api/characters") return {ok: true, json: async () => characters};
      const oneCharacter = url.match(/^\/api\/characters\/([^/]+)$/);
      if (oneCharacter) {
        const found = characters.find(character => character.id === decodeURIComponent(oneCharacter[1]));
        return found ? {ok: true, json: async () => found} : {ok: false, status: 404, json: async () => ({detail: "人物不存在"})};
      }
      const oneVoice = url.match(/^\/api\/voices\/([^/]+)$/);
      if (oneVoice) {
        const found = voices.find(v => v.id === decodeURIComponent(oneVoice[1]));
        return found ? {ok: true, json: async () => found} : {ok: false, status: 404, json: async () => ({detail: "未找到该声音、会话或消息。"})};
      }
      if (url === "/api/conversations" && method === "POST") return {ok: true, json: async () => ({id: "conv-1"})};
      const msgNew = url.match(/^\/api\/conversations\/([^/]+)\/messages$/);
      if (msgNew && method === "POST") { messages.set("msg-1", {id: "msg-1", attempt: 1, status: "queued", content: ""}); return {ok: true, json: async () => ({id: "msg-1", attempt: 1})}; }
      const msgGet = url.match(/^\/api\/messages\/([^/]+)$/);
      if (msgGet) return {ok: true, json: async () => messages.get(msgGet[1])};
      const cancel = url.match(/^\/api\/messages\/([^/]+)\/cancel\?attempt=\d+$/);
      if (cancel && method === "POST") return {ok: true, json: async () => ({cancel_requested: true})};
      if (url === "/api/transcriptions") return {ok: true, json: async () => ({text: "你好"})};
      return {ok: false, status: 404, json: async () => ({detail: "not found"})};
    },
  };
  return {doc, win, calls, messages, timers, app: createRealtime(doc, win)};
}


async function main() {
  let passed = 0;
  const check = async (name, fn) => { await fn(); passed++; console.log("ok -", name); };
  async function speaking(f) {
    await f.app.init(); await f.app.start();
    await f.app.send("说一句", 1);
    const source = FakeEventSource.instances.at(-1);
    source.emit({id: "msg-1", attempt: 1, status: "synthesizing", mode: "realtime", content: "你好"});
    await new Promise(resolve => setImmediate(resolve));
    return source;
  }

  await check("loads voices and selects URL voice_id", async () => {
    const f = fixture("v2");
    await f.app.init();
    assert.equal(f.app.state.voiceId, "v2");
    assert.equal(f.doc.elements.get("start").disabled, false);
    assert.ok(f.doc.elements.get("voice-select").children.some(o => o.value === "v2"));
  });

  await check("invalid URL voice_id falls back to first voice", async () => {
    const f = fixture("missing");
    await f.app.init();
    assert.equal(f.app.state.voiceId, "v1");
  });

  await check("start creates voice-only conversation", async () => {
    const f = fixture("v1");
    await f.app.init();
    await f.app.start();
    assert.equal(f.app.state.conversationId, "conv-1");
    const called = f.calls.find(c => c[1] === "/api/conversations" && c[0] === "POST");
    assert.deepEqual(called[2], {voice_id: "v1"});
    assert.equal(f.doc.elements.get("stage").hidden, false);
    assert.equal(f.app.state.listening, true, "one click starts microphone capture");
  });

  await check("a character link starts a character conversation", async () => {
    const f = fixture(null, [{id: "c1", name: "小林", voice_id: "v1"}], "c1");
    await f.app.init();
    assert.equal(f.app.state.characterId, "c1");
    await f.app.start();
    const called = f.calls.find(call => call[1] === "/api/conversations" && call[0] === "POST");
    assert.deepEqual(called[2], {character_id: "c1"});
    assert.ok(f.calls.some(call => call[1] === "/api/characters/c1"));
  });

  await check("characters sharing a voice and the voice alone keep separate selections", async () => {
    const f = fixture(null, [{id: "c1", name: "小林", voice_id: "v1"}, {id: "c2", name: "小许", voice_id: "v1"}]);
    await f.app.init();
    const select = f.doc.elements.get("voice-select");
    for (const value of ["c:c1", "c:c2", "v1"]) {
      select.value = value;
      await select.emit("change");
      await f.app.start();
      await f.app.end();
    }
    assert.deepEqual(f.calls.filter(([method, url]) => method === "POST" && url === "/api/conversations").map(call => call[2]),
      [{character_id: "c1"}, {character_id: "c2"}, {voice_id: "v1"}]);
  });

  await check("send uses idempotency key and plays after completion", async () => {
    const f = fixture("v1");
    await f.app.init(); await f.app.start();
    const pending = f.app.send("你好", 1);
    for (let i = 0; i < 20 && FakeEventSource.instances.length === 0; i++) await new Promise(r => setImmediate(r));
    const sendCall = f.calls.find(c => c[0] === "POST" && /\/messages$/.test(c[1]));
    assert.ok(sendCall[3], "idempotency key present");
    assert.equal(sendCall[2].mode, "realtime");
    const source = FakeEventSource.instances.at(-1);
    source.emit({id: "msg-1", attempt: 1, status: "complete", content: "你好呀"});
    await pending;
    for (let i = 0; i < 20 && !f.calls.some(c => c[0] === "play"); i++) await new Promise(r => setImmediate(r));
    assert.ok(f.calls.some(c => c[0] === "play"));
  });

  await check("stale round does not play old reply", async () => {
    const f = fixture("v1");
    await f.app.init(); await f.app.start();
    const pending = f.app.send("旧问题", 1);
    await new Promise(r => setImmediate(r));
    f.app.state.round = 2; // user interrupted with a new turn
    const source = FakeEventSource.instances.at(-1);
    source.emit({id: "msg-1", attempt: 1, status: "complete", content: "旧回复"});
    await pending;
    assert.ok(!f.calls.some(c => c[0] === "play"), "old round must not play");
  });

  await check("manual input sends text", async () => {
    const f = fixture("v1");
    await f.app.init(); await f.app.start();
    f.doc.elements.get("manual-input").value = " 手动输入 ";
    await f.doc.elements.get("manual-send").emit("click");
    await new Promise(r => setImmediate(r));
    const sendCall = f.calls.find(c => c[0] === "POST" && /\/messages$/.test(c[1]));
    assert.equal(sendCall[2].content, "手动输入");
  });

  await check("a lost send response is retried with the same idempotency key", async () => {
    const f = fixture("v1");
    const originalFetch = f.win.fetch;
    let savedResponse;
    const keys = [];
    f.win.fetch = async (url, options = {}) => {
      if (options.method === "POST" && /\/conversations\/conv-1\/messages$/.test(url)) {
        keys.push(options.headers["Idempotency-Key"]);
        if (!savedResponse) {
          savedResponse = await originalFetch(url, options);
          throw new Error("connection lost after acceptance");
        }
        return savedResponse;
      }
      return originalFetch(url, options);
    };
    await f.app.init(); await f.app.start();
    await f.app.send("你好", 1);
    assert.equal(keys.length, 2);
    assert.equal(keys[0], keys[1]);
    assert.equal(f.calls.filter(call => call[0] === "POST" && /\/conversations\/conv-1\/messages$/.test(call[1])).length, 1);
  });

  await check("starts playback early on synthesizing in realtime mode", async () => {
    const f = fixture("v1");
    await f.app.init(); await f.app.start();
    const pending = f.app.send("你好", 1);
    for (let i = 0; i < 20 && FakeEventSource.instances.length === 0; i++) await new Promise(r => setImmediate(r));
    const source = FakeEventSource.instances.at(-1);
    source.emit({id: "msg-1", attempt: 1, status: "synthesizing", mode: "realtime", content: "你好"});
    for (let i = 0; i < 20 && !f.calls.some(c => c[0] === "play"); i++) await new Promise(r => setImmediate(r));
    assert.ok(f.calls.some(c => c[0] === "play"), "playback starts while text streams");
    source.emit({id: "msg-1", attempt: 1, status: "complete", mode: "realtime", content: "你好呀"});
    await pending;
  });

  await check("interrupted round stops playback and reports it", async () => {
    const f = fixture("v1");
    await f.app.init(); await f.app.start();
    const pending = f.app.send("说点什么", 1);
    for (let i = 0; i < 20 && FakeEventSource.instances.length === 0; i++) await new Promise(r => setImmediate(r));
    const source = FakeEventSource.instances.at(-1);
    source.emit({id: "msg-1", attempt: 1, status: "synthesizing", mode: "realtime", content: "说点什么"});
    for (let i = 0; i < 20 && !f.calls.some(c => c[0] === "play"); i++) await new Promise(r => setImmediate(r));
    source.emit({id: "msg-1", attempt: 1, status: "interrupted", mode: "realtime", error_code: "interrupted"});
    await pending;
    assert.ok(f.calls.some(c => c[0] === "stop"), "playback stopped on barge-in");
    assert.match(f.doc.elements.get("status").textContent, /打断/);
  });

  await check("validates the URL voice on the server", async () => {
    const good = fixture("v2");
    await good.app.init();
    assert.equal(good.app.state.voiceId, "v2");
    assert.ok(good.calls.some(call => call[1] === "/api/voices/v2"), "server lookup used");
    const bad = fixture("missing");
    await bad.app.init();
    assert.equal(bad.app.state.voiceId, "v1");
    assert.match(bad.doc.elements.get("setup-hint").textContent, /不可用/);
  });

  await check("suspected speech pauses first; sustained speech cancels and submits once", async () => {
    const f = fixture("v1");
    await f.app.init(); await f.app.start();
    const tick = () => new Promise(r => setImmediate(r));
    const pending = f.app.send("问一句", 1);
    for (let i = 0; i < 40 && FakeEventSource.instances.length === 0; i++) await tick();
    const source = FakeEventSource.instances.at(-1);
    source.emit({id: "msg-1", attempt: 1, status: "synthesizing", mode: "realtime", content: "问"});
    for (let i = 0; i < 60 && !(f.calls.some(c => c[0] === "play") && FakeNode.last); i++) await tick();
    assert.ok(f.calls.some(c => c[0] === "play"), "playback started");
    FakeVad.last.queue = ["start"];
    FakeNode.last.port.onmessage({data: new Int16Array(320)});
    assert.ok(f.calls.some(c => c[0] === "pause"), "suspected interruption pauses playback");
    assert.ok(!source.closed, "SSE must stay open until interruption is confirmed");
    assert.ok(!f.calls.some(c => /\/cancel/.test(c[1] || "")), "suspected speech must not cancel the reply");
    FakeVad.last.queue = [{continuousSpeechMs: 360}];
    FakeNode.last.port.onmessage({data: new Int16Array(320)});
    for (let i = 0; i < 60 && !f.calls.some(c => c[0] === "stop"); i++) await tick();
    assert.ok(f.calls.some(c => c[0] === "stop"), "playback stopped on barge-in");
    assert.ok(f.calls.some(c => c[0] === "POST" && /\/api\/messages\/msg-1\/cancel\?attempt=1$/.test(c[1])),
      "running reply cancelled");
    FakeVad.last.queue = ["end"];
    FakeNode.last.port.onmessage({data: new Int16Array(320)});
    for (let i = 0; i < 80 && !f.calls.some(c => c[1] === "/api/transcriptions"); i++) await tick();
    assert.ok(f.calls.some(c => c[1] === "/api/health"), "waited for model release before transcribing");
    assert.ok(f.calls.some(c => c[1] === "/api/transcriptions"), "new turn transcribed");
    const samples = f.calls.find(call => call[0] === "encoded")[1];
    assert.deepEqual(samples.slice(0, 3), [0, 0.5, -0.5], "PCM is normalized before WAV encoding");
    const posts = f.calls.filter(c => c[0] === "POST" && /\/conversations\/conv-1\/messages$/.test(c[1]));
    assert.equal(posts.length, 2, "original plus barge-in turn");
    assert.ok(posts.every(call => call[2].mode === "realtime"));
    void pending;
  });

  await check("short noise resumes the same player and preserves the reply stream", async () => {
    const f = fixture("v1");
    const source = await speaking(f);
    const node = FakeNode.last;
    FakeVad.last.queue = ["start", {quietCount: 9, continuousSpeechMs: 0}];
    node.port.onmessage({data: new Int16Array(320)});
    node.port.onmessage({data: new Int16Array(320)});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.app.state.interruption, "dismissed");
    assert.equal(f.app.state.round, 1);
    assert.ok(!source.closed, "noise must preserve the original SSE");
    assert.equal(f.calls.filter(call => call[0] === "pause").length, 1);
    assert.equal(f.calls.filter(call => call[0] === "resume").length, 1);
    assert.equal(f.calls.filter(call => call[0] === "play").length, 1, "resuming does not replay the message");
    assert.ok(!f.calls.some(call => /\/cancel/.test(call[1] || "")));
    assert.ok(!f.calls.some(call => call[1] === "/api/transcriptions"));
  });

  await check("completion during a suspected interruption still resumes buffered audio", async () => {
    const f = fixture("v1");
    const source = await speaking(f);
    FakeVad.last.queue = ["start"];
    FakeNode.last.port.onmessage({data: new Int16Array(320)});
    source.emit({id: "msg-1", attempt: 1, status: "complete", mode: "realtime", content: "你好呀"});
    assert.equal(f.app.state.activeMessageId, "");
    FakeVad.last.queue = [{event: "end", continuousSpeechMs: 0}];
    FakeNode.last.port.onmessage({data: new Int16Array(320)});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.calls.filter(call => call[0] === "resume").length, 1);
    assert.equal(f.app.state.playing, true);
    assert.ok(!f.calls.some(call => /\/cancel/.test(call[1] || "")));
  });

  await check("unclassified interruption times out and targets the original attempt", async () => {
    const f = fixture("v1");
    const source = await speaking(f);
    FakeVad.last.queue = ["start"];
    FakeNode.last.port.onmessage({data: new Int16Array(320)});
    const timer = [...f.timers.values()].find(timer => timer.delay === 2000);
    assert.ok(timer);
    timer.fn();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.app.state.interruption, "confirmed");
    assert.ok(source.closed);
    assert.ok(f.calls.some(call => call[1] === "/api/messages/msg-1/cancel?attempt=1"));
    assert.ok(f.app.state.metrics.some(item => item.kind === "confirmed" && item.elapsed_ms >= 0));
  });

  await check("ending discards a pending pause and its delayed timeout", async () => {
    const f = fixture("v1");
    await speaking(f);
    FakeVad.last.queue = ["start"];
    FakeNode.last.port.onmessage({data: new Int16Array(320)});
    const timer = [...f.timers.values()].find(timer => timer.delay === 2000);
    await f.app.end();
    timer.fn();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.calls.filter(call => /\/cancel/.test(call[1] || "")).length, 1);
    assert.equal(f.calls.filter(call => call[0] === "resume").length, 0);
    assert.equal(f.app.state.ended, true);
  });

  await check("a retried reply cannot overwrite or resume an old attempt", async () => {
    const f = fixture("v1");
    const source = await speaking(f);
    source.emit({id: "msg-1", attempt: 2, status: "complete", mode: "realtime", content: "其他页面重试的内容"});
    assert.equal(source.closed, true);
    assert.equal(f.app.state.activeMessageId, "");
    assert.match(f.doc.elements.get("status").textContent, /其他页面重试/);
    assert.ok(!f.doc.elements.get("conversation").textContent.includes("其他页面重试的内容"));
  });

  await check("capture requests calibration and follows the selected pause preset", async () => {
    const f = fixture("v1");
    await f.app.init(); await f.app.start();
    assert.equal(FakeVad.last.options.calibrationMs, 1000);
    assert.equal(FakeVad.last.options.endMs, 700);
    f.doc.elements.get("pause-preset").value = "slow";
    FakeNode.last.port.onmessage({data: new Int16Array(320)});
    assert.equal(FakeVad.last.options.endMs, 1100);
    assert.equal(FakeVad.last.options.minMs, 500);
    await f.app.send("说一句", 1);
    FakeNode.last.port.onmessage({data: new Int16Array(320)});
    assert.equal(FakeVad.last.options.minMs, 150, "interruption capture can retain short takeover words for future semantic validation");
  });

  await check("end cancels the running reply", async () => {
    const f = fixture("v1");
    await f.app.init(); await f.app.start();
    const tick = () => new Promise(r => setImmediate(r));
    const pending = f.app.send("问一句", 1);
    for (let i = 0; i < 40 && FakeEventSource.instances.length === 0; i++) await tick();
    await f.app.end();
    assert.ok(f.calls.some(c => c[0] === "POST" && /\/api\/messages\/msg-1\/cancel\?attempt=1$/.test(c[1])),
      "ending the conversation releases the model");
    void pending;
  });

  await check("end stops playback and resets", async () => {
    const f = fixture("v1");
    await f.app.init(); await f.app.start();
    await f.app.end();
    assert.equal(f.app.state.ended, true);
    assert.equal(f.doc.elements.get("stage").hidden, true);
    assert.equal(f.doc.elements.get("setup").hidden, false);
    await f.app.start();
    assert.equal(f.app.state.ended, false);
    assert.equal(f.app.state.listening, true, "a new conversation can start after ending");
  });

  await check("capture continues while streamed audio plays and after it ends", async () => {
    const f = fixture("v1");
    await f.app.init(); await f.app.start();
    await f.app.send("你好", 1);
    const source = FakeEventSource.instances.at(-1);
    source.emit({id: "msg-1", attempt: 1, status: "synthesizing", mode: "realtime", content: "你好"});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.app.state.playing, true, "play promise resolution does not mean playback finished");
    assert.equal(f.app.state.listening, true, "microphone remains active for interruption");
    source.emit({id: "msg-1", attempt: 1, status: "complete", mode: "realtime", content: "你好呀"});
    f.app.stopPlayback();
    assert.equal(f.app.state.playing, false);
    assert.equal(f.app.state.listening, true, "microphone remains active for the next turn");
  });

  await check("ending during microphone permission releases a late stream", async () => {
    const f = fixture("v1");
    let grant, stopped = 0;
    f.win.navigator.mediaDevices.getUserMedia = () => new Promise(resolve => { grant = resolve; });
    await f.app.init();
    const pending = f.app.start();
    await new Promise(resolve => setImmediate(resolve));
    await f.app.end();
    grant({getTracks: () => [{stop() { stopped++; }}]});
    await pending;
    assert.equal(stopped, 1);
    assert.equal(f.app.state.listening, false);
    assert.ok(!f.calls.some(call => call[0] === "POST" && call[1] === "/api/conversations"));
  });

  await check("microphone denial leaves an actionable setup error", async () => {
    const f = fixture("v1");
    f.win.navigator.mediaDevices.getUserMedia = async () => { throw new Error("denied"); };
    await f.app.init(); await f.app.start();
    assert.match(f.doc.elements.get("setup-hint").textContent, /麦克风/);
    assert.equal(f.doc.elements.get("stage").hidden, true);
    assert.ok(!f.calls.some(call => call[0] === "POST" && call[1] === "/api/conversations"));
  });

  await check("a late transcription cannot send into an ended conversation", async () => {
    const f = fixture("v1");
    const originalFetch = f.win.fetch;
    let finishTranscription;
    f.win.fetch = (url, options) => url === "/api/transcriptions"
      ? new Promise(resolve => { finishTranscription = resolve; })
      : originalFetch(url, options);
    await f.app.init(); await f.app.start();
    const pending = f.app.submitUtterance(new Int16Array(16000), 16000);
    for (let index = 0; index < 20 && !finishTranscription; index++) await new Promise(resolve => setImmediate(resolve));
    await f.app.end();
    finishTranscription({ok: true, json: async () => ({text: "迟到的结果"})});
    await pending;
    assert.ok(!f.calls.some(call => call[0] === "POST" && /\/conversations\/conv-1\/messages$/.test(call[1])));
    assert.equal(f.doc.elements.get("conversation").children.length, 0);
  });

  console.log(`Realtime UI checks passed: ${passed}`);
}
main().catch(error => { console.error(error); process.exit(1); });
