"use strict";

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
  day: null,
  searchFiltered: false, // recherche par critères seuls (sans texte)
  view: "default",
};

const $ = (id) => document.getElementById(id);

/* ---------- theme ---------- */
(function initTheme() {
  // Chrome : palette bordeaux imposee, pas de bascule clair/nuit
  if (document.documentElement.hasAttribute("data-force-chrome")) return;
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
  const toggle = $("themeToggle"), label = $("themeLabel");
  if (!toggle) return;
  const dark = theme === "dark";
  toggle.setAttribute("aria-checked", dark ? "true" : "false");
  toggle.setAttribute("aria-label", dark ? "Thème sombre" : "Thème clair");
  if (label) label.textContent = dark ? "Nuit" : "Jour";
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

/* Markdown -> texte simple (sommaire lateral : pas de syntaxe brute). */
function mdPlain(text) {
  return String(text || "")
    .replace(/```[\w-]*\n?([\s\S]*?)```/g, "$1")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/~~([^~]+)~~/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/(^|\s)_([^_\n]+)_(?=\s|$)/g, "$1$2")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s{0,3}>\s?/gm, "")
    .replace(/^\s{0,3}[-*+]\s+/gm, "")
    .replace(/^\s{0,3}\d+[.)]\s+/gm, "")
    .replace(/<[^>]+>/g, "")
    .replace(/^\s{0,3}[|\s:-]*-{2,}[|\s:-]*$/gm, "")
    .replace(/\|/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function renderMarkdown(text) {
  // titres malformes par les modeles : "**#### Titre**" -> "#### Titre"
  // (sinon CommonMark rend le titre en gras avec les # litteraux)
  text = String(text || "").replace(
    /^(\s*)\*\*\s*(#{1,6}\s+[^\n]*?)\s*\*\*\s*$/gm, "$1$2"
  );
  const { text: protectedText, store } = protectMath(text);
  // fail-closed : sans DOMPurify (CDN bloque), jamais de HTML brut
  const canSanitize = typeof DOMPurify !== "undefined";
  let html = null;
  if (canSanitize && typeof marked !== "undefined") {
    try {
      html = DOMPurify.sanitize(
        marked.parse(protectedText, { gfm: true, breaks: true }),
        { ADD_ATTR: ["target", "rel"] }
      );
    } catch (err) {
      html = null;
    }
  }
  if (html == null) {
    html = escapeHtml(protectedText).replace(/\n/g, "<br>");
  }
  return restoreMath(html, store);
}

function resolveLocalImages(html, platform) {
  if (!html || !platform) return html;
  // images et pieces jointes archivees -> route /media (exports/<plateforme>/)
  const media = `"/media/${platform}/`;
  return html
    .replace(/(<img\b[^>]*?\bsrc=)["'](?:images|files)\//g, `$1${media}`)
    .replace(/(<a\b[^>]*?\bhref=)["'](?:images|files)\//g, `$1${media}`);
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

function renderError(message, retry) {
  const main = $("main");
  main.innerHTML = `<div class="state error">
    <p class="state-title">Une erreur est survenue</p>
    <p class="state-text">${escapeHtml(message)}</p>
    <button class="btn ghost" type="button" id="retryBtn">Réessayer</button>
  </div>`;
  const btn = $("retryBtn");
  if (btn && retry) btn.addEventListener("click", retry);
}

function renderEmpty(title, text) {
  $("main").innerHTML = `<div class="state">
    <p class="state-title">${escapeHtml(title)}</p>
    ${text ? `<p class="state-text">${escapeHtml(text)}</p>` : ""}
  </div>`;
}

/* Entree/Espace activent les elements cliquables (div/bouton non natifs). */
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const target = e.target;
  if (target.closest("input, textarea, select, [contenteditable='true']")) return;
  const el = target.closest("button, a, [data-open], .conv-card, .side-item, .anchor");
  if (!el || el.tagName === "BUTTON" || el.tagName === "A") return;
  e.preventDefault();
  el.click();
});

function platformName(p) { return PLATFORM_LABEL[p] || p || "?"; }
function roleLabel(r) { return r === "user" ? "Utilisateur" : (r === "assistant" ? "Assistant" : (r || "?")); }
function formatTs(ts) { return ts ? String(ts).replace("T", " ").replace("Z", "").slice(0, 16) : "—"; }
function scorePct(v) { return Math.round((v || 0) * 100); }

/* ---------- init ---------- */
window.addEventListener("DOMContentLoaded", () => {
  $("paramsToggle").addEventListener("click", () => {
    const collapsed = $("searchParams").classList.toggle("collapsed");
    $("paramsToggle").setAttribute("aria-expanded", collapsed ? "false" : "true");
  });
  $("searchBtn").addEventListener("click", search);
  $("q").addEventListener("keydown", (e) => { if (e.key === "Enter") search(); });
  $("randomBtn").addEventListener("click", () => { rollDice(); openRandomConversation(); });
  $("alpha").addEventListener("input", () => $("alphaVal").textContent = $("alpha").value);
  $("k").addEventListener("input", () => $("kVal").textContent = $("k").value);
  loadFilters();
  loadStats();
  loadHome();
});

/* -- de : lancer anime et face toujours differente de la precedente -- */
const DICE_FACES = [
  [[12, 12]],
  [[8.5, 8.5], [15.5, 15.5]],
  [[8.5, 8.5], [12, 12], [15.5, 15.5]],
  [[8.5, 8.5], [15.5, 8.5], [8.5, 15.5], [15.5, 15.5]],
  [[8.5, 8.5], [15.5, 8.5], [12, 12], [8.5, 15.5], [15.5, 15.5]],
  [[8.5, 8.5], [15.5, 8.5], [8.5, 12], [15.5, 12], [8.5, 15.5], [15.5, 15.5]],
];
let diceFace = 2; // face du HTML : 3 points en diagonale

function setDiceFace(index) {
  const svg = $("randomBtn").querySelector("svg");
  if (!svg) return;
  svg.querySelectorAll("circle").forEach((dot) => dot.remove());
  DICE_FACES[index].forEach(([cx, cy]) => {
    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    dot.setAttribute("cx", cx);
    dot.setAttribute("cy", cy);
    dot.setAttribute("r", "1.2");
    dot.setAttribute("fill", "currentColor");
    dot.setAttribute("stroke", "none");
    svg.appendChild(dot);
  });
  diceFace = index;
}

function rollDice() {
  const svg = $("randomBtn").querySelector("svg");
  if (!svg || svg.classList.contains("rolling")) return;
  svg.classList.add("rolling");
  const next = (diceFace + 1 + Math.floor(Math.random() * (DICE_FACES.length - 1))) % DICE_FACES.length;
  setTimeout(() => setDiceFace(next), 300);
  svg.addEventListener("animationend", () => svg.classList.remove("rolling"), { once: true });
}

async function openRandomConversation() {
  try {
    const data = await (await fetch("/api/random")).json();
    if (data.error) throw new Error(data.error);
    openConversation(data.platform, data.conversation_id, null);
  } catch (err) {
    renderError(err.message, openRandomConversation);
  }
}

/* ---------- accueil : reprise, tableau de bord, explorer, récentes ---------- */
const HOME_PAGE_SIZE = 24;   // cartes de « Dernières conversations » par page
const RANDOM_COUNT = 6;      // cartes « Au hasard » (6 : divisible par 1, 2 et 3)
const LAST_KEY = "aicv-last"; // dernière conversation consultée (localStorage)
const MONTHS_FR = ["janv.", "févr.", "mars", "avr.", "mai", "juin",
  "juil.", "août", "sept.", "oct.", "nov.", "déc."];

const home = {
  items: [],        // toutes les conversations (/api/conversations)
  filter: null,     // plateforme sélectionnée dans la sidebar (null = toutes)
  visible: HOME_PAGE_SIZE,
};

function sectionNote(container, message) {
  if (container) container.innerHTML = `<p class="section-note">${escapeHtml(message)}</p>`;
}

function setPanelBusy(container, busy) {
  const panel = container && container.closest(".panel");
  if (panel) panel.setAttribute("aria-busy", busy ? "true" : "false");
}

/* En accueil, le filtre Plateformes vit dans la section « Dernières
   conversations » : l'aside global est masque et la grille passe sur une
   colonne (tableau de bord + explorer en pleine largeur). */
function setHomeSidebar(hidden) {
  const aside = $("sidebar");
  if (aside) aside.hidden = hidden;
  const layout = document.querySelector(".layout");
  if (layout) layout.classList.toggle("solo", hidden);
}

function loadHome() {
  state.view = "default";
  state.groups = [];
  state.conversation = null;
  home.visible = HOME_PAGE_SIZE;
  renderHomeShell();
  loadHomeConversations();
  loadActivity();
  loadRandom(false);
  if (state.stats) renderDashboardStats();
}

/* -- barre « Reprendre » -- */
function lastVisited() {
  try {
    const raw = localStorage.getItem(LAST_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (err) { return null; }
}

function saveLastVisited(platform, conversationId, conv) {
  try {
    localStorage.setItem(LAST_KEY, JSON.stringify({
      platform,
      conversation_id: conversationId,
      title: (conv && conv.title) || conversationId,
    }));
  } catch (err) { /* stockage indisponible : sans conséquence */ }
}

function renderResumeBar() {
  const bar = $("resumeBar");
  if (!bar) return;
  const last = lastVisited();
  if (!last || !last.platform || !last.conversation_id) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  bar.innerHTML = `
    <span class="resume-label">Reprendre :</span>
    <span class="resume-title">« ${escapeHtml(last.title || last.conversation_id)} »</span>
    <button class="btn btn-sm" type="button" id="resumeBtn">Rouvrir</button>`;
  $("resumeBtn").addEventListener("click", () =>
    openConversation(last.platform, last.conversation_id, null));
}

/* -- structure de l'accueil -- */
function renderHomeShell() {
  $("main").innerHTML = `
    <div class="resume-bar" id="resumeBar" hidden></div>
    <section class="home-section" aria-labelledby="dashTitle">
      <h2 class="section-title" id="dashTitle">Tableau de bord</h2>
      <div class="dash-grid">
        <div class="panel heatmap-panel" aria-busy="true">
          <h3 class="panel-title">Activité — 365 derniers jours</h3>
          <div id="heatmap"><p class="section-note">Chargement…</p></div>
        </div>
        <div class="panel" aria-busy="true">
          <h3 class="panel-title">Conversations par plateforme</h3>
          <div id="platBars"><p class="section-note">Chargement…</p></div>
        </div>
        <div class="panel" aria-busy="true">
          <h3 class="panel-title">Top modèles</h3>
          <div id="modelBars"><p class="section-note">Chargement…</p></div>
        </div>
      </div>
    </section>
    <section class="home-section" aria-labelledby="exploreTitle">
      <h2 class="section-title" id="exploreTitle">Explorer</h2>
      <div class="panel">
        <div class="panel-head">
          <h3 class="panel-title">Au hasard</h3>
          <button class="btn ghost btn-sm" type="button" id="rerollBtn">Relancer</button>
        </div>
        <div class="explore-grid" id="randomCards"><p class="section-note">Chargement…</p></div>
      </div>
      <div class="panel" id="annivPanel" hidden>
        <h3 class="panel-title">Il y a un an aujourd'hui</h3>
        <div class="explore-grid" id="annivCards"></div>
      </div>
    </section>
    <section class="home-section" aria-labelledby="lastTitle">
      <h2 class="section-title" id="lastTitle">Dernières conversations</h2>
      <div class="last-layout">
        <aside class="sidebar last-filter" id="lastFilter" aria-label="Filtrer par plateforme"></aside>
        <div class="last-main">
          <div class="conv-grid" id="lastGrid"><p class="section-note">Chargement…</p></div>
          <div class="more-wrap" id="moreWrap" hidden>
            <button class="btn ghost" type="button" id="seeMoreBtn">Voir plus</button>
          </div>
        </div>
      </div>
    </section>`;
  setHomeSidebar(true);
  renderResumeBar();
  $("rerollBtn").addEventListener("click", () => loadRandom(true));
  $("seeMoreBtn").addEventListener("click", () => {
    home.visible += HOME_PAGE_SIZE;
    renderLastConversations();
  });
}

/* -- bouton scindé « Ouvrir / Copier » (URL de conversation) -- */
function splitOpenCopy(url, label) {
  if (!url) return "";
  // data-stop : les cartes ouvrent la conversation au clic, sauf sur ces elements
  return `<span class="conv-split"><a class="conv-link" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" data-stop="1">${escapeHtml(label)}</a><button class="conv-link conv-copy" type="button" data-stop="1" data-copy-text="${escapeHtml(url)}" title="Copier l'URL de la conversation">Copier</button></span>`;
}

/* -- carte de conversation (grilles : récentes, hasard, anniversaire) -- */
function homeCard(c) {
  return `<div class="conv-card" role="button" tabindex="0" data-platform="${escapeHtml(c.platform)}" data-id="${escapeHtml(c.conversation_id)}">
    <div class="rc-head">
      <span class="badge platform">${escapeHtml(platformName(c.platform))}</span>
      <span class="badge small">${c.message_count ?? "?"} messages</span>
      ${c.has_code ? '<span class="badge small">code</span>' : ""}
    </div>
    <div class="conv-title">${escapeHtml(c.title || c.conversation_id)}</div>
    <div class="rc-meta">
      <span>${escapeHtml(formatTs(c.last_message_at))}</span>
      ${splitOpenCopy(c.url, `Ouvrir sur ${platformName(c.platform)} ↗`)}
    </div>
  </div>`;
}

function bindOpenCards(root) {
  root.querySelectorAll(".conv-card[data-id]").forEach((card) =>
    card.addEventListener("click", (e) => {
      if (e.target.closest("[data-stop]")) return;
      openConversation(card.dataset.platform, card.dataset.id, null);
    }));
}

/* -- tableau de bord : heatmap, barres plateformes, top modèles -- */
async function loadActivity() {
  const box = $("heatmap");
  if (!box) return;
  try {
    const data = await (await fetch("/api/activity")).json();
    renderHeatmap(data);
  } catch (err) {
    sectionNote(box, "Impossible de charger l'activité.");
    setPanelBusy(box, false);
  }
}

function renderHeatmap(data) {
  const box = $("heatmap");
  if (!box) return;
  const days = (data && data.days) || {};
  const keys = Object.keys(days).sort();
  if (!keys.length) {
    sectionNote(box, "Pas encore d'activité.");
    setPanelBusy(box, false);
    return;
  }
  // seuils = quartiles des jours actifs : 5 niveaux bien repartis malgre
  // les pics (sinon, avec un maximum de plusieurs centaines, tout est niveau 1)
  const values = Object.values(days).filter((n) => n > 0).sort((a, b) => a - b);
  const quantile = (p) => values[Math.min(values.length - 1, Math.floor(p * values.length))];
  const thresholds = values.length ? [quantile(0.25), quantile(0.5), quantile(0.75)] : [];
  const toDate = (key) => {
    const [y, m, d] = key.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, d));
  };
  const dayKey = (date) => date.toISOString().slice(0, 10);
  const first = toDate(keys[0]);
  const last = toDate(keys[keys.length - 1]);
  const offset = (first.getUTCDay() + 6) % 7; // lundi = 0
  const start = new Date(first.getTime() - offset * 86400000);
  const totalDays = Math.round((last - start) / 86400000) + 1;
  const weeks = Math.ceil(totalDays / 7);
  let cells = "";
  const months = [];
  let previous = -1;
  for (let week = 0; week < weeks; week++) {
    // etiquette de mois quand la semaine change de mois (repere : jeudi)
    const middle = new Date(start.getTime() + (week * 7 + 3) * 86400000);
    const month = middle.getUTCMonth();
    months.push(month === previous ? "" : MONTHS_FR[month]);
    previous = month;
    for (let day = 0; day < 7; day++) {
      const date = new Date(start.getTime() + (week * 7 + day) * 86400000);
      if (date < first || date > last) {
        cells += '<span class="hm-cell hm-void" aria-hidden="true"></span>';
        continue;
      }
      const count = days[dayKey(date)] || 0;
      const level = count <= 0 ? 0
        : count <= thresholds[0] ? 1
          : count <= thresholds[1] ? 2
            : count <= thresholds[2] ? 3 : 4;
      const label = date.toLocaleDateString("fr-FR", {
        day: "numeric", month: "short", year: "numeric", timeZone: "UTC",
      });
      const text = count === 0 ? "aucun message" : `${count} message${count > 1 ? "s" : ""}`;
      if (count > 0) {
        // jour actif : case cliquable -> vue « journée »
        const aria = date.toLocaleDateString("fr-FR", {
          day: "numeric", month: "long", year: "numeric", timeZone: "UTC",
        });
        cells += `<button type="button" class="hm-cell hm-btn" data-date="${dayKey(date)}"
          data-level="${level}" title="${escapeHtml(label + " · " + text)}"
          aria-label="${escapeHtml(aria + ", " + text)}"></button>`;
      } else {
        cells += `<span class="hm-cell" data-level="0" title="${escapeHtml(label + " · aucun message")}" aria-hidden="true"></span>`;
      }
    }
  }
  const total = Object.values(days).reduce((sum, n) => sum + n, 0);
  box.innerHTML = `
    <div class="heatmap-scroll">
      <div class="heatmap">
        <div class="hm-months" style="--weeks:${weeks}" aria-hidden="true">${months.map((m) => `<span>${m}</span>`).join("")}</div>
        <div class="hm-main">
          <div class="hm-days" aria-hidden="true"><span>lun</span><span></span><span>mer</span><span></span><span>ven</span><span></span><span></span></div>
          <div class="hm-grid" role="group" aria-label="${escapeHtml(`Activité : ${total} messages sur les 365 derniers jours`)}">${cells}</div>
        </div>
      </div>
    </div>
    <div class="hm-legend" aria-hidden="true">
      <span>moins</span>
      ${[0, 1, 2, 3, 4].map((l) => `<span class="hm-cell" data-level="${l}"></span>`).join("")}
      <span>plus</span>
    </div>`;
  box.querySelectorAll(".hm-btn").forEach((btn) =>
    btn.addEventListener("click", () => openDay(btn.dataset.date)));
  setPanelBusy(box, false);
}

/* -- vue « journée » : messages d'une date (clic sur une case de la heatmap) -- */
function formatDayLong(date) {
  const [y, m, d] = String(date).split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d)).toLocaleDateString("fr-FR", {
    day: "numeric", month: "long", year: "numeric", timeZone: "UTC",
  });
}

function dayTime(ts) {
  const value = String(ts || "");
  return value.length >= 16 ? value.slice(11, 16) : "—";
}

async function openDay(date) {
  state.view = "day";
  state.groups = [];
  state.conversation = null;
  state.day = null;
  $("main").innerHTML = `<div class="loading"><div class="loading-spinner"></div><p>Chargement de la journée…</p></div>`;
  try {
    const data = await (await fetch(`/api/day?date=${encodeURIComponent(date)}`)).json();
    if (data.error) throw new Error(data.error);
    state.day = data;
    renderDay(date);
  } catch (err) {
    renderDay(date, err.message);
  }
  renderDaySidebar();
}

function renderDay(date, error) {
  const data = state.day;
  const counters = data
    ? `<div class="rc-meta">
        <span>${data.message_count} messages</span>
        <span>${data.conversation_count} conversation${data.conversation_count > 1 ? "s" : ""}</span>
      </div>`
    : "";
  let body;
  if (error) {
    body = `<p class="section-note">Impossible de charger cette journée (${escapeHtml(error)}).</p>`;
  } else if (!data.conversation_count) {
    body = `<div class="state">
      <p class="state-title">Aucun message ce jour-là</p>
      <p class="state-text">Aucun message indexé à cette date.</p>
    </div>`;
  } else {
    body = data.conversations.map((group, index) => dayCard(group, index)).join("");
  }
  $("main").innerHTML = `<div class="conv-view day-view">
    <div class="conv-head">
      <div>
        <h2>Activité du ${escapeHtml(formatDayLong(date))}</h2>
        ${counters}
      </div>
      <button class="btn ghost" type="button" id="backTop">← Retour</button>
    </div>
    ${body}
  </div>`;
  $("backTop").addEventListener("click", leaveDay);
  if (!data) return;
  $("main").querySelectorAll("[data-open]").forEach((el) =>
    el.addEventListener("click", (e) => {
      if (e.target.closest("[data-stop]")) return;
      const group = data.conversations[Number(el.dataset.open)];
      const index = el.dataset.m !== undefined ? Number(el.dataset.m) : 0;
      const message = group.messages[index] || group.messages[0];
      openConversation(group.platform, group.conversation_id, message && message.message_id);
    }));
}

/* Quitte la vue « journée » pour l'accueil. Le retour d'une conversation
   ouverte depuis la journée passe par backToResults (qui re-affiche la journée). */
function leaveDay() {
  state.day = null;
  loadHome();
}

/* Message_id du jour courant, pour la conversation ouverte depuis la journée.
   Garde : vue « journée » active ET conversation presente dans ce jour
   (sinon null -> aucun surlignage parasite). */
function dayMessageIds(platform, conversationId) {
  if (state.view !== "day" || !state.day) return null;
  const group = (state.day.conversations || []).find(
    (g) => g.platform === platform && g.conversation_id === conversationId
  );
  if (!group) return null;
  const ids = new Set();
  for (const message of group.messages || []) {
    if (message.message_id) ids.add(message.message_id);
  }
  return ids.size ? ids : null;
}

function dayCard(group, index) {
  return `<div class="group-card" id="day-card-${index}">
    <div class="rc-head">
      <span class="badge platform">${escapeHtml(platformName(group.platform))}</span>
      <span class="badge small">${group.message_count} message${group.message_count > 1 ? "s" : ""} ce jour-là</span>
      ${splitOpenCopy(group.url, `Ouvrir sur ${platformName(group.platform)} ↗`)}
    </div>
    <div class="conv-title group-title" data-open="${index}" role="button" tabindex="0">${escapeHtml(group.title || group.conversation_id)}</div>
    <div class="group-hits">
      ${group.messages.map((m, mi) => `
        <div class="hit" data-open="${index}" data-m="${mi}" role="button" tabindex="0">
          <div class="rc-meta">
            <span class="badge role-${m.role === "user" ? "user" : "assistant"}">${escapeHtml(roleLabel(m.role))}</span>
            <span>${escapeHtml(dayTime(m.timestamp))}</span>
            ${m.model ? `<span>${escapeHtml(m.model)}</span>` : ""}
          </div>
          <div class="rc-snippet"><div class="clip">${escapeHtml(m.texte)}</div></div>
        </div>`).join("")}
    </div>
  </div>`;
}

function renderDaySidebar() {
  const sidebar = $("sidebar");
  setHomeSidebar(false);
  const data = state.day;
  if (!data) {
    sidebar.innerHTML = `<h2>Journée</h2><p class="side-empty">Rien à afficher.</p>`;
    return;
  }
  sidebar.innerHTML = `<h2>Conversations (${data.conversation_count})</h2>` +
    data.conversations.map((g, gi) => `
      <div class="side-item" role="button" tabindex="0" data-g="${gi}">
        <span>${escapeHtml((g.title || g.conversation_id).slice(0, 42))}</span>
        <span class="muted">${escapeHtml(platformName(g.platform))} · ${g.message_count} message(s)</span>
      </div>`).join("");
  // la sidebar de la journee deplace vers la carte (pas d'ouverture de page)
  sidebar.querySelectorAll(".side-item[data-g]").forEach((item) =>
    item.addEventListener("click", () => {
      const card = document.getElementById(`day-card-${item.dataset.g}`);
      if (!card) return;
      const flash = () => {
        card.classList.remove("flash");
        void card.offsetWidth; // reflow : rejoue l'animation si deja posee
        card.classList.add("flash");
        setTimeout(() => card.classList.remove("flash"), 1100);
      };
      card.scrollIntoView({ behavior: "smooth", block: "start" });
      // flash a l'arrivee (scrollend), repli si l'evenement ne vient pas
      let flashed = false;
      const once = () => { if (!flashed) { flashed = true; flash(); } };
      document.addEventListener("scrollend", once, { once: true });
      setTimeout(once, 700);
    }));
}

function renderDashboardStats() {
  const data = state.stats;
  if (!data) return;
  const platBox = $("platBars");
  if (platBox) {
    const entries = Object.entries(data.platforms || {})
      .filter(([name]) => PLATFORM_LABEL[name])
      .sort((a, b) => (b[1].messages || 0) - (a[1].messages || 0));
    const max = Math.max(1, ...entries.map(([, info]) => info.messages || 0));
    platBox.innerHTML = entries.map(([name, info]) => {
      const messages = info.messages || 0;
      return `<div class="bar-row">
        <span class="bar-label">${escapeHtml(platformName(name))}</span>
        <span class="bar-track"><span class="bar-fill" style="--w:${Math.round((messages / max) * 100)}%"></span></span>
        <span class="bar-counts"><span>${info.conversations || 0} conv.</span><span class="bar-num">${messages} msg</span></span>
      </div>`;
    }).join("");
    setPanelBusy(platBox, false);
    requestAnimationFrame(() =>
      platBox.querySelectorAll(".bar-fill").forEach((el) => el.classList.add("grown")));
  }
  const modelBox = $("modelBars");
  if (modelBox) {
    const models = data.model_counts || [];
    if (!models.length) {
      sectionNote(modelBox, "Aucun modèle renseigné.");
    } else {
      const max = Math.max(1, ...models.map((m) => m.count || 0));
      modelBox.innerHTML = models.map((m) => `
        <div class="bar-row model">
          <span class="bar-label" title="${escapeHtml(m.model)}">${escapeHtml(m.model)}</span>
          <span class="bar-track"><span class="bar-fill" style="--w:${Math.round(((m.count || 0) / max) * 100)}%"></span></span>
          <span class="bar-counts"><span class="bar-num">${m.count || 0}</span></span>
        </div>`).join("");
      requestAnimationFrame(() =>
        modelBox.querySelectorAll(".bar-fill").forEach((el) => el.classList.add("grown")));
    }
    setPanelBusy(modelBox, false);
  }
}

/* -- explorer : au hasard + il y a un an aujourd'hui -- */
async function loadRandom(refresh) {
  const box = $("randomCards");
  if (!box) return;
  const button = $("rerollBtn");
  if (button) { button.disabled = true; button.textContent = refresh ? "…" : "Relancer"; }
  try {
    const data = await (await fetch(`/api/random?n=${RANDOM_COUNT}`)).json();
    const items = data.conversations || [];
    if (!items.length) {
      sectionNote(box, "Aucune conversation à afficher.");
      return;
    }
    box.innerHTML = items.map((c) => homeCard(c)).join("");
    bindOpenCards(box);
  } catch (err) {
    sectionNote(box, "Impossible de charger les suggestions.");
  } finally {
    if (button) { button.disabled = false; button.textContent = "Relancer"; }
  }
}

function renderAnniversary() {
  const panel = $("annivPanel");
  const box = $("annivCards");
  if (!panel || !box) return;
  const today = new Date();
  const mmdd = String(today.getMonth() + 1).padStart(2, "0") + "-" + String(today.getDate()).padStart(2, "0");
  const year = today.getFullYear();
  const items = home.items.filter((c) => {
    const ts = String(c.last_message_at || "");
    return ts.length >= 10 && ts.slice(5, 10) === mmdd && Number(ts.slice(0, 4)) < year;
  }).slice(0, 6); // plafond : meme grille 1/2/3 colonnes que « Au hasard »
  if (!items.length) { panel.hidden = true; return; }
  panel.hidden = false;
  box.innerHTML = items.map((c) => homeCard(c)).join("");
  bindOpenCards(box);
}

/* -- dernières conversations + sidebar plateformes -- */
async function loadHomeConversations() {
  try {
    const data = await (await fetch("/api/conversations")).json();
    home.items = data.conversations || [];
    renderHomeFilter();
    renderLastConversations();
    renderAnniversary();
  } catch (err) {
    sectionNote($("lastGrid"), "Impossible de charger les conversations.");
    renderHomeFilter();
  }
}

function renderLastConversations() {
  const grid = $("lastGrid");
  if (!grid) return;
  const title = $("lastTitle");
  if (title) title.textContent = "Dernières conversations" + (home.filter ? " — " + platformName(home.filter) : "");
  const items = home.filter ? home.items.filter((c) => c.platform === home.filter) : home.items;
  const shown = items.slice(0, home.visible);
  if (!shown.length) {
    sectionNote(grid, home.filter ? "Aucune conversation pour cette plateforme." : "Rien n'a encore été scrapé.");
  } else {
    grid.innerHTML = shown.map((c) => homeCard(c)).join("");
    bindOpenCards(grid);
  }
  const more = $("moreWrap");
  if (more) more.hidden = items.length <= home.visible;
}

function renderHomeFilter() {
  const sidebar = $("lastFilter");
  if (!sidebar) return;
  const stats = state.stats || {};
  const counted = {};
  for (const item of home.items) counted[item.platform] = (counted[item.platform] || 0) + 1;
  // compteur de la liste affichee en priorite (coherent avec le filtre),
  // repli sur l'index (stats) si la liste n'a pas pu etre chargee
  const countFor = (platform) => {
    if (counted[platform] !== undefined) return counted[platform];
    const info = (stats.platforms || {})[platform];
    return info ? info.conversations || 0 : 0;
  };
  const rows = Object.keys(PLATFORM_LABEL).map((platform) => {
    const active = home.filter === platform;
    const n = countFor(platform);
    return `<div class="side-item${active ? " active" : ""}" role="button" tabindex="0"
        data-platform="${escapeHtml(platform)}" aria-pressed="${active}">
      <span>${escapeHtml(platformName(platform))}</span>
      <span class="muted">${n} conversation${n > 1 ? "s" : ""}</span>
    </div>`;
  }).join("");
  const allActive = home.filter === null;
  sidebar.innerHTML = `<h2>Plateformes</h2>
    <div class="side-item${allActive ? " active" : ""}" role="button" tabindex="0" data-platform="" aria-pressed="${allActive}">
      <span>Toutes</span>
      <span class="muted">${home.items.length} conversation${home.items.length > 1 ? "s" : ""}</span>
    </div>` + rows;
  sidebar.querySelectorAll(".side-item[data-platform]").forEach((item) =>
    item.addEventListener("click", () => {
      home.filter = item.dataset.platform || null;
      home.visible = HOME_PAGE_SIZE;
      renderHomeFilter();
      renderLastConversations();
    }));
}

async function loadFilters() {
  try {
    const data = await (await fetch("/api/filters")).json();
    const container = $("platformChips");
    container.innerHTML = "";
    for (const platform of data.platforms || []) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip";
      chip.dataset.platform = platform;
      chip.setAttribute("aria-pressed", "false");
      chip.innerHTML = `${escapeHtml(platformName(platform))} <span class="count"></span>`;
      chip.addEventListener("click", () => {
        if (state.platforms.has(platform)) state.platforms.delete(platform);
        else state.platforms.add(platform);
        const on = state.platforms.has(platform);
        chip.classList.toggle("active", on);
        chip.setAttribute("aria-pressed", on ? "true" : "false");
        if (state.view === "groups" && state.groups.length) search();
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
    renderDashboardStats();
    if (state.view === "default" && !state.conversation && $("lastFilter")) renderHomeFilter();
  } catch (err) {
    console.error(err);
    sectionNote($("platBars"), "Impossible de charger les statistiques.");
    setPanelBusy($("platBars"), false);
    sectionNote($("modelBars"), "Impossible de charger les statistiques.");
    setPanelBusy($("modelBars"), false);
  }
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

function hasSearchFilters() {
  return state.platforms.size > 0 || !!$("modelFilter").value
    || !!$("dateFrom").value || !!$("dateTo").value;
}

async function search() {
  const query = $("q").value.trim();
  // sans texte : il faut au moins un critere actif (chatbots, modèle, dates)
  if (!query && !hasSearchFilters()) return;
  state.terms = query ? tokenize(query) : [];
  state.conversation = null;
  state.view = "groups";
  $("main").innerHTML = `<div class="loading"><div class="loading-spinner"></div><p>Recherche…</p></div>`;
  try {
    const data = await (await fetch("/api/search?" + buildQuery().toString())).json();
    state.groups = data.groups || [];
    state.searchFiltered = !!data.filtered;
    renderGroups();
    renderSidebar();
  } catch (err) {
    renderError(err.message, search);
  }
}

function renderGroups() {
  const main = $("main");
  if (!state.groups.length) {
    renderEmpty(
      "Aucun message trouvé",
      state.searchFiltered
        ? "Aucun message ne correspond à ces critères."
        : "Essaie d'autres mots-clés ou élargis les filtres."
    );
    return;
  }
  const hits = state.groups.reduce((a, g) => a + g.hit_count, 0);
  const suffix = state.searchFiltered ? " (filtre)" : "";
  main.innerHTML = `<h2 class="section-title">${state.groups.length} conversation${state.groups.length > 1 ? "s" : ""} · ${hits} message${hits > 1 ? "s" : ""}${suffix}</h2>` +
    `<div class="results-grid">` + state.groups.map((g, gi) => `
    <div class="group-card ${g.hit_count > 1 ? "multi" : "single"}" data-g="${gi}">
      <div class="rc-head">
        <span class="badge platform">${escapeHtml(platformName(g.platform))}</span>
        <span class="badge score">${g.hit_count > 1 ? g.hit_count + " extraits" : "1 extrait"}</span>
        ${g.best_score != null ? `<span class="badge small">meilleur score ${scorePct(g.best_score)}</span>` : ""}
        ${splitOpenCopy(g.url, "Ouvrir ↗")}
      </div>
      <div class="conv-title group-title" data-open="${gi}" role="button" tabindex="0">${escapeHtml(g.title || g.conversation_id)}</div>
      <div class="group-hits">
        ${g.messages.map((r, mi) => `
          <div class="hit" data-open="${gi}" data-m="${mi}" role="button" tabindex="0">
            <div class="rc-meta">
              <span class="badge role-${r.role === "user" ? "user" : "assistant"}">${escapeHtml(roleLabel(r.role))}</span>
              <span>${escapeHtml(formatTs(r.timestamp))}</span>
              ${r.score != null ? `<span>score ${scorePct(r.score)} · sém ${scorePct(r.semantic_score)} · mots ${scorePct(r.keyword_score)}</span>` : ""}
            </div>
            <div class="rc-snippet"><div class="clip">${resolveLocalImages(renderMarkdown(stripFences(r.texte)), r.platform)}</div></div>
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
  setHomeSidebar(false);
  if (state.conversation) {
    const conv = state.conversation;
    const dayIds = dayMessageIds(conv.platform, conv.conversation_id);
    sidebar.innerHTML = `<h2>${escapeHtml(conv.title || conv.conversation_id)}</h2>
      <div class="side-item" id="backToResults" role="button" tabindex="0">← Retour</div>
      <div class="side-sep"></div>
      <h2>Messages</h2>` + (conv.messages || []).map((m, i) =>
        `<div class="anchor${dayIds && m.message_id && dayIds.has(m.message_id) ? " day" : ""}" data-mid="${escapeHtml(m.message_id || "")}" data-i="${i}" role="button" tabindex="0">
          ${escapeHtml(roleLabel(m.role))} · ${escapeHtml(mdPlain(m.texte).slice(0, 48))}
        </div>`).join("");
    $("backToResults").addEventListener("click", backToResults);
    sidebar.querySelectorAll(".anchor").forEach((a) =>
      a.addEventListener("click", () => scrollToMessage(a.dataset.mid, Number(a.dataset.i))));
    return;
  }
  sidebar.innerHTML = `<h2>Conversations (${state.groups.length})</h2>` + state.groups.map((g, gi) => `
    <div class="side-item" role="button" tabindex="0" data-g="${gi}">
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
  state.matchedMid = null;
  if (state.view === "groups" && state.groups.length) {
    renderGroups();
    renderSidebar();
  } else if (state.view === "day" && state.day) {
    // conversation ouverte depuis une journée : on y revient sans re-fetch
    renderDay(state.day.date);
    renderDaySidebar();
  } else {
    loadHome();
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
    saveLastVisited(platform, conversationId, data.conversation);
    renderConversation();
    renderSidebar();
    setTimeout(() => {
      const target = document.querySelector(".msg.matched") ||
        document.querySelector(".msg");
      if (target) target.scrollIntoView({ block: "center" });
    }, 60);
  } catch (err) {
    renderError(err.message, () => openConversation(platform, conversationId, matchedMid));
  }
}

function renderConversation() {
  const conv = state.conversation;
  const messages = conv.messages || [];
  const dayIds = dayMessageIds(conv.platform, conv.conversation_id);
  $("main").innerHTML = `<div class="conv-view">
    <div class="conv-head">
      <div>
        <h2>${escapeHtml(conv.title || conv.conversation_id)}</h2>
        <div class="rc-meta"><span>${escapeHtml(platformName(conv.platform))}</span>
          ${conv.model ? `<span>${escapeHtml(conv.model)}</span>` : ""}
          <span>${messages.length} messages</span>
          <span>${escapeHtml(formatTs(conv.started_at))} → ${escapeHtml(formatTs(conv.last_message_at))}</span>
          ${splitOpenCopy(state.conversationUrl, `Ouvrir sur ${platformName(conv.platform)} ↗`)}
        </div>
      </div>
      <button class="btn ghost" id="backTop">← Retour</button>
    </div>
    ${messages.map((m, i) => renderMessage(m, i, dayIds)).join("")}
  </div>`;
  $("backTop").addEventListener("click", backToResults);
  enhance($("main"), state.terms);
}

function linkify(text) {
  const escaped = escapeHtml(text);
  return escaped.replace(
    /(https?:\/\/[^\s<]+)/g,
    '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>'
  );
}

function renderExtras(m, platform) {
  const blocks = [];
  const reasoning = String(m.reasoning || "").trim();
  if (reasoning) {
    const html = resolveLocalImages(renderMarkdown(reasoning), platform);
    blocks.push(
      `<details class="extra"><summary>Raisonnement</summary><div class="extra-body">${html}</div></details>`
    );
  }
  for (const [label, values] of [
    ["Artefacts", m.artifacts],
    ["Sources", m.sources],
    ["Outils", m.tools],
  ]) {
    const items = (values || []).filter(Boolean);
    if (!items.length) continue;
    blocks.push(
      `<details class="extra"><summary>${label} <span class="count">${items.length}</span></summary>` +
      `<ul class="extra-list">${items.map((v) => `<li>${linkify(v)}</li>`).join("")}</ul></details>`
    );
  }
  return blocks.join("");
}

function renderVariants(m, platform) {
  const variants = (m.variants || []).filter((v) => v && String(v.texte || "").trim());
  if (variants.length < 2) return "";
  const rows = variants.map((v, vi) => {
    const body = resolveLocalImages(renderMarkdown(stripFences(v.texte || "")), platform);
    const label = v.kind === "edit" ? "édition" : "régénération";
    return `<div class="variant${v.active ? " active" : ""}">
      <div class="variant-head">
        <span class="badge small">version ${vi + 1} · ${escapeHtml(label)}</span>
        ${v.active ? '<span class="badge small variant-active">active</span>' : ""}
        ${v.timestamp ? `<span class="muted">${escapeHtml(formatTs(v.timestamp))}</span>` : ""}
      </div>
      <div class="variant-body">${body}</div>
    </div>`;
  }).join("");
  return `<details class="extra variants">
    <summary>Versions alternatives <span class="count">${variants.length}</span></summary>
    <div class="variants-body">${rows}</div>
  </details>`;
}

function renderMessage(m, i, dayIds) {
  const matched = state.matchedMid && m.message_id === state.matchedMid;
  // messages du jour : rendu identique pour tous, message cliqué compris
  // (`.matched` reste pose pour le scroll d'ouverture, neutralise en CSS)
  const inDay = dayIds && m.message_id && dayIds.has(m.message_id);
  const platform = state.conversation && state.conversation.platform;
  const body = resolveLocalImages(renderMarkdown(stripFences(m.texte || "")), platform);
  const code = (m.code_blocks || []).map((b) =>
    `<div class="code-block"><div class="code-head"><span>${escapeHtml(b.language || "code")}</span>
      <button class="copy-btn" data-copy="${i}">copier</button></div>
      <pre><code>${escapeHtml(b.code || "")}</code></pre></div>`).join("");
  const variantCount = (m.variants || []).length;
  return `<div class="msg ${m.role === "user" ? "user" : "assistant"}${matched ? " matched" : ""}${inDay ? " day" : ""}"
      data-mid="${escapeHtml(m.message_id || "")}" data-i="${i}" id="msg-${i}">
    <div class="msg-head">
      <span class="badge role-${m.role === "user" ? "user" : "assistant"}">${escapeHtml(roleLabel(m.role))}</span>
      <span>${escapeHtml(formatTs(m.timestamp))}</span>
      ${m.model ? `<span>${escapeHtml(m.model)}</span>` : ""}
      ${variantCount > 1 ? `<span class="badge small">${variantCount} versions</span>` : ""}
    </div>
    <div class="msg-text">${body}</div>
    ${code}
    ${renderVariants(m, platform)}
    ${renderExtras(m, platform)}
  </div>`;
}

function scrollToMessage(mid, index) {
  let target = mid ? document.querySelector(`.msg[data-mid="${CSS.escape(mid)}"]`) : null;
  if (!target) target = document.getElementById("msg-" + index);
  if (target) target.scrollIntoView({ block: "center", behavior: "smooth" });
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".copy-btn, [data-copy-text]");
  if (!btn) return;
  let text = btn.dataset.copyText;
  if (text === undefined) {
    const i = Number(btn.dataset.copy);
    const block = state.conversation.messages[i].code_blocks;
    text = (block && block[0] && block[0].code) || "";
  }
  // libelle d'origine memorise : un 2e clic pendant le ✓ ne doit pas l'ecraser
  if (!btn.dataset.copyLabel) btn.dataset.copyLabel = btn.textContent;
  navigator.clipboard.writeText(text).then(() => {
    if (btn.dataset.copied) return; // animation deja en cours
    btn.dataset.copied = "1";
    btn.style.minWidth = `${btn.offsetWidth}px`; // stabilite de mise en page
    btn.innerHTML = '<span class="copy-check" aria-hidden="true">✓</span>';
    btn.classList.add("copied");
    btn.setAttribute("aria-label", "Copié");
    setTimeout(() => {
      btn.textContent = btn.dataset.copyLabel;
      btn.style.minWidth = "";
      btn.classList.remove("copied");
      btn.removeAttribute("aria-label");
      delete btn.dataset.copied;
    }, 1300);
  });
});
