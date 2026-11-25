"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Element {
  constructor(tag = "div") { this.tagName = tag; this.children = []; this.events = {}; this.value = ""; this.hidden = false; this.disabled = false; this.textContent = ""; this.open = false; this.className = ""; }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.textContent = ""; this.append(...children); }
  addEventListener(event, fn) { (this.events[event] ||= []).push(fn); }
  async emit(event) { for (const fn of this.events[event] || []) await fn({target: this, preventDefault() {}}); }
  setAttribute(name, value) { this[name] = value; }
  querySelector(selector) {
    for (const child of this.children) {
      if (selector.startsWith(".") ? child.className.split(" ").includes(selector.slice(1)) : child.tagName === selector) return child;
      const nested = child.querySelector(selector); if (nested) return nested;
    }
    return null;
  }
  remove() { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); }
  replaceWith(other) { const index = this.parent.children.indexOf(this); this.parent.children[index] = other; other.parent = this.parent; }
  focus() { this.focused = true; }
  showModal() { this.open = true; }
  close() { this.open = false; return this.emit("close"); }
}

const source = fs.readFileSync(require.resolve("../web/app.js"), "utf8");
const section = source.slice(source.indexOf("let summaryConversation ="), source.lastIndexOf("start();"));
function fixture() {
  const elements = new Map();
  const $ = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const messages = [{id: "u1", role: "user", content: "<img src=x> 请下次继续讨论", created_at: "2026-11-23T10:00:00Z"}];
  const state = {messages, conversations: [{id: "conv", voice_name: "纯声音"}, {id: "other", voice_name: "另一声音"}], current: "conv", characters: [{id: "c1", name: "小林"}], voices: [{id: "v1", name: "声音"}]};
  const item = {kind: "open_thread", text: "继续整理面试问题", source_message_ids: ["u1"]};
  const f = {elements, $, state, calls: [], summary: {current_revision_id: "r1", items: [item], covered_until_assistant_id: "a1", status: "idle"},
    profile: {profile_revision_id: "p1"}, candidates: {items: [], status: "idle"}, hook: null};
  const api = async (path, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : undefined;
    f.calls.push({path, method: options.method || "GET", body});
    if (f.hook) { const result = f.hook(path, options, body); if (result !== undefined) return result; }
    if (path.includes("/summary/revisions")) return {items: [{id: "old", origin: "auto", created_at: "2026-11-23T10:00:00Z", items: [item]}], next_offset: null};
    if (path.endsWith("/summary")) return f.summary;
    if (path.endsWith("/profile-candidates")) return f.candidates;
    if (path.includes("/profile-revisions")) return {items: [{id: "p1", origin: "manual", created_at: "2026-11-23T10:00:00Z", profile: {name: "小林", voice_id: "v1", examples: []}}], next_offset: null};
    if (path === "characters/c1") return f.profile;
    throw new Error(`Unexpected API ${path}`);
  };
  const ctx = vm.createContext({$, state, api, post: (path, body) => api(path, {method: "POST", body: JSON.stringify(body)}), node: (tag, className, text) => Object.assign(new Element(tag), {className: className || "", textContent: text ?? ""}),
    currentConversation: () => state.conversations.find(c => c.id === state.current), displayName: c => c.voice_name, characterFields: ["name", "background", "personality", "relationship", "speech_style"],
    toast() {}, newConversation: async selection => f.calls.push({newConversation: selection}), Date, console});
  vm.runInContext(section, ctx);
  f.ctx = ctx; f.read = expression => vm.runInContext(expression, ctx);
  return f;
}
const clone = value => JSON.parse(JSON.stringify(value));
const buttons = row => row.children.filter(child => child.tagName === "button");
function candidate() {
  return {id: "g1", field: "speech_style", old_value: "喜欢长句", new_value: "先说结论", reason: "两次明确提出偏好", base_revision_id: "p1", base_memory_version: 2, review_token: "token-1", created_at: "2026-11-23T10:00:00Z", sources: [{source_id: "u1", text: "<script>请先说结论</script>", created_at: "2026-11-23T10:00:00Z"}]};
}

(async () => {
  let checks = 0;
  async function check(name, fn) { await fn(); checks++; console.log("ok -", name); }
  await check("pure voice conversations can inspect a sourced summary", async () => {
    const f = fixture(); await f.ctx.openSummary();
    assert.equal(f.$("summary-dialog").open, true);
    assert.equal(f.read("summaryBase"), "r1");
    const details = f.read("summaryEditors[0].row").querySelector("details");
    assert.equal(details.querySelector("blockquote").textContent, f.state.messages[0].content);
    assert.equal(details.querySelector("blockquote").children.length, 0, "source is plain text");
  });
  await check("summary edits retain source IDs and send a revision baseline", async () => {
    const f = fixture(); await f.ctx.openSummary();
    const editor = f.read("summaryEditors[0]"); editor.input.value = "继续讨论交通安排"; await editor.input.emit("input");
    f.hook = (path, options, body) => { if (options.method === "PUT") { f.summary = {...f.summary, current_revision_id: "r2", items: body.items}; return f.summary; } };
    await f.$("summary-form").emit("submit");
    assert.deepEqual(f.calls.find(call => call.method === "PUT").body, {base_revision_id: "r1", items: [{kind: "open_thread", text: "继续讨论交通安排", source_message_ids: ["u1"]}]});
    assert.equal(f.read("summaryDirty"), false);
  });
  await check("background summary updates preserve drafts and require re-reading", async () => {
    const f = fixture(); await f.ctx.openSummary();
    const editor = f.read("summaryEditors[0]"); editor.input.value = "我的草稿"; await editor.input.emit("input");
    f.summary = {...f.summary, current_revision_id: "r2", items: [{...f.summary.items[0], text: "后台新摘要"}]};
    await f.ctx.loadSummary();
    assert.equal(f.read("summaryEditors[0].input"), editor.input);
    assert.equal(editor.input.value, "我的草稿");
    assert.equal(f.$("save-summary").disabled, true);
    await f.$("reload-summary").emit("click");
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.read("summaryEditors[0].input.value"), "后台新摘要");
    assert.equal(f.read("summaryBase"), "r2");
  });
  await check("an old conversation response cannot overwrite the newly opened panel", async () => {
    const f = fixture(); await f.ctx.openSummary(); let resolve;
    f.hook = path => path === "conversations/conv/summary" ? new Promise(done => { resolve = done; }) : undefined;
    const pending = f.ctx.loadSummary(); f.state.current = "other"; await f.ctx.openSummary();
    resolve({current_revision_id: "stale", items: [], status: "idle"}); await pending;
    assert.equal(f.read("summaryConversation"), "other");
    assert.equal(f.read("summaryBase"), "r1");
  });
  await check("a summary conflict keeps the draft and source metadata", async () => {
    const f = fixture(); await f.ctx.openSummary();
    const editor = f.read("summaryEditors[0]"); editor.input.value = "本地修改"; await editor.input.emit("input");
    f.hook = (_path, options) => { if (options.method === "PUT") throw Object.assign(new Error("版本冲突"), {status: 409}); };
    await f.$("summary-form").emit("submit");
    assert.equal(editor.input.value, "本地修改");
    assert.equal(f.$("reload-summary").hidden, false);
    assert.equal(f.$("summary-error").textContent, "版本冲突");
  });
  await check("reopening the same summary invalidates an earlier save response", async () => {
    const f = fixture(); await f.ctx.openSummary(); let resolve;
    f.hook = (_path, options) => options.method === "PUT" ? new Promise(done => { resolve = done; }) : undefined;
    const pending = f.ctx.summaryMutation("PUT", "", {base_revision_id: "r1", items: f.summary.items});
    await f.$("summary-dialog").close(); await f.ctx.openSummary();
    const editor = f.read("summaryEditors[0]"); editor.input.value = "重新打开后的草稿"; await editor.input.emit("input");
    resolve({...f.summary, current_revision_id: "stale-save"}); await pending;
    assert.equal(f.read("summaryEditors[0].input.value"), "重新打开后的草稿");
    assert.equal(f.read("summaryBase"), "r1");
  });
  await check("summary rollback is guarded while a draft is being edited", async () => {
    const f = fixture(); await f.ctx.openSummary();
    await f.read("summaryEditors[0].input").emit("input");
    await buttons(f.$("summary-revisions").children[0])[0].emit("click");
    assert.ok(f.$("summary-error").textContent.includes("保存修改"));
    assert.equal(f.calls.filter(call => call.method === "POST").length, 0);
  });
  await check("growth polling retains drafts and confirmation sends reviewed version and token", async () => {
    const f = fixture(); f.candidates.items = [candidate()]; await f.ctx.openEvolution("c1");
    const entry = f.read("growthRows.get('g1')"); entry.input.value = "回答简洁，先说结论"; await entry.input.emit("input");
    await f.ctx.loadEvolution(); assert.equal(f.read("growthRows.get('g1').input"), entry.input);
    f.hook = (path, options) => { if (path.endsWith("/g1") && options.method === "POST") { f.candidates = {items: [], status: "idle"}; f.profile = {profile_revision_id: "p2"}; return f.candidates; } };
    await buttons(entry.row)[0].emit("click");
    assert.deepEqual(f.calls.find(call => call.path.endsWith("/g1")).body, {action: "approve", review_token: "token-1", base_revision_id: "p1", new_value: "回答简洁，先说结论"});
    assert.equal(f.read("growthRows.size"), 0);
  });
  await check("growth re-review updates token and preserves an edited proposal", async () => {
    const f = fixture(); f.candidates.items = [candidate()]; await f.ctx.openEvolution("c1");
    const entry = f.read("growthRows.get('g1')"); entry.input.value = "我的风格草稿"; await entry.input.emit("input");
    f.hook = (path, options, body) => {
      if (!path.endsWith("/g1") || options.method !== "POST") return;
      if (body.action === "approve") throw Object.assign(new Error("请重新审阅"), {status: 409});
      f.candidates = {items: [{...candidate(), base_revision_id: "p2", review_token: "token-2"}], status: "idle"}; return f.candidates;
    };
    await buttons(entry.row)[0].emit("click"); assert.equal(buttons(entry.row)[2].hidden, false);
    await buttons(entry.row)[2].emit("click");
    assert.equal(f.read("growthRows.get('g1').input.value"), "我的风格草稿");
    assert.equal(f.read("growthRows.get('g1').item.review_token"), "token-2");
    assert.equal(f.read("growthRows.get('g1').input.focused"), true);
  });
  await check("profile rollback posts the current baseline and target revision", async () => {
    const f = fixture(); await f.ctx.openEvolution("c1"); f.profile = {profile_revision_id: "p2"}; await f.ctx.loadEvolution();
    f.hook = path => path.endsWith("profile-rollback") ? f.profile : undefined;
    await buttons(f.$("profile-revisions").children[0])[0].emit("click");
    assert.deepEqual(f.calls.find(call => call.path.endsWith("profile-rollback")).body, {base_revision_id: "p2", target_revision_id: "p1"});
  });
  await check("close invalidates an in-flight growth request", async () => {
    const f = fixture(); let resolve;
    f.hook = path => path.endsWith("/profile-candidates") ? new Promise(done => { resolve = done; }) : undefined;
    const pending = f.ctx.openEvolution("c1"); await f.$("evolution-dialog").close();
    resolve({items: [candidate()], status: "idle"}); await pending;
    assert.equal(f.read("growthRows.size"), 0);
    assert.equal(f.read("evolutionCharacter"), null);
  });
  assert.ok(source.includes("body.base_revision_id = editingCharacterRevision"), "profile editing includes baseline");
  assert.ok(!section.includes("innerHTML"), "all derived content uses text nodes");
  console.log(`${checks} evolution UI checks passed`);
})().catch(error => { console.error(error); process.exitCode = 1; });
