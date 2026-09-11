"use strict";

const PLATFORMS = ["chatgpt", "claude", "gemini", "perplexity", "grok", "mistral"];
const PLATFORM_LABEL = {
  chatgpt: "ChatGPT", claude: "Claude", gemini: "Gemini",
  perplexity: "Perplexity", grok: "Grok", mistral: "Mistral",
};

const state = {
  platforms: new Set(),
  results: [],
  conversation: null,
  matchedMid: null,
  terms: [],
  stats: null,
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
  $("alpha").addEventListener("input", () => $("alphaVal").textContent = $("alpha").value);
  $("k").addEventListener("input", () => $("kVal").textContent = $("k").value);
  loadFilters();
  loadStats();
});

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
  $("main").innerHTML = `<div class="loading"><div class="loading-spinner"></div><p>Recherche…</p></div>`;
  try {
    const data = await (await fetch("/api/search?" + buildQuery().toString())).json();
    state.results = data.results || [];
    renderResults();
    renderSidebar();
  } catch (err) {
    $("main").innerHTML = `<div class="no-results">Erreur: ${escapeHtml(err.message)}</div>`;
  }
}

function renderResults() {
  const main = $("main");
  if (!state.results.length) {
    main.innerHTML = `<div class="no-results">Aucun message trouvé.</div>`;
    return;
  }
  main.innerHTML = `<div class="results-grid">` + state.results.map((r, i) => `
    <div class="result-card" data-i="${i}">
      <div class="rc-head">
        <span class="badge platform">${escapeHtml(platformName(r.platform))}</span>
        <span class="badge role-${r.role === "user" ? "user" : "assistant"}">${escapeHtml(roleLabel(r.role))}</span>
        <span class="badge score">score ${scorePct(r.score)}</span>
        <span class="badge small">sém ${scorePct(r.semantic_score)} · mots ${scorePct(r.keyword_score)}</span>
      </div>
      <div class="rc-meta">
        <span>${escapeHtml(formatTs(r.timestamp))}</span>
        ${r.model ? `<span>${escapeHtml(r.model)}</span>` : ""}
        <span>conv ${escapeHtml((r.conversation_id || "").slice(0, 8))}</span>
      </div>
      <div class="rc-snippet"><div class="clip">${renderMarkdown(stripFences(r.texte))}</div></div>
    </div>`).join("") + `</div>`;
  main.querySelectorAll(".result-card").forEach((card) =>
    card.addEventListener("click", () => {
      const r = state.results[Number(card.dataset.i)];
      openConversation(r.platform, r.conversation_id, r.message_id);
    }));
  enhance(main, state.terms);
}

function renderSidebar() {
  const sidebar = $("sidebar");
  if (state.conversation) {
    const conv = state.conversation;
    sidebar.innerHTML = `<h2>${escapeHtml(conv.title || conv.conversation_id)}</h2>
      <div class="side-item" id="backToResults">← Résultats</div>
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
  const seen = new Map();
  for (const r of state.results) {
    const key = r.platform + "/" + r.conversation_id;
    if (!seen.has(key)) seen.set(key, { ...r, hits: 0 });
    seen.get(key).hits++;
  }
  sidebar.innerHTML = `<h2>Conversations (${seen.size})</h2>` + [...seen.values()].map((c) => `
    <div class="side-item" data-platform="${escapeHtml(c.platform)}" data-id="${escapeHtml(c.conversation_id)}" data-mid="${escapeHtml(c.message_id || "")}">
      <span>${escapeHtml((c.conversation_id || "").slice(0, 8))} · ${escapeHtml(platformName(c.platform))}</span>
      <span class="muted">${c.hits} message(s) trouvé(s)</span>
    </div>`).join("");
  sidebar.querySelectorAll(".side-item[data-id]").forEach((item) =>
    item.addEventListener("click", () =>
      openConversation(item.dataset.platform, item.dataset.id, item.dataset.mid)));
}

function backToResults() {
  state.conversation = null;
  renderResults();
  renderSidebar();
}

async function openConversation(platform, conversationId, matchedMid) {
  $("main").innerHTML = `<div class="loading"><div class="loading-spinner"></div><p>Chargement de la conversation…</p></div>`;
  try {
    const params = new URLSearchParams({ platform, id: conversationId });
    const data = await (await fetch("/api/conversation?" + params.toString())).json();
    if (data.error) throw new Error(data.error);
    state.conversation = data.conversation;
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
        </div>
      </div>
      <button class="btn ghost" id="backTop">← Résultats</button>
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
