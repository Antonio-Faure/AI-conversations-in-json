"use strict";

const PLATFORMS = ["chatgpt", "claude", "gemini", "perplexity", "grok", "mistral"];
const PLATFORM_LABEL = {
  chatgpt: "ChatGPT", claude: "Claude", gemini: "Gemini",
  perplexity: "Perplexity", grok: "Grok", mistral: "Mistral",
};

const state = {
  platforms: new Set(),
  groups: [],
  conversation: null,
  conversationUrl: null,
  matchedMid: null,
  terms: [],
  stats: null,
  view: "default",
};

const $ = (id) => document.getElementById(id);

/* ---------- theme ---------- */
(function initTheme() {
  const saved = localStorage.getItem("aicv-theme") || "dark";
  document.documentElement.setAttribute("data-theme", saved);
  window.addEventListener("DOMContentLoaded", () => {
    updateToggleUI(saved);
    $("themeToggle").addEventListener("click", () => {
      const current = document.documentElement.getAttribute("data-theme");
      const next = current === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("aicv-theme", next);
      updateToggleUI(next);
    });
  });
})();

function updateToggleUI(theme) {
  const track = $("toggleTrack"), label = $("themeLabel");
  if (!track) return;
  if (theme === "dark") { track.classList.add("active"); label.textContent = "Jour"; }
  else { track.classList.remove("active"); label.textContent = "Nuit"; }
}

/* ---------- helpers ---------- */
function escapeHtml(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function tokenize(text) {
  const out = [];
  for (const raw of String(text || "").toLowerCase().match(/[\w'’\-]+/gu) || []) {
    if (raw.length >= 2 && !out.includes(raw)) out.push(raw);
  }
  return out;
}

/* ---------- markdown + LaTeX ---------- */
const MATH_PATTERNS = [
  { re: /\$\$([\s\S]+?)\$\$/g, wrap: (m) => "$$" + m + "$$" },
  { re: /\\\[([\s\S]+?)\\\]/g, wrap: (m) => "\\[" + m + "\\]" },
  { re: /\\\(([\s\S]+?)\\\)/g, wrap: (m) => "\\(" + m + "\\)" },
  { re: /\$([^\s$][^$\n]*?[^\s$])\$/g, wrap: (m) => "$" + m + "$" },
];

function protectMath(text) {
  const store = [];
  let out = String(text || "");
  for (const { re, wrap } of MATH_PATTERNS) {
    out = out.replace(re, (_, inner) => {
      const token = "@@M" + store.length + "@@";
      store.push(wrap(inner));
      return token;
    });
  }
  return { text: out, store };
}

function restoreMath(html, store) {
  return html.replace(/@@M(\d+)@@/g, (_, i) => escapeHtml(store[Number(i)] || ""));
}

function stripFences(text) {
  return String(text || "").replace(/```[\s\S]*?```/g, "").replace(/\n{3,}/g, "\n\n").trim();
}

function renderMarkdown(text) {
  const { text: protectedText, store } = protectMath(text);
  let html;
  try {
    html = (typeof marked !== "undefined")
      ? marked.parse(protectedText, { gfm: true, breaks: true })
      : escapeHtml(protectedText).replace(/\n/g, "<br>");
  } catch (err) {
    html = escapeHtml(protectedText).replace(/\n/g, "<br>");
  }
  if (typeof DOMPurify !== "undefined") {
    html = DOMPurify.sanitize(html, { ADD_ATTR: ["target", "rel"] });
  }
  return restoreMath(html, store);
}

function highlightDom(root, terms) {
  if (!terms || !terms.length) return;
  const lowered = terms.map((t) => t.toLowerCase());
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  let node;
  while ((node = walker.nextNode())) nodes.push(node);
  for (const textNode of nodes) {
    if (!textNode.nodeValue.trim()) continue;
    const parent = textNode.parentElement;
    if (!parent || parent.closest("mark,.katex,pre,code,a,script,style")) continue;
    const value = textNode.nodeValue;
    const low = value.toLowerCase();
    let hit = -1, len = 0;
    for (let i = 0; i < lowered.length; i++) {
      const pos = low.indexOf(lowered[i]);
      if (pos !== -1 && (hit === -1 || pos < hit)) { hit = pos; len = lowered[i].length; }
    }
    if (hit === -1) continue;
    const before = value.slice(0, hit);
    const match = value.slice(hit, hit + len);
    const after = value.slice(hit + len);
    const mark = document.createElement("mark");
    mark.textContent = match;
    const frag = document.createDocumentFragment();
    if (before) frag.appendChild(document.createTextNode(before));
    frag.appendChild(mark);
    if (after) frag.appendChild(document.createTextNode(after));
    parent.replaceChild(frag, textNode);
  }
}

function hardenLinks(root) {
  root.querySelectorAll("a[href]").forEach((a) => {
    a.setAttribute("target", "_blank");
    a.setAttribute("rel", "noopener noreferrer");
  });
}

function enhance(root, terms) {
  if (typeof renderMathInElement === "function") {
    try {
      renderMathInElement(root, {
        delimiters: [
          { left: "$$", right: "$$", display: true },
          { left: "\\[", right: "\\]", display: true },
          { left: "\\(", right: "\\)", display: false },
          { left: "$", right: "$", display: false },
        ],
        throwOnError: false,
        ignoredTags: ["pre", "code", "script", "style"],
      });
    } catch (err) { /* KaTeX indisponible : on laisse le TeX brut */ }
  }
  highlightDom(root, terms);
  hardenLinks(root);
}

function platformName(p) { return PLATFORM_LABEL[p] || p || "?"; }
function roleLabel(r) { return r === "user" ? "Utilisateur" : (r === "assistant" ? "Assistant" : (r || "?")); }
function formatTs(ts) { return ts ? String(ts).replace("T", " ").replace("Z", "").slice(0, 16) : "—"; }
function scorePct(v) { return Math.round((v || 0) * 100); }

/* ---------- init ---------- */
window.addEventListener("DOMContentLoaded", () => {
  $("paramsToggle").addEventListener("click", () =>
    $("searchParams").classList.toggle("collapsed"));
  $("searchBtn").addEventListener("click", search);
  $("q").addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
  $("randomBtn").addEventListener("click", openRandomConversation);
  $("alpha").addEventListener("input", () => $("alphaVal").textContent = $("alpha").value);
  $("k").addEventListener("input", () => $("kVal").textContent = $("k").value);
  loadFilters();
  loadStats();
  loadDefaultConversations();
});

async function openRandomConversation() {
  try {
    const data = await (await fetch("/api/random")).json();
    if (data.error) throw new Error(data.error);
    openConversation(data.platform, data.conversation_id, null);
  } catch (err) {
    $("main").innerHTML = `<div class="no-results">Erreur: ${escapeHtml(err.message)}</div>`;
  }
}

async function loadDefaultConversations() {
  state.view = "default";
  state.groups = [];
  state.conversation = null;
  $("main").innerHTML = `<div class="loading"><div class="loading-spinner"></div><p>Chargement…</p></div>`;
  try {
    const data = await (await fetch("/api/conversations")).json();
    const items = (data.conversations || []).slice(0, 100);
    renderConversationList(items, "Dernières conversations");
    renderDefaultSidebar(items);
  } catch (err) { console.error(err); }
}

function renderConversationList(items, heading) {
  const main = $("main");
  if (!items.length) {
    main.innerHTML = `<div class="no-results">Aucune conversation.</div>`;
    return;
  }
  main.innerHTML = `<h2 class="section-title">${escapeHtml(heading)}</h2>` + `<div class="conv-list">` +
    items.map((c, i) => `
      <div class="conv-card" data-i="${i}">
        <div class="rc-head">
          <span class="badge platform">${escapeHtml(platformName(c.platform))}</span>
          <span class="badge small">${c.message_count ?? "?"} messages</span>
          ${c.has_code ? '<span class="badge small">code</span>' : ""}
        </div>
        <div class="conv-title">${escapeHtml(c.title || c.conversation_id)}</div>
        <div class="rc-meta">
          <span>${escapeHtml(formatTs(c.last_message_at))}</span>
          ${c.url ? `<a href="${escapeHtml(c.url)}" target="_blank" rel="noopener noreferrer" class="conv-link" data-stop="1">Ouvrir sur ${escapeHtml(platformName(c.platform))} ↗</a>` : ""}
        </div>
      </div>`).join("") + `</div>`;
  main.querySelectorAll(".conv-card").forEach((card) =>
    card.addEventListener("click", (e) => {
      if (e.target.closest("[data-stop]")) return;
      const c = items[Number(card.dataset.i)];
      openConversation(c.platform, c.conversation_id, null);
    }));
}

function renderDefaultSidebar(items) {
  const sidebar = $("sidebar");
  sidebar.innerHTML = `<h2>Conversations récentes</h2>` + items.slice(0, 40).map((c) => `
    <div class="side-item" data-platform="${escapeHtml(c.platform)}" data-id="${escapeHtml(c.conversation_id)}">
      <span>${escapeHtml((c.title || c.conversation_id).slice(0, 42))}</span>
      <span class="muted">${escapeHtml(platformName(c.platform))} · ${escapeHtml(formatTs(c.last_message_at))}</span>
    </div>`).join("");
  sidebar.querySelectorAll(".side-item[data-id]").forEach((item) =>
    item.addEventListener("click", () => openConversation(item.dataset.platform, item.dataset.id, null)));
}

async function loadFilters() {
  try {
    const data = await (await fetch("/api/filters")).json();
    const container = $("platformChips");
    container.innerHTML = "";
    for (const platform of data.platforms || []) {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.dataset.platform = platform;
      chip.innerHTML = `${escapeHtml(platformName(platform))} <span class="count"></span>`;
      chip.addEventListener("click", () => {
        if (state.platforms.has(platform)) state.platforms.delete(platform);
        else state.platforms.add(platform);
        chip.classList.toggle("active", state.platforms.has(platform));
        if (state.results.length) search();
      });
      container.appendChild(chip);
    }
    const select = $("modelFilter");
    for (const model of data.models || []) {
      const opt = document.createElement("option");
      opt.value = model; opt.textContent = model;
      select.appendChild(opt);
    }
    if (data.date_min) $("dateFrom").min = data.date_min.slice(0, 10);
    if (data.date_max) $("dateTo").max = data.date_max.slice(0, 10);
  } catch (err) { console.error(err); }
}

async function loadStats() {
  try {
    const data = await (await fetch("/api/stats")).json();
    state.stats = data;
    const stats = $("stats");
    const items = [
      ["Messages", data.total_messages],
      ["Conversations", Object.values(data.platforms || {}).reduce((a, p) => a + (p.conversations || 0), 0)],
      ["Chatbots", Object.keys(data.platforms || {}).length],
    ];
    stats.innerHTML = items.map(([label, value]) =>
      `<div><span class="stat-value">${value}</span><span class="stat-label">${label}</span></div>`
    ).join("");
    document.querySelectorAll("#platformChips .chip").forEach((chip) => {
      const info = (data.platforms || {})[chip.dataset.platform];
      const count = chip.querySelector(".count");
      if (info && count) count.textContent = info.messages;
    });
  } catch (err) { console.error(err); }
}

function buildQuery() {
  const params = new URLSearchParams();
  params.set("q", $("q").value.trim());
  params.set("k", $("k").value);
  params.set("alpha", $("alpha").value);
  if (state.platforms.size) params.set("platforms", [...state.platforms].join(","));
  if ($("modelFilter").value) params.set("model", $("modelFilter").value);
  if ($("dateFrom").value) params.set("date_from", $("dateFrom").value);
  if ($("dateTo").value) params.set("date_to", $("dateTo").value);
  return params;
}

async function search() {
  const query = $("q").value.trim();
  if (!query) return;
  state.terms = tokenize(query);
  state.conversation = null;
  state.view = "groups";
  $("main").innerHTML = `<div class="loading"><div class="loading-spinner"></div><p>Recherche…</p></div>`;
  try {
    const data = await (await fetch("/api/search?" + buildQuery().toString())).json();
    state.groups = data.groups || [];
    renderGroups();
    renderSidebar();
  } catch (err) {
    $("main").innerHTML = `<div class="no-results">Erreur: ${escapeHtml(err.message)}</div>`;
  }
}

function renderGroups() {
  const main = $("main");
  if (!state.groups.length) {
    main.innerHTML = `<div class="no-results">Aucun message trouvé.</div>`;
    return;
  }
  main.innerHTML = `<h2 class="section-title">${state.groups.length} conversation(s) — ${state.groups.reduce((a, g) => a + g.hit_count, 0)} message(s)</h2>` +
    `<div class="results-grid">` + state.groups.map((g, gi) => `
    <div class="group-card ${g.hit_count > 1 ? "multi" : "single"}" data-g="${gi}">
      <div class="rc-head">
        <span class="badge platform">${escapeHtml(platformName(g.platform))}</span>
        <span class="badge score">${g.hit_count > 1 ? g.hit_count + " extraits" : "1 extrait"}</span>
        <span class="badge small">meilleur score ${scorePct(g.best_score)}</span>
        ${g.url ? `<a class="conv-link" href="${escapeHtml(g.url)}" target="_blank" rel="noopener noreferrer" data-stop="1">Ouvrir ↗</a>` : ""}
      </div>
      <div class="conv-title" data-open="${gi}">${escapeHtml(g.title || g.conversation_id)}</div>
      <div class="group-hits">
        ${g.messages.map((r, mi) => `
          <div class="hit" data-open="${gi}" data-m="${mi}">
            <div class="rc-meta">
              <span class="badge role-${r.role === "user" ? "user" : "assistant"}">${escapeHtml(roleLabel(r.role))}</span>
              <span>${escapeHtml(formatTs(r.timestamp))}</span>
              <span>score ${scorePct(r.score)} · sém ${scorePct(r.semantic_score)} · mots ${scorePct(r.keyword_score)}</span>
            </div>
            <div class="rc-snippet"><div class="clip">${renderMarkdown(stripFences(r.texte))}</div></div>
          </div>`).join("")}
      </div>
    </div>`).join("") + `</div>`;
  main.querySelectorAll("[data-open]").forEach((el) =>
    el.addEventListener("click", (e) => {
      if (e.target.closest("[data-stop]")) return;
      const group = state.groups[Number(el.dataset.open)];
      const index = el.dataset.m !== undefined ? Number(el.dataset.m) : 0;
      const hit = group.messages[index] || group.messages[0];
      openConversation(group.platform, group.conversation_id, hit && hit.message_id);
    }));
  enhance(main, state.terms);
}

function renderSidebar() {
  const sidebar = $("sidebar");
  if (state.conversation) {
    const conv = state.conversation;
    sidebar.innerHTML = `<h2>${escapeHtml(conv.title || conv.conversation_id)}</h2>
      <div class="side-item" id="backToResults">← Retour</div>
      <div class="side-sep"></div>
      <h2>Messages</h2>` + (conv.messages || []).map((m, i) =>
        `<div class="anchor" data-mid="${escapeHtml(m.message_id || "")}" data-i="${i}">
          ${escapeHtml(roleLabel(m.role))} · ${escapeHtml((m.texte || "").replace(/\s+/g, " ").slice(0, 48))}
        </div>`).join("");
    $("backToResults").addEventListener("click", backToResults);
    sidebar.querySelectorAll(".anchor").forEach((a) =>
      a.addEventListener("click", () => scrollToMessage(a.dataset.mid, Number(a.dataset.i))));
    return;
  }
  sidebar.innerHTML = `<h2>Conversations (${state.groups.length})</h2>` + state.groups.map((g, gi) => `
    <div class="side-item" data-g="${gi}">
      <span>${escapeHtml((g.title || g.conversation_id).slice(0, 42))}</span>
      <span class="muted">${escapeHtml(platformName(g.platform))} · ${g.hit_count} message(s)</span>
    </div>`).join("");
  sidebar.querySelectorAll(".side-item[data-g]").forEach((item) =>
    item.addEventListener("click", () => {
      const group = state.groups[Number(item.dataset.g)];
      const hit = group.messages[0];
      openConversation(group.platform, group.conversation_id, hit && hit.message_id);
    }));
}

function backToResults() {
  state.conversation = null;
  if (state.view === "groups" && state.groups.length) {
    renderGroups();
    renderSidebar();
  } else {
    loadDefaultConversations();
  }
}

async function openConversation(platform, conversationId, matchedMid) {
  $("main").innerHTML = `<div class="loading"><div class="loading-spinner"></div><p>Chargement de la conversation…</p></div>`;
  try {
    const params = new URLSearchParams({ platform, id: conversationId });
    const data = await (await fetch("/api/conversation?" + params.toString())).json();
    if (data.error) throw new Error(data.error);
    state.conversation = data.conversation;
    state.conversationUrl = data.url || data.conversation.url || null;
    state.matchedMid = matchedMid || null;
    renderConversation();
    renderSidebar();
    setTimeout(() => {
      const target = document.querySelector(".msg.matched") ||
        document.querySelector(".msg");
      if (target) target.scrollIntoView({ block: "center" });
    }, 60);
  } catch (err) {
    $("main").innerHTML = `<div class="no-results">Erreur: ${escapeHtml(err.message)}</div>`;
  }
}

function renderConversation() {
  const conv = state.conversation;
  const messages = conv.messages || [];
  $("main").innerHTML = `<div class="conv-view">
    <div class="conv-head">
      <div>
        <h2>${escapeHtml(conv.title || conv.conversation_id)}</h2>
        <div class="rc-meta"><span>${escapeHtml(platformName(conv.platform))}</span>
          ${conv.model ? `<span>${escapeHtml(conv.model)}</span>` : ""}
          <span>${messages.length} messages</span>
          <span>${escapeHtml(formatTs(conv.started_at))} → ${escapeHtml(formatTs(conv.last_message_at))}</span>
          ${state.conversationUrl ? `<a class="conv-link" href="${escapeHtml(state.conversationUrl)}" target="_blank" rel="noopener noreferrer">Ouvrir sur ${escapeHtml(platformName(conv.platform))} ↗</a>` : ""}
        </div>
      </div>
      <button class="btn ghost" id="backTop">← Retour</button>
    </div>
    ${messages.map((m, i) => renderMessage(m, i)).join("")}
  </div>`;
  $("backTop").addEventListener("click", backToResults);
  enhance($("main"), state.terms);
}

function renderMessage(m, i) {
  const matched = state.matchedMid && m.message_id === state.matchedMid;
  const body = renderMarkdown(stripFences(m.texte || ""));
  const code = (m.code_blocks || []).map((b) =>
    `<div class="code-block"><div class="code-head"><span>${escapeHtml(b.language || "code")}</span>
      <button class="copy-btn" data-copy="${i}">copier</button></div>
      <pre><code>${escapeHtml(b.code || "")}</code></pre></div>`).join("");
  return `<div class="msg ${m.role === "user" ? "user" : "assistant"}${matched ? " matched" : ""}"
      data-mid="${escapeHtml(m.message_id || "")}" data-i="${i}" id="msg-${i}">
    <div class="msg-head">
      <span class="badge role-${m.role === "user" ? "user" : "assistant"}">${escapeHtml(roleLabel(m.role))}</span>
      <span>${escapeHtml(formatTs(m.timestamp))}</span>
      ${m.model ? `<span>${escapeHtml(m.model)}</span>` : ""}
    </div>
    <div class="msg-text">${body}</div>
    ${code}
  </div>`;
}

function scrollToMessage(mid, index) {
  let target = mid ? document.querySelector(`.msg[data-mid="${CSS.escape(mid)}"]`) : null;
  if (!target) target = document.getElementById("msg-" + index);
  if (target) target.scrollIntoView({ block: "center", behavior: "smooth" });
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".copy-btn");
  if (!btn) return;
  const i = Number(btn.dataset.copy);
  const block = state.conversation.messages[i].code_blocks;
  const text = (block && block[0] && block[0].code) || "";
  navigator.clipboard.writeText(text).then(() => {
    btn.textContent = "copié"; setTimeout(() => (btn.textContent = "copier"), 1200);
  });
});
