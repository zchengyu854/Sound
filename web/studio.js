"use strict";
// VOICE˚ Studio 交互层：书房抽屉（标签页滑块 / ⌘K / 搜索过滤）、记忆入口、
// 记忆分段滑块、涟漪反馈。只负责新外壳，不改 app.js 的数据逻辑。
(() => {
  const $ = id => document.getElementById(id);
  const drawer = $("library-dialog");
  if (!drawer) return;

  /* ---------- 标签页 ---------- */
  const tabs = [...drawer.querySelectorAll(".library-tabs button[data-tab]")];
  const panels = [...drawer.querySelectorAll(".library-body [data-panel]")];
  const glider = drawer.querySelector(".tab-glider");

  function openTab(name) {
    if (!tabs.some(tab => tab.dataset.tab === name)) name = "recall";
    for (const tab of tabs) tab.setAttribute("aria-selected", String(tab.dataset.tab === name));
    for (const panel of panels) panel.hidden = panel.dataset.panel !== name;
    requestAnimationFrame(placeGlider);
  }
  function placeGlider() {
    const active = drawer.querySelector('.library-tabs button[aria-selected="true"]');
    if (!active || !glider) return;
    glider.style.width = `${active.offsetWidth}px`;
    glider.style.left = `${active.offsetLeft}px`;
  }
  function openDrawer(name = "recall") {
    if (!drawer.open) drawer.showModal();
    openTab(name);
  }
  for (const tab of tabs) tab.addEventListener("click", () => openTab(tab.dataset.tab));
  drawer.querySelector(".library-tabs").addEventListener("keydown", event => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const current = tabs.findIndex(tab => tab.getAttribute("aria-selected") === "true");
    const next = (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    event.preventDefault();
    tabs[next].focus();
    openTab(tabs[next].dataset.tab);
  });
  addEventListener("resize", () => { placeGlider(); placePill(); });
  drawer.addEventListener("transitionend", placeGlider);

  /* ---------- 快捷键与入口 ---------- */
  document.addEventListener("keydown", event => {
    if (!(event.metaKey || event.ctrlKey) || event.shiftKey || event.key.toLowerCase() !== "k") return;
    if (document.querySelector("dialog[open]")) return;
    event.preventDefault();
    openDrawer("recall");
    setTimeout(() => $("recall-search")?.focus(), 60);
  });
  $("mobile-list")?.addEventListener("click", () => openDrawer("recall"));
  $("nav-settings-row")?.addEventListener("click", () => $("nav-settings")?.click());
  // 选中一条回忆后收起书房，回到对话舞台。
  $("conversations")?.addEventListener("click", event => {
    if (event.target.closest(".conversation")) drawer.close();
  });

  /* ---------- 记忆入口 ---------- */
  function openMemoriesTarget() {
    const conversation = typeof currentConversation === "function" ? currentConversation() : null;
    const characterId = conversation?.character_id || state.characters[0]?.id || null;
    if (!characterId) { toast("先创建人物，才有可管理的记忆。"); return; }
    openMemories(characterId);
  }
  $("memory-open")?.addEventListener("click", openMemoriesTarget);
  $("memory-open-drawer")?.addEventListener("click", openMemoriesTarget);

  /* ---------- 回忆搜索 ---------- */
  const search = $("recall-search"), conversations = $("conversations");
  function filterRecalls() {
    const query = search.value.trim().toLowerCase();
    let visible = 0;
    for (const row of conversations.querySelectorAll(".conversation")) {
      const hit = !query || row.textContent.toLowerCase().includes(query);
      row.hidden = !hit;
      if (hit) visible += 1;
    }
    $("recall-empty").hidden = !(query && !visible);
  }
  search.addEventListener("input", filterRecalls);
  new MutationObserver(filterRecalls).observe(conversations, {childList: true});
  drawer.addEventListener("close", () => {
    search.value = "";
    filterRecalls();
  });

  /* ---------- 记忆分段滑块 ---------- */
  const memorySwitch = drawer.ownerDocument.querySelector(".memory-switch");
  function ensureSegPill() {
    if (!memorySwitch || memorySwitch.querySelector(".seg-pill") || !memorySwitch.querySelector("button")) return;
    const pill = document.createElement("span");
    pill.className = "seg-pill";
    pill.setAttribute("aria-hidden", "true");
    memorySwitch.prepend(pill);
  }
  function placePill() {
    if (!memorySwitch) return;
    ensureSegPill();
    const pill = memorySwitch.querySelector(".seg-pill");
    const active = memorySwitch.querySelector('button[aria-pressed="true"]');
    if (!pill || !active) return;
    pill.style.left = `${active.offsetLeft}px`;
    pill.style.width = `${active.offsetWidth}px`;
  }
  memorySwitch?.addEventListener("click", event => {
    if (!event.target.closest("button")) return;
    requestAnimationFrame(placePill);
  });
  const memoryDialog = $("memory-dialog");
  if (memoryDialog) {
    new MutationObserver(() => { if (memoryDialog.open) requestAnimationFrame(placePill); })
      .observe(memoryDialog, {attributes: true, attributeFilter: ["open"]});
  }

  /* ---------- 会话更多菜单 ---------- */
  document.addEventListener("click", event => {
    const open = document.querySelector(".chat-more[open]");
    if (open && !open.contains(event.target)) open.removeAttribute("open");
  }, true);
  document.querySelector(".more-menu")?.addEventListener("click", event => {
    if (event.target.closest("button")) document.querySelector(".chat-more[open]")?.removeAttribute("open");
  });

  /* ---------- 涟漪反馈 ---------- */
  document.addEventListener("pointerdown", event => {
    const target = event.target.closest?.(".send, .drawer-new, .primary, .home-start");
    if (!target || target.disabled) return;
    const rect = target.getBoundingClientRect();
    const size = Math.max(rect.width, rect.height) * 2;
    const span = document.createElement("span");
    span.className = "ripple";
    span.style.width = span.style.height = `${size}px`;
    span.style.left = `${event.clientX - rect.left - size / 2}px`;
    span.style.top = `${event.clientY - rect.top - size / 2}px`;
    target.append(span);
    setTimeout(() => span.remove(), 600);
  }, {passive: true});

  /* ---------- 唱片首字同步 ---------- */
  const monogram = $("rail-monogram"), discInitial = $("disc-initial");
  if (monogram && discInitial) {
    const syncDisc = () => {
      const text = monogram.textContent.trim();
      if (text && text !== "◦") discInitial.textContent = text;
    };
    new MutationObserver(syncDisc).observe(monogram, {childList: true, characterData: true, subtree: true});
    syncDisc();
  }

  window.__studio = {openTab, openDrawer, placeGlider, placePill};
})();
