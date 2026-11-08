"use strict";

/* Titre d'onglet : la vue courante, pour l'historique et les lecteurs d'ecran. */
/* « 1 message » / « 2 messages » (jamais « 1 messages »). */
function plural(n, word) {
  const value = Number(n) || 0;
  return `${value} ${word}${value > 1 ? "s" : ""}`;
}

function setTitle(label) {
  const clean = String(label || "").trim();
  document.title = clean ? `${clean} — Conversations IA` : "Recherche — Conversations IA";
}

/* ---------- session ---------- */
/* Toute reponse 401 (session expiree ou revoquee) ramene a la connexion. */
(function guardSession() {
  if (typeof window.fetch !== "function") return; // DOM minimal (tests)
  const nativeFetch = window.fetch.bind(window);
  window.fetch = function (...args) {
    return nativeFetch(...args).then((response) => {
      if (response.status === 401 && !window.location.pathname.startsWith("/login")) {
        const next = encodeURIComponent(window.location.pathname + window.location.search);
        window.location.replace(`/login?next=${next}`);
      }
      return response;
    });
  };
})();

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
  searchMedia: [],       // médias renvoyés par la recherche (même vecteur)
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

/* Titre affichable : les modeles mettent souvent du markdown dans le
   premier message ; on n'affiche jamais la syntaxe brute. */
const plainTitle = (text) => mdPlain(String(text || ""));

/* Sanitisation commune : le contenu des conversations est non fiable. On
   retire en plus les styles inline (une surcouche plein ecran peut tromper
   l'utilisateur) et les elements de formulaire (inertes, mais inutiles). */
const SANITIZE_OPTS = {
  ADD_ATTR: ["target", "rel"],
  FORBID_ATTR: ["style"],
  FORBID_TAGS: ["style", "form", "input", "button", "textarea", "select",
    "option", "dialog", "marquee"],
};

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
        SANITIZE_OPTS
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
  // fail-closed : sans DOMPurify on ne reecrit rien (le texte est deja echappe)
  if (typeof DOMPurify === "undefined") return html;
  const media = `/media/${encodeURIComponent(platform)}/`;
  const replaced = html
    .replace(/(<img\b[^>]*?\bsrc=)["'](?:images|files)\//g, `$1"${media}`)
    .replace(/(<a\b[^>]*?\bhref=)["'](?:images|files)\//g, `$1"${media}`);
  // la chaine est modifiee APRES la sanitisation initiale : on la revalide
  // (une plateforme hostile ne doit pas pouvoir injecter du HTML ici)
  return DOMPurify.sanitize(replaced, SANITIZE_OPTS);
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
  const el = target.closest("button, a, [data-open], [data-media-open], .conv-card, .side-item, .anchor");
  if (!el || el.tagName === "BUTTON" || el.tagName === "A") return;
  e.preventDefault();
  el.click();
});

function platformName(p) { return PLATFORM_LABEL[p] || p || "?"; }
function roleLabel(r) { return r === "user" ? "Utilisateur" : (r === "assistant" ? "Assistant" : (r || "?")); }
function formatTs(ts) { return ts ? String(ts).replace("T", " ").replace("Z", "").slice(0, 16) : "—"; }
function scorePct(v) { return Math.round((v || 0) * 100); }

/* ---------- état dans l'URL (permaliens de recherche) ---------- */
const URL_DEFAULTS = { k: "30", alpha: "0.6" };

function urlSearchParams() {
  const params = new URLSearchParams();
  const query = $("q").value.trim();
  if (query) params.set("q", query);
  if ($("k").value !== URL_DEFAULTS.k) params.set("k", $("k").value);
  if ($("alpha").value !== URL_DEFAULTS.alpha) params.set("alpha", $("alpha").value);
  if (state.platforms.size) params.set("platforms", [...state.platforms].sort().join(","));
  if ($("modelFilter").value) params.set("model", $("modelFilter").value);
  if ($("dateFrom").value) params.set("date_from", $("dateFrom").value);
  if ($("dateTo").value) params.set("date_to", $("dateTo").value);
  return params;
}

function syncUrl(push) {
  const query = urlSearchParams().toString();
  const url = location.pathname + (query ? "?" + query : "");
  if (url === location.pathname + location.search) return;
  const entry = { search: query };
  if (push) history.pushState(entry, "", url);
  else history.replaceState(entry, "", url);
}

function hasUrlSearch(params) {
  return ["q", "platforms", "model", "date_from", "date_to"]
    .some((key) => params.get(key));
}

/* Applique l'URL courante : relance la recherche (ou revient à l'accueil).
   Rend true quand une recherche a été relancée. */
function applyUrlState() {
  const params = new URLSearchParams(location.search);
  if (!hasUrlSearch(params)) {
    if (state.view === "groups" || state.view === "similar") {
      $("q").value = "";
      loadHome();
    }
    return false;
  }
  $("q").value = params.get("q") || "";
  $("k").value = params.get("k") || URL_DEFAULTS.k;
  $("alpha").value = params.get("alpha") || URL_DEFAULTS.alpha;
  $("alphaVal").textContent = $("alpha").value;
  $("kVal").textContent = $("k").value;
  state.platforms = new Set((params.get("platforms") || "").split(",").filter(Boolean));
  document.querySelectorAll("#platformChips .chip").forEach((chip) =>
    chip.classList.toggle("active", state.platforms.has(chip.dataset.platform)));
  $("modelFilter").value = params.get("model") || "";
  $("dateFrom").value = params.get("date_from") || "";
  $("dateTo").value = params.get("date_to") || "";
  search();
  return true;
}

/* ---------- bulles d'en-tête (raccourcis, index) ---------- */
function togglePop(id, force) {
  const pop = $(id);
  if (!pop) return;
  const open = force !== undefined ? force : pop.hidden;
  const ids = [["shortcutsPop", "shortcutsBtn"], ["infoPop", "infoBtn"]];
  for (const [otherId, otherBtn] of ids) {
    if (otherId === id) continue;
    const node = $(otherId);
    if (node) node.hidden = true;
    const button = $(otherBtn);
    if (button) button.setAttribute("aria-expanded", "false");
  }
  pop.hidden = !open;
  const button = $(ids.find(([popId]) => popId === id)[1]);
  if (button) button.setAttribute("aria-expanded", open ? "true" : "false");
  // l'index n'est chargé qu'à la première ouverture
  if (open && id === "infoPop" && !dash.status) loadStatus();
}

function closePops() {
  togglePop("shortcutsPop", false);
  togglePop("infoPop", false);
}

document.addEventListener("click", (e) => {
  if (e.target.closest(".header-pop, .header-actions")) return;
  closePops();
});

/* ---------- raccourcis clavier ---------- */
function isTyping(target) {
  if (!target || typeof target.closest !== "function") return false;
  return !!target.closest("input, textarea, select, [contenteditable='true']");
}

function moveResult(delta) {
  const cards = [...document.querySelectorAll("#main .group-card, #main .media-card")];
  if (!cards.length) return;
  const current = cards.findIndex((card) => card.classList.contains("current"));
  const next = current < 0
    ? (delta > 0 ? 0 : cards.length - 1)
    : Math.max(0, Math.min(cards.length - 1, current + delta));
  cards.forEach((card, i) => card.classList.toggle("current", i === next));
  cards[next].scrollIntoView({ block: "center", behavior: "smooth" });
}

document.addEventListener("keydown", (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "Escape") {
    if (!$("shortcutsPop").hidden || !$("infoPop").hidden) { closePops(); return; }
    if (isTyping(e.target)) { e.target.blur(); return; }
    if (!$("searchParams").classList.contains("collapsed")) {
      $("searchParams").classList.add("collapsed");
      $("paramsToggle").setAttribute("aria-expanded", "false");
      return;
    }
    if (state.conversation) backToResults();
    return;
  }
  if (isTyping(e.target)) return;
  if (e.key === "/") { e.preventDefault(); $("q").focus(); $("q").select(); return; }
  if (e.key === "?") { e.preventDefault(); togglePop("shortcutsPop", true); return; }
  if (e.key === "j") { moveResult(1); return; }
  if (e.key === "k") { moveResult(-1); return; }
  if (e.key === "r") { rollDice(); openRandomConversation(); return; }
  if (e.key === "h" && (location.search || state.view !== "default" || state.conversation)) {
    if (location.search) history.pushState({}, "", location.pathname);
    loadHome();
  }
});

/* ---------- init ---------- */
window.addEventListener("DOMContentLoaded", () => {
  $("paramsToggle").addEventListener("click", () => {
    const collapsed = $("searchParams").classList.toggle("collapsed");
    $("paramsToggle").setAttribute("aria-expanded", collapsed ? "false" : "true");
  });
  $("searchBtn").addEventListener("click", () => search({ push: true }));
  $("q").addEventListener("keydown", (e) => {
    if (e.key === "Enter") search({ push: true });
  });
  $("randomBtn").addEventListener("click", () => { rollDice(); openRandomConversation(); });
  $("shortcutsBtn").addEventListener("click", () => togglePop("shortcutsPop"));
  $("infoBtn").addEventListener("click", () => togglePop("infoPop"));
  const logoutBtn = $("logoutBtn");
  if (logoutBtn) {
    logoutBtn.addEventListener("click", () => {
      logoutBtn.disabled = true;
      fetch("/api/logout", { method: "POST" })
        .catch(() => {})
        .then(() => window.location.replace("/login"));
    });
  }
  $("alpha").addEventListener("input", () => $("alphaVal").textContent = $("alpha").value);
  $("k").addEventListener("input", () => $("kVal").textContent = $("k").value);
  // une URL partagee (?q=…&platforms=…) relance la recherche au chargement
  loadFilters().finally(() => {
    if (!applyUrlState()) loadHome();
  });
  loadStats();
  loadStatus();
  startJobWatch();
  const follow = $("jobFollow");
  if (follow) follow.addEventListener("click", () => openDailyPage());
  window.addEventListener("popstate", applyUrlState);
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
  setTitle("");
  state.view = "default";
  state.groups = [];
  state.terms = [];
  state.conversation = null;
  home.visible = HOME_PAGE_SIZE;
  renderHomeShell();
  loadHomeConversations();
  loadActivity();
  loadTimeline();
  loadStatus();
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
    <span class="resume-title">« ${escapeHtml(plainTitle(last.title || last.conversation_id))} »</span>
    <button class="btn btn-sm" type="button" id="resumeBtn">Rouvrir</button>`;
  $("resumeBtn").addEventListener("click", () =>
    openConversation(last.platform, last.conversation_id, null));
}

/* -- structure de l'accueil -- */
function renderHomeShell() {
  $("main").innerHTML = `
    <div class="resume-bar" id="resumeBar" hidden></div>
    <div class="alert-banner" id="parseAlert" hidden role="alert"></div>
    <section class="home-section" aria-labelledby="dashTitle">
      <h2 class="section-title" id="dashTitle">Tableau de bord</h2>
      <div class="dash-grid">
        <div class="panel rotator-panel heatmap-panel" aria-busy="true">
          <div class="panel-head">
            <h3 class="panel-title" id="rotHeatTitle">Activité — 365 derniers jours</h3>
            <div class="rot-nav" id="rotHeatNav" hidden>
              <button class="rot-arrow" type="button" id="rotHeatPrev" aria-label="Partie précédente">‹</button>
              <span class="rot-dots" id="rotHeatDots"></span>
              <button class="rot-arrow" type="button" id="rotHeatNext" aria-label="Partie suivante">›</button>
            </div>
          </div>
          <div class="rot-body heat-body" id="rotHeatBody"><p class="section-note">Chargement…</p></div>
        </div>
        <div class="panel rotator-panel" aria-busy="true">
          <div class="panel-head">
            <h3 class="panel-title" id="rotPlatTitle">Conversations par plateforme</h3>
            <div class="rot-nav" id="rotPlatNav" hidden>
              <button class="rot-arrow" type="button" id="rotPlatPrev" aria-label="Partie précédente">‹</button>
              <span class="rot-dots" id="rotPlatDots"></span>
              <button class="rot-arrow" type="button" id="rotPlatNext" aria-label="Partie suivante">›</button>
            </div>
          </div>
          <div class="rot-body" id="rotPlatBody"><p class="section-note">Chargement…</p></div>
        </div>
        <div class="panel rotator-panel" aria-busy="true">
          <div class="panel-head">
            <h3 class="panel-title" id="rotModelTitle">Top modèles</h3>
            <div class="rot-nav" id="rotModelNav" hidden>
              <button class="rot-arrow" type="button" id="rotModelPrev" aria-label="Partie précédente">‹</button>
              <span class="rot-dots" id="rotModelDots"></span>
              <button class="rot-arrow" type="button" id="rotModelNext" aria-label="Partie suivante">›</button>
            </div>
          </div>
          <div class="rot-body" id="rotModelBody"><p class="section-note">Chargement…</p></div>
        </div>
      </div>
    </section>
    <section class="home-section" aria-labelledby="exploreTitle">
      <h2 class="section-title" id="exploreTitle">Explorer</h2>
      <div class="panel">
        <div class="panel-head">
          <h3 class="panel-title">Au hasard</h3>
          <div class="panel-actions">
            <button class="btn ghost btn-sm" type="button" id="galleryBtn">Galerie des médias</button>
            <button class="btn ghost btn-sm" type="button" id="rerollBtn">Relancer</button>
          </div>
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
  $("galleryBtn").addEventListener("click", () => openGallery());
  $("rotHeatPrev").addEventListener("click", () => stepSlide(heatRotator, -1));
  $("rotHeatNext").addEventListener("click", () => stepSlide(heatRotator, 1));
  $("rotPlatPrev").addEventListener("click", () => stepSlide(platRotator, -1));
  $("rotPlatNext").addEventListener("click", () => stepSlide(platRotator, 1));
  $("rotModelPrev").addEventListener("click", () => stepSlide(modelRotator, -1));
  $("rotModelNext").addEventListener("click", () => stepSlide(modelRotator, 1));
  $("seeMoreBtn").addEventListener("click", () => {
    home.visible += HOME_PAGE_SIZE;
    renderLastConversations();
  });
  renderJobStatus();
}

/* -- bouton scindé « Ouvrir / Copier » (URL de conversation) -- */
function splitOpenCopy(url, label, copyLabel = "Copier") {
  if (!url) return "";
  // data-stop : les cartes ouvrent la conversation au clic, sauf sur ces elements
  return `<span class="conv-split"><a class="conv-link" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer" data-stop="1">${escapeHtml(label)}</a><button class="conv-link conv-copy" type="button" data-stop="1" data-copy-text="${escapeHtml(url)}" title="Copier l'URL de la conversation">${escapeHtml(copyLabel)}</button></span>`;
}

/* -- carte de conversation (grilles : récentes, hasard, anniversaire) -- */
function homeCard(c) {
  return `<div class="conv-card" role="button" tabindex="0" data-platform="${escapeHtml(c.platform)}" data-id="${escapeHtml(c.conversation_id)}">
    <div class="rc-head">
      <span class="badge platform">${escapeHtml(platformName(c.platform))}</span>
      <span class="badge small">${escapeHtml(c.message_count ?? "?")} messages</span>
      ${c.has_code ? '<span class="badge small">code</span>' : ""}
    </div>
    <div class="conv-title">${escapeHtml(plainTitle(c.title || c.conversation_id))}</div>
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

/* -- tableau de bord : activité (heatmap) et santé, en rotation -- */
async function loadActivity() {
  try {
    dash.activity = await (await fetch("/api/activity")).json();
  } catch (err) {
    dash.activity = { error: true };
  }
  renderRotator(heatRotator);
}

/* HTML de la heatmap (partie « Activité » du panneau rotatif). */
function heatmapHtml(data) {
  if (data && data.error) {
    return `<p class="section-note">Impossible de charger l'activité.</p>`;
  }
  const days = (data && data.days) || {};
  const keys = Object.keys(days).sort();
  if (!keys.length) {
    return `<p class="section-note">Pas encore d'activité.</p>`;
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
      const text = count === 0 ? "aucun message" : plural(count, "message");
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
  return `
    <div class="heat-head">
      <span class="status-meta">${total} messages sur 365 jours</span>
      <button class="btn ghost btn-sm" type="button" id="dailyBtn">Lancer un daily</button>
    </div>
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
}

/* les cases actives ouvrent la vue « journée » ; le bouton lance un daily */
function bindHeatmap(root) {
  // la fenetre montre les 12 derniers mois : on l'ouvre sur la fin (aujourd'hui)
  const scroller = root.querySelector(".heatmap-scroll");
  if (scroller && scroller.scrollWidth > scroller.clientWidth) {
    scroller.scrollLeft = scroller.scrollWidth;
  }
  root.querySelectorAll(".hm-btn").forEach((btn) =>
    btn.addEventListener("click", () => openDay(btn.dataset.date)));
  const button = root.querySelector("#dailyBtn");
  if (button) button.addEventListener("click", openDailyPage);
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
  // liens durcis et surlignage comme ailleurs ; pas de KaTeX ici : dans une vue
  // de survol, un prix en « $ » serait pris pour une formule
  highlightDom($("main"), state.terms);
  hardenLinks($("main"));
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
      <span class="badge small">${plural(group.message_count, "message")} ce jour-là</span>
      ${splitOpenCopy(group.url, `Ouvrir sur ${platformName(group.platform)} ↗`)}
    </div>
    <div class="conv-title group-title" data-open="${index}" role="button" tabindex="0">${escapeHtml(plainTitle(group.title || group.conversation_id))}</div>
    <div class="group-hits">
      ${group.messages.map((m, mi) => `
        <div class="hit" data-open="${index}" data-m="${mi}" role="button" tabindex="0">
          <div class="rc-meta">
            <span class="badge role-${m.role === "user" ? "user" : "assistant"}">${escapeHtml(roleLabel(m.role))}</span>
            <span>${escapeHtml(dayTime(m.timestamp))}</span>
            ${m.model ? `<span>${escapeHtml(m.model)}</span>` : ""}
          </div>
          <div class="rc-snippet"><div class="clip">${resolveLocalImages(renderMarkdown(stripFences(m.texte)), group.platform)}</div></div>
        </div>`).join("")}
    </div>
  </div>`;
}

function renderDaySidebar() {
  setTitle(state.day ? `Activité du ${formatDayLong(state.day.date)}` : "Journée");
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
        <span title="${escapeHtml(plainTitle(g.title || g.conversation_id))}">${escapeHtml(plainTitle(g.title || g.conversation_id))}</span>
        <span class="muted">${escapeHtml(platformName(g.platform))} · ${plural(g.message_count, "message")}</span>
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

/* -- tableau de bord : barres plateformes, top modèles, panneaux rotatifs -- */
function platformBarsHtml(data) {
  const entries = Object.entries(data.platforms || {})
    .filter(([name]) => PLATFORM_LABEL[name])
    .sort((a, b) => (b[1].messages || 0) - (a[1].messages || 0));
  const max = Math.max(1, ...entries.map(([, info]) => info.messages || 0));
  return entries.map(([name, info]) => {
    const messages = info.messages || 0;
    return `<div class="bar-row">
      <span class="bar-label">${escapeHtml(platformName(name))}</span>
      <span class="bar-track"><span class="bar-fill" style="--w:${Math.round((messages / max) * 100)}%"></span></span>
      <span class="bar-counts"><span>${info.conversations || 0} conv.</span><span class="bar-num">${messages} msg</span></span>
    </div>`;
  }).join("");
}

function modelBarsHtml(data) {
  const models = data.model_counts || [];
  if (!models.length) return "";
  const max = Math.max(1, ...models.map((m) => m.count || 0));
  return models.map((m) => `
    <div class="bar-row model">
      <span class="bar-label" title="${escapeHtml(m.model)}">${escapeHtml(m.model)}</span>
      <span class="bar-track"><span class="bar-fill" style="--w:${Math.round(((m.count || 0) / max) * 100)}%"></span></span>
      <span class="bar-counts"><span class="bar-num">${m.count || 0}</span></span>
    </div>`).join("");
}

/* -- panneaux rotatifs : une partie toutes les 5 minutes -------------------
   Chaque panneau tourne sur sa dimension : vue d'ensemble (barres), puis une
   partie par élément (conversations par mois). Flèches + pastilles pour
   naviguer ; la rotation patiente si l'onglet est masqué et s'arrête quand le
   panneau quitte la page. */
const ROTATE_MS = 5 * 60 * 1000;
const dash = {
  timeline: null,  // séries mensuelles (/api/timeline)
  activity: null,  // heatmap d'activité (/api/activity)
  status: null,    // index, scrape, santé, sauvegardes (/api/status)
};

function makeRotator(ids, build, empty) {
  return { ids, build, empty, slides: [], index: 0, timer: null };
}

const platRotator = makeRotator(
  { body: "rotPlatBody", title: "rotPlatTitle", nav: "rotPlatNav",
    dots: "rotPlatDots", prev: "rotPlatPrev", next: "rotPlatNext" },
  platformSlides, "Pas encore de statistiques.",
);
const modelRotator = makeRotator(
  { body: "rotModelBody", title: "rotModelTitle", nav: "rotModelNav",
    dots: "rotModelDots", prev: "rotModelPrev", next: "rotModelNext" },
  modelSlides, "Aucun modèle renseigné.",
);
// panneau « Activité » : la heatmap alterne avec la santé des scrapers
const heatRotator = makeRotator(
  { body: "rotHeatBody", title: "rotHeatTitle", nav: "rotHeatNav",
    dots: "rotHeatDots", prev: "rotHeatPrev", next: "rotHeatNext" },
  heatSlides, "Pas encore d'activité.",
);
const rotators = [heatRotator, platRotator, modelRotator];

function stopRotation(rot) {
  if (rot.timer) { clearInterval(rot.timer); rot.timer = null; }
}

function startRotation(rot) {
  stopRotation(rot);
  if (rot.slides.length < 2) return;
  rot.timer = setInterval(() => {
    if (!$(rot.ids.body)) { stopRotation(rot); return; } // panneau quitté
    if (document.hidden) return;                         // onglet masqué : on patiente
    stepSlide(rot, 1);
  }, ROTATE_MS);
}

function stepSlide(rot, delta) {
  const count = rot.slides.length;
  if (!count) return;
  rot.index = (rot.index + delta + count) % count;
  paintSlide(rot);
  startRotation(rot); // une partie reste affichée 5 min (y compris après un clic)
}

function paintSlide(rot) {
  const body = $(rot.ids.body);
  if (!body || !rot.slides.length) return;
  const slide = rot.slides[rot.index];
  const title = $(rot.ids.title);
  if (title) {
    title.textContent = slide.title;
    title.classList.toggle("mono", !!slide.mono);
  }
  body.innerHTML = slide.html;
  if (typeof slide.after === "function") slide.after(body);
  requestAnimationFrame(() =>
    body.querySelectorAll(".bar-fill").forEach((el) => el.classList.add("grown")));
  const dots = $(rot.ids.dots);
  if (dots) {
    [...dots.children].forEach((dot, i) => {
      dot.classList.toggle("active", i === rot.index);
      if (i === rot.index) dot.setAttribute("aria-current", "true");
      else dot.removeAttribute("aria-current");
    });
  }
  setPanelBusy(body, false);
}

/* -- parties : activité (heatmap) et santé des scrapers -- */
function heatSlides() {
  const slides = [{
    title: "Activité — 365 derniers jours",
    html: heatmapHtml(dash.activity),
    after: bindHeatmap,
  }];
  if (dash.status) {
    slides.push({
      title: "Santé des scrapers",
      html: healthHtml(dash.status),
      after: bindHealth,
    });
  }
  return slides;
}

/* Santé des scrapers : forme des payloads API, contrôle des parseurs,
   fraîcheur du scrape — et le bouton de test. */
function healthHtml(data) {
  const rows = (data.scrape || []).map((item) => {
    const health = item.health;
    const known = health && health.rate != null;
    const ok = known && health.rate >= (health.threshold || 95) && !health.breaks;
    const cls = !known ? "unknown" : ok ? "ok" : "warn";
    const title = known
      ? `forme des payloads API : ${health.matches}/${health.n} conformes, ${health.drifts} dérive(s), ${health.breaks} rupture(s)`
      : "aucun relevé de forme API";
    const report = item.recheck;
    const compared = report ? (report.compared ?? report.checked) : 0;
    const anomalies = report ? (report.issues || []).length : 0;
    const parseTitle = report
      ? `contrôle des parseurs : ${compared} comparée(s), rétention ${pctText(report.retention)}`
        + (anomalies ? `, ${anomalies} anomalie(s)` : "")
        + `, vérifié ${relativeAge(report.checked_at)}`
      : "aucun contrôle de parseur enregistré";
    const parseBadge = `<span class="status-badge parse ${report ? escapeHtml(report.verdict) : "unknown"}"
      title="${escapeHtml(parseTitle)}">${report ? escapeHtml(VERDICT_LABEL[report.verdict] || report.verdict) : "—"}</span>`;
    // « dernier message » n'apparait plus (largeur du panneau) : il reste
    // dans l'infobulle de la ligne, avec le detail des deux badges
    const rowTitle = `${platformName(item.platform)} · ${item.conversations} conversations`
      + ` · scrapé ${relativeAge(item.scraped_at)}`
      + ` · dernier message ${relativeAge(item.last_message_at)}`;
    return `<div class="status-row" title="${escapeHtml(rowTitle)}">
      <span class="status-platform">${escapeHtml(platformName(item.platform))}</span>
      <span class="status-badge ${cls}" title="${escapeHtml(title)}">${known ? `${health.rate} %` : "—"}</span>
      ${parseBadge}
      <span class="status-meta">${item.conversations} conv.</span>
      <span class="status-meta">scrapé ${escapeHtml(relativeAge(item.scraped_at))}</span>
    </div>`;
  }).join("");
  return `<div class="health-head">
      <span class="status-meta">Forme des payloads API · contrôle des parseurs · fraîcheur</span>
      <button class="btn ghost btn-sm" type="button" id="parseTestBtn">Lancer un test</button>
    </div>
    <div class="status-list">${rows}</div>
    <p class="section-note">Forme des payloads API (<code>api_health.json</code>) : vert = conforme,
    orange = dérive ou rupture. Contrôle des parseurs : re-scrape aléatoire comparé à l'archive
    (messages, rôles, images, tableaux, listes, code, LaTeX) — <code>scripts/recheck.py</code>.</p>`;
}

/* le bouton « Lancer un test » de la partie santé ouvre la page de test */
function bindHealth(root) {
  const button = root.querySelector("#parseTestBtn");
  if (button) button.addEventListener("click", openParseTest);
}

/* -- parties : plateformes (barres, toutes plateformes, puis une par plateforme) -- */
function platformSlides() {
  const slides = [];
  if (state.stats) {
    slides.push({ title: "Conversations par plateforme", html: platformBarsHtml(state.stats) });
  }
  const timeline = dash.timeline;
  if (timeline) {
    if (timeline.total) {
      slides.push({
        title: "Toutes plateformes — conversations par mois",
        html: monthChartHtml(timeline.total, timeline.months),
      });
    }
    (timeline.platforms || []).forEach((p) => slides.push({
      title: `${platformName(p.platform)} — conversations par mois`,
      html: monthChartHtml(p, timeline.months),
    }));
  }
  return slides;
}

/* -- parties : modèles (barres, puis une par modèle) -- */
function modelSlides() {
  const slides = [];
  if (state.stats) {
    const bars = modelBarsHtml(state.stats);
    if (bars) slides.push({ title: "Top modèles", html: bars });
  }
  const timeline = dash.timeline;
  if (timeline) {
    (timeline.models || []).forEach((m) => slides.push({
      title: `${m.model} — conversations par mois`,
      mono: true,
      html: monthChartHtml(m, timeline.months),
    }));
  }
  return slides;
}

function monthLabel(key) { // « 2026-01 » -> « janvier 2026 »
  const [y, m] = String(key || "").split("-").map(Number);
  if (!m) return "";
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString("fr-FR", {
    month: "long", year: "numeric", timeZone: "UTC",
  });
}

function monthAxisShort(key) { // « 2026-01 » -> « jan » (case de ~25 px)
  const m = Number(String(key || "").slice(5));
  return m ? MONTHS_FR[m - 1].replace(".", "").slice(0, 3) : "";
}

function monthAxisLabel(key) { // « 2026-01 » -> « janv. 26 »
  const [y, m] = String(key || "").split("-").map(Number);
  return m ? `${MONTHS_FR[m - 1]} ${String(y).slice(2)}` : "";
}

function monthChartHtml(entry, months) {
  const values = entry.series || [];
  const max = Math.max(1, ...values);
  const total = entry.total || 0;
  const peak = Math.max(0, values.indexOf(max));
  const trend = entry.trend;
  const trendHtml = trend
    ? `<span class="mc-trend ${trend.pct >= 0 ? "up" : "down"}"
        title="${escapeHtml(`Dernier mois complet (${trend.from} → ${trend.to})`)}"
        >${trend.pct >= 0 ? "↗" : "↘"} ${trend.pct > 0 ? "+" : ""}${trend.pct} %</span>`
    : "";
  const bars = values.map((value, i) => {
    const height = Math.round((value / max) * 100);
    const label = `${monthLabel(months[i])} · ${value} conversation${value > 1 ? "s" : ""}`;
    return `<span class="mc-bar${value ? "" : " zero"}" title="${escapeHtml(label)}">
      <span class="mc-fill" style="--h:${height}%"></span></span>`;
  }).join("");
  // repères : premier mois, chaque janvier, dernier mois
  const axis = months.map((month, i) => {
    const show = i === 0 || i === months.length - 1 || month.slice(5) === "01";
    return `<span>${show ? escapeHtml(monthAxisShort(month)) : ""}</span>`;
  }).join("");
  return `
    <div class="mc-head">
      <span class="mc-total">${total}<small>conversation${total > 1 ? "s" : ""}</small></span>
      <span class="mc-side">
        ${trendHtml}
        <span class="mc-peak">pic ${escapeHtml(monthAxisLabel(months[peak]))} · ${max}</span>
      </span>
    </div>
    <div class="mc-chart" role="img"
      aria-label="${escapeHtml(`${total} conversations ; maximum ${max} en ${monthLabel(months[peak])}`)}">${bars}</div>
    <div class="mc-axis" aria-hidden="true">${axis}</div>`;
}

function renderDots(rot) {
  const dots = $(rot.ids.dots);
  const nav = $(rot.ids.nav);
  if (!dots || !nav) return;
  nav.hidden = rot.slides.length < 2;
  dots.innerHTML = rot.slides.map((slide, i) =>
    `<button class="rot-dot" type="button" data-slide="${i}" aria-label="${escapeHtml(`Afficher : ${slide.title}`)}" title="${escapeHtml(slide.title)}"></button>`
  ).join("");
  dots.querySelectorAll(".rot-dot").forEach((dot) =>
    dot.addEventListener("click", () => {
      rot.index = Number(dot.dataset.slide);
      paintSlide(rot);
      startRotation(rot);
    }));
}

function renderRotator(rot) {
  const body = $(rot.ids.body);
  if (!body) return;
  rot.slides = rot.build();
  if (!rot.slides.length) {
    sectionNote(body, rot.empty);
    setPanelBusy(body, false);
    return;
  }
  rot.index = Math.min(rot.index, rot.slides.length - 1);
  renderDots(rot);
  paintSlide(rot);
  startRotation(rot);
}

function renderDashboardStats() {
  rotators.forEach(renderRotator);
}

/* -- séries mensuelles (conversations par plateforme et par modèle) -- */
async function loadTimeline() {
  try {
    dash.timeline = await (await fetch("/api/timeline")).json();
  } catch (err) {
    console.error(err);
  }
  renderDashboardStats();
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
    renderDashboardStats();
    rotators.forEach((rot) => {
      const box = $(rot.ids.body);
      if (box && !box.querySelector(".bar-row, .mc-chart")) {
        sectionNote(box, "Impossible de charger les statistiques.");
        setPanelBusy(box, false);
      }
    });
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

let searchSeq = 0;

async function search(options) {
  const query = $("q").value.trim();
  // sans texte : il faut au moins un critere actif (chatbots, modèle, dates)
  if (!query && !hasSearchFilters()) return;
  state.terms = query ? tokenize(query) : [];
  state.conversation = null;
  state.view = "groups";
  const seq = ++searchSeq;
  syncUrl(options && options.push);
  $("main").innerHTML = `<div class="loading"><div class="loading-spinner"></div><p>Recherche…</p></div>`;
  try {
    const response = await fetch("/api/search?" + buildQuery().toString());
    const data = await response.json();
    if (seq !== searchSeq) return; // une recherche plus recente est en cours
    if (!response.ok || data.error) {
      throw new Error(data.error || `recherche impossible (${response.status})`);
    }
    state.groups = data.groups || [];
    state.searchFiltered = !!data.filtered;
    state.searchMedia = data.media || [];
    renderGroups();
    renderSidebar();
    const query = $("q").value.trim();
    setTitle(query ? `Recherche « ${query} »` : "Recherche filtrée");
  } catch (err) {
    if (seq !== searchSeq) return;
    state.groups = [];
    state.searchMedia = [];
    renderSidebar(); // la sidebar ne doit pas garder des resultats perimes
    renderError(err.message, search);
  }
}

function renderGroups() {
  const main = $("main");
  const media = state.searchMedia || [];
  if (!state.groups.length && !media.length) {
    renderEmpty(
      "Aucun message trouvé",
      state.searchFiltered
        ? "Aucun message ne correspond à ces critères."
        : "Essaie d'autres mots-clés ou élargis les filtres."
    );
    return;
  }
  const hits = state.groups.reduce((a, g) => a + (g.messages || []).length, 0);
  const mediaTotal = state.groups.reduce((a, g) => a + (g.media || []).length, 0) + media.length;
  const suffix = state.searchFiltered ? " (filtre)" : "";
  const mediaSuffix = mediaTotal ? ` · ${plural(mediaTotal, "média")}` : "";
  // un média est un hit à part entière de sa conversation (rattaché par le
  // serveur, il compte dans le score et les extraits du groupe) ; seuls les
  // médias sans conversation restent des cartes autonomes en fin de flux
  const cards = [
    ...state.groups.map((g, gi) => groupCard(g, gi)),
    ...media.map((m) => mediaCard(m)),
  ];
  const similar = state.view === "similar" && state.similar;
  const header = similar
    ? `<div class="similar-head">
        <div>
          <h2 class="section-title">Messages similaires</h2>
          <p class="section-note">Voisins sémantiques de : « ${escapeHtml(mdPlain(similar.message.texte).slice(0, 120))} »</p>
        </div>
        <button class="btn ghost btn-sm" type="button" id="backToConv">← Retour à la conversation</button>
      </div>`
    : `<h2 class="section-title">${plural(state.groups.length, "conversation")} · ${plural(hits, "message")}${mediaSuffix}${suffix}</h2>`;
  main.innerHTML = header +
    `<div class="results-grid">` + cards.join("") + `</div>`;
  const backConv = $("backToConv");
  if (backConv) backConv.addEventListener("click", backToConversation);
  main.querySelectorAll("[data-open]").forEach((el) =>
    el.addEventListener("click", (e) => {
      if (e.target.closest("[data-stop]")) return;
      const group = state.groups[Number(el.dataset.open)];
      const index = el.dataset.m !== undefined ? Number(el.dataset.m) : 0;
      const hit = group.messages[index] || group.messages[0];
      openConversation(group.platform, group.conversation_id, hit && hit.message_id);
    }));
  // médias : clic (hors lecteur et hors liens) -> conversation d'origine
  main.querySelectorAll("[data-media-open]").forEach((card) =>
    card.addEventListener("click", (e) => {
      if (e.target.closest("audio, video, a")) return;
      openConversation(card.dataset.platform, card.dataset.id, card.dataset.mid || null);
    }));
  enhance(main, state.terms);
}

/* -- carte de conversation (résultats de recherche) -- */
function groupCard(g, gi) {
  const messages = g.messages || [];
  const media = g.media || [];
  const count = messages.length + media.length;
  return `<div class="group-card ${count > 1 ? "multi" : "single"}" data-g="${gi}">
    <div class="group-top">
      <div class="conv-title group-title" data-open="${gi}" role="button" tabindex="0">${escapeHtml(plainTitle(g.title || g.conversation_id))}</div>
      ${splitOpenCopy(g.url, "Ouvrir ↗")}
    </div>
    <div class="rc-head">
      <span class="badge platform">${escapeHtml(platformName(g.platform))}</span>
      ${messages.length ? `<span class="badge score">${messages.length > 1 ? messages.length + " extraits" : "1 extrait"}</span>` : ""}
      ${media.length ? `<span class="badge score">${plural(media.length, "média")}</span>` : ""}
      ${g.best_score != null ? `<span class="badge small">meilleur score ${scorePct(g.best_score)}</span>` : ""}
    </div>
    <div class="group-hits">
      ${messages.map((r, mi) => `
        <div class="hit" data-open="${gi}" data-m="${mi}" role="button" tabindex="0">
          <div class="rc-meta">
            <span class="badge role-${r.role === "user" ? "user" : "assistant"}">${escapeHtml(roleLabel(r.role))}</span>
            <span>${escapeHtml(formatTs(r.timestamp))}</span>
            ${r.score != null ? `<span>score ${scorePct(r.score)} · sém ${scorePct(r.semantic_score)} · mots ${scorePct(r.keyword_score)}</span>` : ""}
          </div>
          <div class="rc-snippet"><div class="clip">${resolveLocalImages(renderMarkdown(stripFences(r.texte)), r.platform || g.platform)}</div></div>
        </div>`).join("")}
      ${media.map((m) => mediaHit(m)).join("")}
    </div>
  </div>`;
}

/* -- média rattaché à une conversation (hit des résultats) -- */
function mediaHit(m) {
  const filename = String(m.path || "").split("/").pop();
  const url = m.url || (m.platform && filename
    ? `/media/${encodeURIComponent(m.platform)}/${encodeURIComponent(filename)}` : "");
  let body = "";
  if (m.kind === "audio") {
    body = `<audio controls preload="none" src="${escapeHtml(url)}"></audio>`;
  } else if (m.kind === "video") {
    body = `<video controls preload="metadata" src="${escapeHtml(url)}"></video>`;
  } else if (url) {
    body = `<img src="${escapeHtml(url)}" alt="${escapeHtml(m.texte || "")}" loading="lazy">`;
  }
  return `<div class="hit media-hit ${escapeHtml(m.kind || "file")}" data-media-open="1" role="button" tabindex="0"
      data-platform="${escapeHtml(m.platform)}" data-id="${escapeHtml(m.conversation_id || "")}" data-mid="${escapeHtml(m.message_id || "")}">
    <div class="rc-meta">
      <span class="badge small">${escapeHtml(m.kind || "média")}</span>
      ${m.score != null ? `<span>score ${scorePct(m.score)}</span>` : ""}
    </div>
    <div class="media-body">${body}</div>
  </div>`;
}

/* -- carte média (résultats de recherche) -- */
function mediaCard(m) {
  const filename = String(m.path || "").split("/").pop();
  const url = m.url || (m.platform && filename
    ? `/media/${encodeURIComponent(m.platform)}/${encodeURIComponent(filename)}` : "");
  const label = m.title || m.texte || m.conversation_id || filename || "média";
  const badge = `<span class="badge small">${escapeHtml(m.kind || "média")}</span>`;
  const platform = m.platform
    ? `<span class="badge platform">${escapeHtml(platformName(m.platform))}</span>` : "";
  const score = m.score != null ? `<span class="badge small">${scorePct(m.score)}</span>` : "";
  let body = "";
  if (m.kind === "audio") {
    body = `<audio controls preload="none" src="${escapeHtml(url)}"></audio>`;
  } else if (m.kind === "video") {
    body = `<video controls preload="metadata" src="${escapeHtml(url)}"></video>`;
  } else if (url) {
    body = `<img src="${escapeHtml(url)}" alt="${escapeHtml(m.texte || "")}" loading="lazy">`;
  }
  const openable = !!m.conversation_id;
  const attrs = openable
    ? ` data-media-open="1" role="button" tabindex="0" data-platform="${escapeHtml(m.platform)}" data-id="${escapeHtml(m.conversation_id)}" data-mid="${escapeHtml(m.message_id || "")}"`
    : "";
  const orphan = !openable && url
    ? `<a class="conv-link" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">Ouvrir le fichier ↗</a>` : "";
  return `<figure class="media-card ${escapeHtml(m.kind || "file")}"${attrs}>
    <div class="media-body">${body}</div>
    <figcaption>
      <div class="rc-head">${badge}${platform}${score}</div>
      <div class="media-caption">${escapeHtml(label)}</div>
      ${orphan}
    </figcaption>
  </figure>`;
}

function renderSidebar() {
  const sidebar = $("sidebar");
  setHomeSidebar(false);
  if (state.conversation) {
    const conv = state.conversation;
    const dayIds = dayMessageIds(conv.platform, conv.conversation_id);
    sidebar.innerHTML = `<h2>${escapeHtml(plainTitle(conv.title || conv.conversation_id))}</h2>
      <div class="side-item" id="backToResults" role="button" tabindex="0">← Retour</div>
      <div class="side-sep"></div>
      <input type="search" id="convSearch" class="conv-search" autocomplete="off"
        placeholder="Chercher dans la conversation…" aria-label="Chercher dans la conversation">
      <div class="side-count" id="convCount" hidden></div>
      <h2>Sommaire</h2>` + (conv.messages || []).map((m, i) =>
        `<div class="anchor${dayIds && m.message_id && dayIds.has(m.message_id) ? " day" : ""}" data-mid="${escapeHtml(m.message_id || "")}" data-i="${i}" role="button" tabindex="0">
          ${escapeHtml(roleLabel(m.role))} · ${escapeHtml(mdPlain(m.texte).slice(0, 48))}
        </div>`).join("");
    $("backToResults").addEventListener("click", backToResults);
    $("convSearch").addEventListener("input", (e) => filterConversation(e.target.value));
    $("convSearch").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); jumpToFirstMatch(); }
    });
    sidebar.querySelectorAll(".anchor").forEach((a) =>
      a.addEventListener("click", () => scrollToMessage(a.dataset.mid, Number(a.dataset.i))));
    return;
  }
  sidebar.innerHTML = `<h2>Conversations (${state.groups.length})</h2>` + state.groups.map((g, gi) => `
    <div class="side-item" role="button" tabindex="0" data-g="${gi}">
      <span title="${escapeHtml(plainTitle(g.title || g.conversation_id))}">${escapeHtml(plainTitle(g.title || g.conversation_id))}</span>
      <span class="muted">${escapeHtml(platformName(g.platform))} · ${plural(g.hit_count, "message")}</span>
    </div>`).join("");
  sidebar.querySelectorAll(".side-item[data-g]").forEach((item) =>
    item.addEventListener("click", () => {
      const group = state.groups[Number(item.dataset.g)];
      const hit = group.messages[0];
      openConversation(group.platform, group.conversation_id, hit && hit.message_id);
    }));
}

function backToResults() {
  if (state.view === "similar" && state.similar) { backToConversation(); return; }
  const restoreScroll = state.returnScroll;
  state.conversation = null;
  state.matchedMid = null;
  state.returnScroll = null;
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
  if (restoreScroll != null) window.scrollTo(0, restoreScroll);
  focusMain();
}

let conversationSeq = 0;

async function openConversation(platform, conversationId, matchedMid) {
  const seq = ++conversationSeq;
  state.returnScroll = window.scrollY;
  $("main").innerHTML = `<div class="loading"><div class="loading-spinner"></div><p>Chargement de la conversation…</p></div>`;
  try {
    const params = new URLSearchParams({ platform, id: conversationId });
    const data = await (await fetch("/api/conversation?" + params.toString())).json();
    if (seq !== conversationSeq) return; // une autre conversation a ete demandee
    if (data.error) throw new Error(data.error);
    state.conversation = data.conversation;
    state.conversationUrl = data.url || data.conversation.url || null;
    state.matchedMid = matchedMid || null;
    saveLastVisited(platform, conversationId, data.conversation);
    renderConversation();
    renderSidebar();
    setTitle(plainTitle(data.conversation.title));
    setTimeout(() => {
      const target = document.querySelector(".msg.matched") ||
        document.querySelector(".msg");
      if (target) target.scrollIntoView({ block: "center" });
      focusMain();
    }, 60);
  } catch (err) {
    if (seq !== conversationSeq) return;
    renderError(err.message, () => openConversation(platform, conversationId, matchedMid));
  }
}

/* Le rendu remplace #main : sans cela le focus retombe sur <body>. */
function focusMain() {
  const main = $("main");
  if (main) main.focus({ preventScroll: true });
}

function renderConversation() {
  const conv = state.conversation;
  const messages = conv.messages || [];
  const dayIds = dayMessageIds(conv.platform, conv.conversation_id);
  $("main").innerHTML = `<div class="conv-view">
    <div class="conv-head">
      <div>
        <h2>${escapeHtml(plainTitle(conv.title || conv.conversation_id))}</h2>
        <div class="rc-meta"><span>${escapeHtml(platformName(conv.platform))}</span>
          ${conv.model ? `<span>${escapeHtml(conv.model)}</span>` : ""}
          <span>${messages.length} messages</span>
          <span>${escapeHtml(formatTs(conv.started_at))} → ${escapeHtml(formatTs(conv.last_message_at))}</span>
          ${splitOpenCopy(state.conversationUrl, `Ouvrir sur ${platformName(conv.platform)} ↗`, "Copier l'URL")}
        </div>
      </div>
      <div class="conv-actions">
        <button class="btn ghost btn-sm" type="button" id="copyConv"
          title="Copier la conversation en markdown">Copier</button>
        <button class="btn ghost btn-sm" type="button" id="downloadConv"
          title="Télécharger la conversation en markdown (.md)">Télécharger .md</button>
        <button class="btn ghost" id="backTop">← Retour</button>
      </div>
    </div>
    ${messages.map((m, i) => renderMessage(m, i, dayIds)).join("")}
  </div>`;
  $("backTop").addEventListener("click", backToResults);
  $("copyConv").addEventListener("click", (e) => copyConversation(e.currentTarget));
  $("downloadConv").addEventListener("click", downloadConversation);
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

function renderVariants(m, platform, index) {
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
    <div class="variants-body">
      <div class="variants-bar">
        <button class="btn ghost btn-sm" type="button" data-compare="${index}">Comparer côte à côte</button>
      </div>
      ${rows}
    </div>
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
      <span class="msg-actions">
        <button class="msg-action" type="button" data-copy-msg="${i}" title="Copier le markdown du message">copier</button>
        <button class="msg-action" type="button" data-similar="${i}" title="Messages similaires (voisins sémantiques)">similaires</button>
      </span>
    </div>
    <div class="msg-text">${body}</div>
    ${code}
    ${renderVariants(m, platform, i)}
    ${renderExtras(m, platform)}
  </div>`;
}

function scrollToMessage(mid, index) {
  let target = mid ? document.querySelector(`.msg[data-mid="${CSS.escape(mid)}"]`) : null;
  if (!target) target = document.getElementById("msg-" + index);
  if (target) target.scrollIntoView({ block: "center", behavior: "smooth" });
}

/* ---------- voisins sémantiques d'un message ---------- */
const SIMILAR_K = 12;

async function showSimilar(conv, message, index) {
  state.similar = { conversation: conv, message, index, from: state.view };
  state.view = "similar";
  state.conversation = null;
  state.matchedMid = null;
  state.terms = [];
  state.searchMedia = [];
  state.searchFiltered = false;
  $("main").innerHTML = `<div class="loading"><div class="loading-spinner"></div><p>Recherche des messages similaires…</p></div>`;
  const params = new URLSearchParams({
    platform: conv.platform, id: conv.conversation_id, k: String(SIMILAR_K),
  });
  if (message.message_id) params.set("mid", message.message_id);
  else params.set("i", String(index));
  try {
    const data = await (await fetch("/api/similar?" + params.toString())).json();
    if (data.error) throw new Error(data.error);
    state.groups = data.groups || [];
    renderGroups();
    renderSidebar();
  } catch (err) {
    renderError(err.message, () => showSimilar(conv, message, index));
  }
}

function backToConversation() {
  const similar = state.similar;
  if (!similar) { loadHome(); return; }
  state.conversation = similar.conversation;
  state.matchedMid = similar.message.message_id || null;
  state.view = similar.from || "default";
  renderConversation();
  renderSidebar();
  setTimeout(() => {
    const target = document.querySelector(".msg.matched") || document.querySelector(".msg");
    if (target) target.scrollIntoView({ block: "center" });
  }, 60);
}

/* ---------- export markdown d'une conversation ---------- */
function conversationMarkdown(conv) {
  const lines = [`# ${conv.title || conv.conversation_id}`, ""];
  lines.push(`- Plateforme : ${platformName(conv.platform)}`);
  if (conv.url) lines.push(`- URL : ${conv.url}`);
  if (conv.model) lines.push(`- Modèle : ${conv.model}`);
  if (conv.started_at || conv.last_message_at) {
    lines.push(`- Dates : ${formatTs(conv.started_at)} → ${formatTs(conv.last_message_at)}`);
  }
  lines.push(`- Messages : ${(conv.messages || []).length}`, "");
  for (const message of conv.messages || []) {
    const when = message.timestamp ? ` — ${formatTs(message.timestamp)}` : "";
    const model = message.model ? ` (${message.model})` : "";
    lines.push(`## ${roleLabel(message.role)}${model}${when}`, "", message.texte || "", "");
  }
  return lines.join("\n");
}

function copyFeedback(btn, text) {
  if (!navigator.clipboard) return;
  navigator.clipboard.writeText(text).then(() => {
    if (!btn.dataset.copyLabel) btn.dataset.copyLabel = btn.textContent;
    btn.classList.add("copied");
    btn.textContent = "✓ copié";
    setTimeout(() => {
      btn.textContent = btn.dataset.copyLabel;
      btn.classList.remove("copied");
      delete btn.dataset.copyLabel;
    }, 1300);
  });
}

function downloadConversation() {
  const conv = state.conversation;
  if (!conv) return;
  const slug = String(conv.title || conv.conversation_id)
    .toLowerCase().replace(/[^\w]+/g, "-").replace(/^-|-$/g, "").slice(0, 60)
    || "conversation";
  const blob = new Blob([conversationMarkdown(conv)], {
    type: "text/markdown;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${conv.platform}-${slug}.md`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/* ---------- recherche dans la conversation (sommaire) ---------- */
function conversationMatches(query) {
  const conv = state.conversation;
  const terms = tokenize(query || "");
  return ((conv && conv.messages) || []).map((message) => {
    const text = mdPlain(message.texte || "").toLowerCase();
    return terms.length > 0 && terms.every((term) => text.includes(term));
  });
}

function filterConversation(query) {
  if (!state.conversation) return;
  const matches = conversationMatches(query);
  const total = matches.filter(Boolean).length;
  document.querySelectorAll("#sidebar .anchor").forEach((anchor) => {
    anchor.hidden = query ? !matches[Number(anchor.dataset.i)] : false;
  });
  document.querySelectorAll("#main .msg").forEach((el) => {
    el.classList.toggle("search-hit", !!query && !!matches[Number(el.dataset.i)]);
  });
  const count = $("convCount");
  if (count) {
    count.hidden = !query;
    count.textContent = `${plural(total, "message")} trouvé${total > 1 ? "s" : ""}`;
  }
}

function jumpToFirstMatch() {
  const hit = document.querySelector("#main .msg.search-hit");
  if (hit) hit.scrollIntoView({ block: "center", behavior: "smooth" });
}

/* ---------- galerie des médias ---------- */
const GALLERY_PAGE = 60;
const gallery = { platform: null, kind: null, offset: 0, total: 0, items: [] };

async function openGallery(reset = true) {
  const fromGallery = !reset && state.view === "gallery";
  state.view = "gallery";
  state.conversation = null;
  state.groups = [];
  setHomeSidebar(true);
  if (reset) { gallery.offset = 0; gallery.items = []; }
  if (!fromGallery) {
    $("main").innerHTML = `<div class="loading"><div class="loading-spinner"></div><p>Chargement de la galerie…</p></div>`;
  }
  const params = new URLSearchParams({
    limit: String(GALLERY_PAGE), offset: String(gallery.offset),
  });
  if (gallery.platform) params.set("platform", gallery.platform);
  if (gallery.kind) params.set("kind", gallery.kind);
  try {
    const data = await (await fetch("/api/media?" + params.toString())).json();
    if (data.error) throw new Error(data.error);
    gallery.total = data.total || 0;
    gallery.items = gallery.items.concat(data.items || []);
    gallery.offset = gallery.items.length;
    // « charger plus » ajoute les cartes a la vue existante : la position
    // de defilement et le focus sont conserves
    if (fromGallery && appendGallery(data.items || [])) return;
    renderGallery();
  } catch (err) {
    renderError(err.message, () => openGallery(reset));
  }
}

/* Ajoute les cartes a la grille existante ; false si la vue a change. */
function appendGallery(items) {
  const grid = document.querySelector("#main .results-grid");
  if (!grid) return false;
  grid.insertAdjacentHTML("beforeend", items.map(mediaCard).join(""));
  const more = document.getElementById("galleryMore");
  const rest = gallery.total - gallery.items.length;
  if (more) {
    if (rest > 0) more.textContent = `Charger plus (${rest} restants)`;
    else more.closest(".more-wrap")?.remove();
  }
  return true;
}

function renderGallery() {
  setTitle("Galerie des médias");
  const platformChips = Object.keys(PLATFORM_LABEL).map((platform) => `
    <button class="chip${gallery.platform === platform ? " active" : ""}" type="button"
      data-gallery-platform="${platform}">${escapeHtml(platformName(platform))}</button>`).join("");
  const kindChips = [["", "Tous"], ["image", "Images"], ["audio", "Audio"], ["video", "Vidéos"]]
    .map(([value, label]) => `
      <button class="chip${(gallery.kind || "") === value ? " active" : ""}" type="button"
        data-gallery-kind="${value}">${label}</button>`).join("");
  const more = gallery.items.length < gallery.total
    ? `<div class="more-wrap"><button class="btn ghost" type="button" id="galleryMore">Charger plus (${gallery.total - gallery.items.length} restants)</button></div>`
    : "";
  const body = gallery.items.length
    ? `<div class="results-grid media-grid">${gallery.items.map((m) => mediaCard(m)).join("")}</div>${more}`
    : `<p class="section-note">Aucun média pour ces filtres.</p>`;
  $("main").innerHTML = `<div class="gallery-view">
    <div class="conv-head">
      <div>
        <h2>Galerie des médias</h2>
        <p class="section-note">${plural(gallery.total, "média")} indexé${gallery.total > 1 ? "s" : ""}</p>
      </div>
      <button class="btn ghost" type="button" id="galleryBack">← Retour</button>
    </div>
    <div class="gallery-filters">
      <div class="chip-list"><button class="chip${gallery.platform ? "" : " active"}" type="button"
        data-gallery-platform="">Toutes</button>${platformChips}</div>
      <div class="chip-list">${kindChips}</div>
    </div>
    ${body}
  </div>`;
  $("galleryBack").addEventListener("click", () => loadHome());
  const moreBtn = $("galleryMore");
  if (moreBtn) moreBtn.addEventListener("click", () => openGallery(false));
  $("main").querySelectorAll("[data-gallery-platform]").forEach((chip) =>
    chip.addEventListener("click", () => {
      gallery.platform = chip.dataset.galleryPlatform || null;
      openGallery(true);
    }));
  $("main").querySelectorAll("[data-gallery-kind]").forEach((chip) =>
    chip.addEventListener("click", () => {
      gallery.kind = chip.dataset.galleryKind || null;
      openGallery(true);
    }));
  $("main").querySelectorAll("[data-media-open]").forEach((card) =>
    card.addEventListener("click", (e) => {
      if (e.target.closest("audio, video, a")) return;
      openConversation(card.dataset.platform, card.dataset.id, card.dataset.mid || null);
    }));
}

/* ---------- page « Scrape quotidien » (comme run.py --daily) ---------- */
const dailyRun = {
  platforms: new Set(),
  running: false,
  poll: null,
};

function openDailyPage() {
  state.view = "daily";
  state.conversation = null;
  state.groups = [];
  setHomeSidebar(true);
  if (!dailyRun.platforms.size) {
    dailyRun.platforms = new Set(Object.keys(PLATFORM_LABEL));
  }
  renderDailyPage();
  loadDailyState();
  renderJobStatus();
}

function stopDailyPolling() {
  if (dailyRun.poll) { clearInterval(dailyRun.poll); dailyRun.poll = null; }
}

function startDailyPolling() {
  stopDailyPolling();
  dailyRun.poll = setInterval(async () => {
    if (!$("dailyState")) { stopDailyPolling(); return; } // page quittée
    await loadDailyState();
    if (!dailyRun.running) stopDailyPolling();
  }, 2500);
}

function renderDailyPage() {
  setTitle("Scrape quotidien");
  const chips = Object.keys(PLATFORM_LABEL).map((platform) => `
    <button class="chip${dailyRun.platforms.has(platform) ? " active" : ""}" type="button"
      data-daily-platform="${platform}">${escapeHtml(platformName(platform))}</button>`).join("");
  $("main").innerHTML = `<div class="parse-test">
    <div class="conv-head">
      <div>
        <h2>Scrape quotidien</h2>
        <p class="section-note">Même routine que <code>run.py --daily</code> : conversations
        inconnues + les 20 plus récentes de chaque chatbot. Les conversations déjà à jour
        ne sont pas réécrites.</p>
      </div>
      <button class="btn ghost" type="button" id="dailyBack">← Retour</button>
    </div>
    <div class="panel">
      <div class="test-form">
        <div class="test-platforms">
          <span class="filter-label">Chatbots à scraper</span>
          <div class="chip-list">${chips}</div>
        </div>
        <button class="btn" type="button" id="dailyStart">Lancer le daily</button>
      </div>
      <div id="dailyState" class="test-state"></div>
    </div>
  </div>`;
  $("dailyBack").addEventListener("click", () => { stopDailyPolling(); loadHome(); });
  $("dailyStart").addEventListener("click", startDaily);
  $("main").querySelectorAll("[data-daily-platform]").forEach((chip) =>
    chip.addEventListener("click", () => {
      const platform = chip.dataset.dailyPlatform;
      if (dailyRun.platforms.has(platform)) dailyRun.platforms.delete(platform);
      else dailyRun.platforms.add(platform);
      chip.classList.toggle("active", dailyRun.platforms.has(platform));
    }));
}

async function startDaily() {
  const box = $("dailyState");
  if (!dailyRun.platforms.size) {
    if (box) sectionNote(box, "Choisis au moins un chatbot.");
    return;
  }
  const button = $("dailyStart");
  if (button) button.disabled = true;
  try {
    const response = await fetch("/api/scrape", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platforms: [...dailyRun.platforms], mode: "daily" }),
    });
    const data = await response.json();
    if (data.error) throw new Error(data.error);
    dailyRun.running = true;
    renderDailyState(data);
    startDailyPolling();
  } catch (err) {
    renderDailyState({ error: err.message });
  } finally {
    // un job lance garde le bouton inactif jusqu'a la fin du run
    if (button && !dailyRun.running) button.disabled = false;
  }
}

async function loadDailyState() {
  try {
    const data = await (await fetch("/api/scrape")).json();
    dailyRun.running = !!data.running;
    renderDailyState(data);
    if (dailyRun.running) startDailyPolling();
    else stopDailyPolling();
  } catch (err) { /* silencieux : la page reste utilisable */ }
}

/* Un seul scrape a la fois : le bouton de lancement reste inactif pendant
   un run (y compris declenche par le cron) et reprend son libelle ensuite. */
function renderDailyStart(data) {
  const start = $("dailyStart");
  if (!start) return;
  const running = !!data.running;
  const mode = JOB_MODE_LABEL[data.mode] || "Scrape";
  start.disabled = running;
  start.textContent = running ? `${mode} en cours…` : "Lancer le daily";
  start.classList.toggle("running", running);
}

/* Badges par chatbot : « en cours… » tant que le scrape tourne, puis
   OK / partiel / ignoré selon le bilan du run. */
function renderDailyState(data) {
  const box = $("dailyState");
  if (!box) return;
  if (data.error) {
    box.innerHTML = `<p class="section-note">${escapeHtml(data.error)}</p>`;
    return;
  }
  if (!data.started_at) {
    box.innerHTML = `<p class="section-note">Aucun scrape lancé pour l'instant.</p>`;
    renderDailyStart(data);
    return;
  }
  const external = data.running && data.source === "cli";
  const steps = external ? "" : (data.platforms || []).map((platform) => {
    const done = (data.done || []).find((item) => item.platform === platform);
    let cls = "unknown";
    let label = "—";
    if (done) {
      if (done.skipped) {
        cls = "warn";
        label = `ignoré (${done.skipped})`;
      } else if (done.failed) {
        cls = "warn";
        label = `${done.exported} écrite(s) · ${done.failed} échec(s)`;
      } else {
        cls = "ok";
        label = `OK · ${done.exported} écrite(s)`;
      }
    } else if (data.running) {
      cls = "running";
      label = "en cours…";
    }
    return `<span class="test-step ${cls}">${escapeHtml(platformName(platform))} · ${escapeHtml(label)}</span>`;
  }).join("");
  const rows = (data.done || []).map((item) => {
    const badge = item.skipped ? "ignoré" : (item.failed ? "partiel" : "OK");
    const cls = item.skipped || item.failed ? "warn" : "ok";
    return `<div class="status-row">
      <span class="status-platform">${escapeHtml(platformName(item.platform))}</span>
      <span class="status-badge ${cls}">${badge}</span>
      <span class="status-meta">${item.discovered} découvertes · ${item.targets} cible${item.targets > 1 ? "s" : ""}</span>
      <span class="status-meta">${item.exported} écrite${item.exported > 1 ? "s" : ""}${item.patched ? ` (dont ${item.patched} patchée${item.patched > 1 ? "s" : ""})` : ""}</span>
      <span class="status-meta">${item.failed} échec${item.failed > 1 ? "s" : ""}</span>
    </div>`;
  }).join("");
  const log = (data.log || []).slice(-16)
    .map((line) => `<div class="test-log-line">${escapeHtml(line)}</div>`).join("");
  const modeLabel = JOB_MODE_LABEL[data.mode] || "Scrape";
  const progress = external ? "" : jobProgress(data);
  const status = data.running
    ? `${modeLabel} en cours depuis ${escapeHtml(elapsedLabel(data.started_at) || "…")}`
      + (progress ? ` · ${escapeHtml(progress.trim())} chatbots traités` : "")
      + (external ? " · lancé par le cron ou la ligne de commande"
        + " (progression détaillée indisponible)" : "")
    : `${modeLabel} terminé ${escapeHtml(relativeAge(data.finished_at))}.`;
  renderDailyStart(data);
  box.innerHTML = `${steps ? `<div class="test-steps">${steps}</div>` : ""}
    <p class="section-note">${status}</p>
    ${rows ? `<div class="status-list">${rows}</div>` : ""}
    <details class="test-log"${data.running ? " open" : ""}>
      <summary>Journal</summary><div class="test-log-body">${log || "<em>—</em>"}</div>
    </details>`;
}

/* ---------- indicateur « scrape en cours » (page, cron ou CLI) ----------
   Le state du job vient de `/api/scrape` : lancé depuis la page (mémoire du
   serveur) ou par le cron/CLI (fichier `.state/aicv-run.json`). Le même état
   alimente la barre d'état globale et le bouton du tableau de bord. */
const jobWatch = { timer: null, data: null };
const JOB_MODE_LABEL = { daily: "Daily", monthly: "Monthly" };

/* Durée lisible : « 12 s », « 3 min 05 s », « 1 h 20 ». */
function elapsedLabel(iso) {
  const then = Date.parse(iso || "");
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) {
    return seconds % 60 ? `${minutes} min ${seconds % 60} s` : `${minutes} min`;
  }
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} h ${String(rest).padStart(2, "0")}` : `${hours} h`;
}

/* Avancement « 2/6 chatbots » (vide si les plateformes sont inconnues). */
function jobProgress(data) {
  const total = (data.platforms || []).length;
  const done = (data.done || []).length;
  return total ? `${done}/${total} ` : "";
}

/* Libelle annonce par le lecteur d'ecran : il change rarement (mode,
   avancement), contrairement a la duree. */
function jobLabelText(data) {
  const mode = JOB_MODE_LABEL[data.mode] || "Scrape";
  const total = (data.platforms || []).length;
  // la progression par chatbot n'est connue que pour un job de la page
  const progress = data.source === "cli" ? "" : jobProgress(data);
  const parts = [`${mode} en cours`];
  if (progress) parts.push(`${progress}chatbot${total > 1 ? "s" : ""}`);
  return parts.join(" · ");
}

/* Duree et origine : information visuelle, mise a jour a chaque sondage. */
function jobMetaText(data) {
  const elapsed = elapsedLabel(data.started_at);
  return [
    elapsed ? `depuis ${elapsed}` : "",
    data.source === "cli" ? "lancé par le cron" : "",
  ].filter(Boolean).join(" · ");
}

function renderJobStatus() {
  const data = jobWatch.data;
  const running = !!(data && data.running);
  const mode = JOB_MODE_LABEL[(data && data.mode) || ""] || "Scrape";
  const bar = $("jobBar");
  if (bar) {
    // sur la page « Scrape quotidien », son propre panneau suffit
    const show = running && state.view !== "daily";
    bar.hidden = !show;
    if (show) {
      const label = $("jobLabel");
      const meta = $("jobElapsed");
      if (label) label.textContent = jobLabelText(data);
      if (meta) meta.textContent = jobMetaText(data);
    }
  }
  const dailyBtn = $("dailyBtn");
  if (dailyBtn) {
    const label = running ? `${mode} en cours…` : "Lancer un daily";
    if (dailyBtn.textContent !== label) dailyBtn.textContent = label;
    dailyBtn.classList.toggle("running", running);
    dailyBtn.title = running
      ? `${jobLabelText(data)} · ${jobMetaText(data)} — voir la progression`
      : "Ouvrir la page « Scrape quotidien »";
    if (running) dailyBtn.setAttribute("aria-label", `${label} — voir la progression`);
    else dailyBtn.removeAttribute("aria-label");
  }
}

async function pollJobStatus() {
  if (document.hidden) return; // onglet en arrière-plan : on reprend au retour
  try {
    const response = await fetch("/api/scrape");
    if (!response.ok) return;
    jobWatch.data = await response.json();
    renderJobStatus();
  } catch (err) { /* silencieux : l'indicateur n'est pas critique */ }
}

function startJobWatch() {
  pollJobStatus();
  if (jobWatch.timer) clearInterval(jobWatch.timer);
  jobWatch.timer = setInterval(pollJobStatus, 4000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pollJobStatus();
  });
}

/* ---------- page « Test des parseurs » (re-scrape + rapport) ---------- */
const parseTest = {
  platforms: new Set(),
  perPlatform: 3,
  running: false,
  poll: null,
};

function openParseTest() {
  state.view = "parse-test";
  state.conversation = null;
  state.groups = [];
  setHomeSidebar(true);
  if (!parseTest.platforms.size) {
    parseTest.platforms = new Set(Object.keys(PLATFORM_LABEL));
  }
  renderParseTest();
  loadParseTestState();
}

function stopParsePolling() {
  if (parseTest.poll) { clearInterval(parseTest.poll); parseTest.poll = null; }
}

function startParsePolling() {
  stopParsePolling();
  parseTest.poll = setInterval(async () => {
    if (!$("testState")) { stopParsePolling(); return; } // page quittée
    await loadParseTestState();
    if (!parseTest.running) stopParsePolling();
  }, 2500);
}

function renderParseTest() {
  setTitle("Test des parseurs");
  const chips = Object.keys(PLATFORM_LABEL).map((platform) => `
    <button class="chip${parseTest.platforms.has(platform) ? " active" : ""}" type="button"
      data-test-platform="${platform}">${escapeHtml(platformName(platform))}</button>`).join("");
  $("main").innerHTML = `<div class="parse-test">
    <div class="conv-head">
      <div>
        <h2>Test des parseurs</h2>
        <p class="section-note">Re-scrape un échantillon aléatoire de conversations
        (taille moyenne ou grande) et compare à l'archive : messages, alternance des rôles,
        images, tableaux, listes, langages de code, LaTeX. Rien n'est écrit dans l'archive.</p>
      </div>
      <button class="btn ghost" type="button" id="parseBack">← Retour</button>
    </div>
    <div class="panel">
      <div class="test-form">
        <div class="test-platforms">
          <span class="filter-label">Parseurs à tester</span>
          <div class="chip-list">${chips}</div>
        </div>
        <div class="test-count">
          <label class="filter-label" for="testCount">Conversations par parseur</label>
          <input type="number" id="testCount" class="test-input" min="1" max="20"
            value="${parseTest.perPlatform}" aria-label="Conversations par parseur">
        </div>
        <button class="btn" type="button" id="testStart">Lancer le test</button>
      </div>
      <div id="testState" class="test-state"></div>
    </div>
    <div id="testReport"></div>
  </div>`;
  $("parseBack").addEventListener("click", () => { stopParsePolling(); loadHome(); });
  $("testCount").addEventListener("change", (e) => {
    parseTest.perPlatform = Math.max(1, Math.min(20, Number(e.target.value) || 3));
    e.target.value = parseTest.perPlatform;
  });
  $("testStart").addEventListener("click", startParseTest);
  $("main").querySelectorAll("[data-test-platform]").forEach((chip) =>
    chip.addEventListener("click", () => {
      const platform = chip.dataset.testPlatform;
      if (parseTest.platforms.has(platform)) parseTest.platforms.delete(platform);
      else parseTest.platforms.add(platform);
      chip.classList.toggle("active", parseTest.platforms.has(platform));
    }));
}

async function startParseTest() {
  const box = $("testState");
  if (!parseTest.platforms.size) {
    if (box) sectionNote(box, "Choisis au moins un parseur.");
    return;
  }
  const button = $("testStart");
  if (button) button.disabled = true;
  try {
    const response = await fetch("/api/recheck", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        platforms: [...parseTest.platforms],
        per_platform: parseTest.perPlatform,
      }),
    });
    const data = await response.json();
    if (data.error) throw new Error(data.error);
    parseTest.running = true;
    renderParseTestState(data);
    startParsePolling();
  } catch (err) {
    renderParseTestState({ error: err.message });
  } finally {
    if (button) button.disabled = false;
  }
}

async function loadParseTestState() {
  try {
    const data = await (await fetch("/api/recheck")).json();
    parseTest.running = !!data.running;
    renderParseTestState(data);
    renderParseTestReport(data.reports || []);
    if (parseTest.running) startParsePolling();
    else stopParsePolling();
  } catch (err) { /* silencieux : la page reste utilisable */ }
}

function renderParseTestState(data) {
  const box = $("testState");
  if (!box) return;
  if (data.error) {
    box.innerHTML = `<p class="section-note">${escapeHtml(data.error)}</p>`;
    return;
  }
  if (!data.started_at) {
    box.innerHTML = `<p class="section-note">Aucun test lancé pour l'instant.</p>`;
    return;
  }
  const steps = (data.platforms || []).map((platform) => {
    const done = (data.done || []).find((item) => item.platform === platform);
    const cls = done ? done.verdict : (data.running ? "running" : "unknown");
    const label = done
      ? (VERDICT_LABEL[done.verdict] || done.verdict)
      : (data.running ? "en cours…" : "—");
    return `<span class="test-step ${escapeHtml(cls)}">${escapeHtml(platformName(platform))} · ${escapeHtml(label)}</span>`;
  }).join("");
  const log = (data.log || []).slice(-14)
    .map((line) => `<div class="test-log-line">${escapeHtml(line)}</div>`).join("");
  const status = data.running
    ? `Test en cours depuis ${escapeHtml(relativeAge(data.started_at))}…`
    : `Terminé ${escapeHtml(relativeAge(data.finished_at))}.`;
  box.innerHTML = `<div class="test-steps">${steps}</div>
    <p class="section-note">${status}</p>
    <details class="test-log"${data.running ? " open" : ""}>
      <summary>Journal</summary><div class="test-log-body">${log || "<em>—</em>"}</div>
    </details>`;
}

function renderParseTestReport(reports) {
  const box = $("testReport");
  if (!box) return;
  if (!reports.length) { box.innerHTML = ""; return; }
  const lines = reports.map((report) => {
    const issues = (report.issues || []).map((issue) => {
      const reason = issue.reason ? ` — ${escapeHtml(issue.reason)}` : "";
      const counts = issue.old_messages != null
        ? ` (${escapeHtml(issue.new_messages ?? "?")}/${escapeHtml(issue.old_messages)} messages)` : "";
      return `<li><strong>${escapeHtml(issue.title || issue.conversation_id || "?")}</strong>${reason}${counts}</li>`;
    }).join("");
    const compared = escapeHtml(report.compared ?? report.checked ?? 0);
    const row = `<tr>
      <td class="report-platform">${escapeHtml(platformName(report.platform))}</td>
      <td><span class="parse-badge ${escapeHtml(report.verdict)}">${escapeHtml(VERDICT_LABEL[report.verdict] || report.verdict)}</span></td>
      <td class="num col-compare">${compared}</td>
      <td class="num">${pctText(report.retention)}</td>
      <td class="num col-fail">${escapeHtml(report.failed || 0)}</td>
      <td class="report-age">${escapeHtml(relativeAge(report.checked_at))}</td>
    </tr>`;
    return issues ? `${row}<tr class="report-issues"><td colspan="6"><ul class="test-issues">${issues}</ul></td></tr>` : row;
  }).join("");
  box.innerHTML = `<h2 class="section-title">Rapport des défaillances</h2>
    <div class="panel report-panel"><div class="report-scroll">
      <table class="report-table" aria-label="Dernier contrôle par plateforme">
        <thead><tr>
          <th scope="col">Plateforme</th>
          <th scope="col">Verdict</th>
          <th scope="col" class="num col-compare">Comparées</th>
          <th scope="col" class="num">Rétention</th>
          <th scope="col" class="num col-fail">Échecs</th>
          <th scope="col">Vérifié</th>
        </tr></thead>
        <tbody>${lines}</tbody>
      </table></div>
    </div>`;
}

/* ---------- statut : santé des scrapers, index, sauvegardes ---------- */
async function loadStatus() {
  try {
    const data = await (await fetch("/api/status")).json();
    renderStatus(data);
  } catch (err) {
    ["indexBox"].forEach((id) => {
      const box = $(id);
      if (box) {
        sectionNote(box, "Impossible de charger le statut.");
        setPanelBusy(box, false);
      }
    });
  }
}

const VERDICT_LABEL = {
  ok: "OK", attention: "à surveiller", critique: "CRITIQUE", inconnu: "non vérifié",
};

function pctText(value) {
  return value == null ? "?" : `${Math.round(value * 100)} %`;
}

const ALERT_ICON = '<svg class="alert-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 4.6 21 19.4H3z"/><path d="M12 10.2v4.1"/><circle cx="12" cy="17.1" r=".95" fill="currentColor" stroke="none"/></svg>';

/* Contrôle des parseurs : bandeau d'alerte en haut de l'accueil quand une
   plateforme est critique (le détail vit dans la partie « Santé des scrapers »). */
function renderParseAlert(data) {
  const banner = $("parseAlert");
  if (!banner) return;
  const rows = (data.scrape || []).filter((item) => item.recheck);
  const critical = rows.filter((item) => item.recheck.verdict === "critique");
  const worried = rows.filter((item) => item.recheck.verdict === "attention");
  const names = (items) => items.map((item) =>
    `${platformName(item.platform)} (${pctText(item.recheck.retention)})`).join(", ");
  if (critical.length) {
    banner.hidden = false;
    banner.className = "alert-banner critical";
    banner.innerHTML = `${ALERT_ICON}
      <span><strong>Contrôle des parseurs : ${critical.length} plateforme${critical.length > 1 ? "s" : ""} en échec</strong>
      — ${escapeHtml(names(critical))}. Un re-scrape aléatoire ne retrouve plus ce que
      l'archive contient : voir la partie « Santé des scrapers ».</span>`;
  } else if (worried.length) {
    banner.hidden = false;
    banner.className = "alert-banner warn";
    banner.innerHTML = `${ALERT_ICON}
      <span><strong>Contrôle des parseurs : à surveiller</strong>
      — ${escapeHtml(names(worried))}.</span>`;
  } else {
    banner.hidden = true;
    banner.innerHTML = "";
  }
}

function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)} Go`;
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)} Mo`;
  if (value >= 1e3) return `${(value / 1e3).toFixed(0)} ko`;
  return `${value} o`;
}

function relativeAge(iso) {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const hours = (Date.now() - then) / 3600000;
  if (hours < 1) return "il y a moins d'une heure";
  if (hours < 24) return `il y a ${Math.round(hours)} h`;
  const days = Math.round(hours / 24);
  return `il y a ${days} jour${days > 1 ? "s" : ""}`;
}

function renderStatus(data) {
  dash.status = data;
  renderParseAlert(data);
  // la santé des scrapers est une partie du panneau « Activité » (rotation)
  renderRotator(heatRotator);
  const indexBox = $("indexBox");
  if (indexBox) {
    const idx = data.index || {};
    const media = idx.media || {};
    const backups = data.backups || [];
    const backupsText = backups.length
      ? `${backups.length} conservée${backups.length > 1 ? "s" : ""}, dernière ${escapeHtml(relativeAge(backups[0].created_at))}`
      : "aucune";
    indexBox.innerHTML = `
      <div class="stats status-stats">
        <div><span class="stat-value">${idx.messages || 0}</span><span class="stat-label">messages</span></div>
        <div><span class="stat-value">${idx.conversations || 0}</span><span class="stat-label">conversations</span></div>
        <div><span class="stat-value">${media.total || 0}</span><span class="stat-label">médias</span></div>
        <div><span class="stat-value">${formatBytes(idx.db_bytes)}</span><span class="stat-label">base</span></div>
      </div>
      <p class="section-note">Indexé ${escapeHtml(relativeAge(idx.indexed_at))} · sauvegardes : ${backupsText}</p>`;
    setPanelBusy(indexBox, false);
  }
}

/* Copier le markdown d'un message / ouvrir ses voisins sémantiques /
   comparer les versions alternatives. */
document.addEventListener("click", (e) => {
  const copy = e.target.closest("[data-copy-msg]");
  if (copy) {
    const messages = (state.conversation && state.conversation.messages) || [];
    const message = messages[Number(copy.dataset.copyMsg)];
    if (message) copyFeedback(copy, message.texte || "");
    return;
  }
  const similar = e.target.closest("[data-similar]");
  if (similar && state.conversation) {
    const index = Number(similar.dataset.similar);
    const message = (state.conversation.messages || [])[index];
    if (message) showSimilar(state.conversation, message, index);
    return;
  }
  const compare = e.target.closest("[data-compare]");
  if (compare) {
    const body = compare.closest(".variants-body");
    if (body) {
      const on = body.classList.toggle("compare");
      compare.textContent = on ? "Vue empilée" : "Comparer côte à côte";
    }
  }
});

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".copy-btn, [data-copy-text]");
  if (!btn) return;
  let text = btn.dataset.copyText;
  if (text === undefined) {
    // le bouton vit dans son bloc : un message peut en contenir plusieurs
    const block = btn.closest(".code-block");
    const code = block ? block.querySelector("code") : null;
    text = (code && code.textContent) || "";
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
