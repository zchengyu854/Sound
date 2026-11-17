"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const {randomUUID} = require("node:crypto");
const {createRealtime} = require("./realtime_web/app.js");
const html = fs.readFileSync("./realtime_web/index.html", "utf8");

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
  const elements = new Map([...html.matchAll(/<([a-z-]+)[^>]*\bid="([^"]+)"[^>]*>/g)].map(m => [m[2], new Element(m[1])]));
  return {getElementById: id => elements.get(id) || null, createElement: tag => new Element(tag), elements};
}
class FakeEventSource {
  constructor(url) { this.url = url; FakeEventSource.instances.push(this); }
  emit(data) { if (this.onmessage) this.onmessage({data: JSON.stringify(data)}); }
  close() { this.closed = true; }
}
FakeEventSource.instances = [];

class FakeVad {
  constructor() { this.queue = []; FakeVad.last = this; }
  push() { return this.queue.shift() ?? null; }
  samples() { return new Int16Array(320); }
}
class FakeNode {
  constructor() { this.port = {onmessage: null}; FakeNode.last = this; }
  connect() { return {connect() {}}; }
  disconnect() {}
}
class FakeContext {
  constructor() { this.sampleRate = 16000; this.destination = {}; this.audioWorklet = {addModule: async () => {}}; }
  resume() { return Promise.resolve(); }
  createMediaStreamSource() { return {connect() {}}; }
  createGain() { return {gain: {value: 0}, connect() { return {connect() {}}; }}; }
  close() { return Promise.resolve(); }
}

function fixture(voiceId) {
  FakeEventSource.instances.length = 0;
  FakeNode.last = null;
  FakeVad.last = null;
  const doc = fakeDocument();
  const calls = [];
  const messages = new Map();
  const voices = [{id: "v1", name: "声音一"}, {id: "v2", name: "声音二"}];
  const win = {
    crypto: {randomUUID}, FormData: class { append() {} }, Blob: class { constructor(p) { this.parts = p; } },
    URL: class { constructor(u) { this.href = u; } searchParams = {get: key => key === "voice_id" ? voiceId : null}; },
    Option: class { constructor(text, value) { this.text = text; this.value = value; } },
    location: {href: `http://localhost/realtime/?voice_id=${voiceId || ""}`},
    setTimeout: () => 1, clearTimeout() {},
    EventSource: FakeEventSource,
    AudioContext: FakeContext, AudioWorkletNode: FakeNode,
    navigator: {mediaDevices: {getUserMedia: async () => ({getTracks: () => [{stop() {}}]})}},
    VoiceAudio: {Player: class {
      // Stays pending until stop(), like the real streaming player.
      play() { calls.push(["play"]); return new Promise(resolve => { this.done = resolve; }); }
      stop() { calls.push(["stop"]); if (this.done) this.done(); }
    }, encodeWav: () => new ArrayBuffer(44)},
    VoiceVad: {VoiceVad: FakeVad},
    async fetch(url, options = {}) {
      const method = options.method || "GET";
      calls.push([method, url, options.body && typeof options.body === "string" ? JSON.parse(options.body) : null, options.headers?.["Idempotency-Key"]]);
      if (url === "/api/health") return {ok: true, json: async () => ({busy: false})};
      if (url === "/api/voices") return {ok: true, json: async () => voices};
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
      const cancel = url.match(/^\/api\/messages\/([^/]+)\/cancel$/);
      if (cancel && method === "POST") return {ok: true, json: async () => ({cancel_requested: true})};
      if (url === "/api/transcriptions") return {ok: true, json: async () => ({text: "你好"})};
      return {ok: false, status: 404, json: async () => ({detail: "not found"})};
    },
  };
  return {doc, win, calls, messages, app: createRealtime(doc, win)};
}


async function main() {
  let passed = 0;
  const check = async (name, fn) => { await fn(); passed++; console.log("ok -", name); };

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
    assert.match(bad.doc.elements.get("setup-hint").textContent, /不存在/);
  });

  await check("barge-in stops playback, cancels the reply and submits a new turn", async () => {
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
    for (let i = 0; i < 60 && !f.calls.some(c => c[0] === "stop"); i++) await tick();
    assert.ok(f.calls.some(c => c[0] === "stop"), "playback stopped on barge-in");
    assert.ok(f.calls.some(c => c[0] === "POST" && /\/api\/messages\/msg-1\/cancel$/.test(c[1])),
      "running reply cancelled");
    FakeVad.last.queue = ["end"];
    FakeNode.last.port.onmessage({data: new Int16Array(320)});
    for (let i = 0; i < 80 && !f.calls.some(c => c[1] === "/api/transcriptions"); i++) await tick();
    assert.ok(f.calls.some(c => c[1] === "/api/health"), "waited for model release before transcribing");
    assert.ok(f.calls.some(c => c[1] === "/api/transcriptions"), "new turn transcribed");
    const posts = f.calls.filter(c => c[0] === "POST" && /\/conversations\/conv-1\/messages$/.test(c[1]));
    assert.equal(posts.length, 2, "original plus barge-in turn");
    assert.ok(posts.every(call => call[2].mode === "realtime"));
    void pending;
  });

  await check("end cancels the running reply", async () => {
    const f = fixture("v1");
    await f.app.init(); await f.app.start();
    const tick = () => new Promise(r => setImmediate(r));
    const pending = f.app.send("问一句", 1);
    for (let i = 0; i < 40 && FakeEventSource.instances.length === 0; i++) await tick();
    await f.app.end();
    assert.ok(f.calls.some(c => c[0] === "POST" && /\/api\/messages\/msg-1\/cancel$/.test(c[1])),
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
  });

  console.log(`Realtime UI checks passed: ${passed}`);
}
main().catch(error => { console.error(error); process.exit(1); });
