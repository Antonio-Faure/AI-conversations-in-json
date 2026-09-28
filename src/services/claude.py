"""Scraper Claude / Anthropic (claude.ai) via Playwright.

Claude virtualise le transcript : le DOM ne rend qu'une fenetre de messages
autour de la position de scroll (les autres sont remplaces par un spacer). Le
service accumule les rangees `[data-testid='transcript-row']` en remontant le
fil, puis reconstruit un HTML complet avant de le parser.

Avant chaque capture, les panneaux de raisonnement/thinking repliables sont
deplies (`[data-cds-row-toggle]`) : Claude n'insere le raisonnement integral
dans le DOM (`[data-cds-row-panel]`) qu'une fois le panneau ouvert.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from ..parsers.claude import ClaudeParser, merge_transcript_rows
from ..schema import ConversationRef
from .base import BaseService, ScrapedPage

#: rangee de message du transcript 2026 (une par message, virtualisee)
TURN_SELECTOR = "[data-testid='transcript-row']"

#: remise a zero de l'accumulateur JS des rangees
_RESET_ROWS_JS = (
    "window.__aicvClaude = {rows: Object.create(null), order: []};"
    "window.__aicvClaudeScroller = null;"
    "return 0;"
)

#: localise le conteneur scrollable du fil et le memorise (le scroll window
#: n'atteint pas le transcript, qui a son propre overflow)
_FIND_SCROLLER_JS = """
return (function(){
  const row = document.querySelector(__SELECTOR__);
  if (!row) return null;
  let el = row;
  while (el) {
    const cs = getComputedStyle(el);
    if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')
        && el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 150) {
      window.__aicvClaudeScroller = el;
      return {tag: el.tagName, client: el.clientHeight, height: el.scrollHeight,
              top: Math.round(el.scrollTop)};
    }
    el = el.parentElement;
  }
  return null;
})();
""".replace("__SELECTOR__", json.dumps(TURN_SELECTOR))

#: memorise les rangees visibles (cle = data-index) ; garde le rendu le plus
#: complet (une rangee capturee pendant un re-rendu peut etre plus courte)
_COLLECT_ROWS_JS = """
return (function(){
  const acc = window.__aicvClaude;
  if (!acc) return null;
  const nodes = document.querySelectorAll(__SELECTOR__);
  for (const n of nodes) {
    const idx = n.getAttribute('data-index');
    let key;
    if (idx !== null && idx !== '') {
      key = 'i:' + idx;
    } else {
      const text = (n.innerText || n.textContent || '')
        .replace(/\\s+/g, ' ').trim().slice(0, 160);
      key = 'h:' + text;
    }
    const html = n.outerHTML;
    const prev = acc.rows[key];
    if (prev === undefined) { acc.rows[key] = html; acc.order.push(key); }
    else if (html.length > prev.length) { acc.rows[key] = html; }
  }
  let setsize = 0;
  const holder = document.querySelector(__SETSIZE__);
  if (holder) setsize = parseInt(holder.getAttribute('aria-setsize'), 10) || 0;
  const s = window.__aicvClaudeScroller;
  return {dom: nodes.length, total: acc.order.length, setsize: setsize,
          top: s ? Math.round(s.scrollTop) : 0};
})();
""".replace("__SELECTOR__", json.dumps(TURN_SELECTOR)).replace(
    "__SETSIZE__",
    json.dumps("[data-testid='transcript-row'] [aria-setsize]"),
)

#: va au bas du fil (les messages recents s'y chargent)
_SCROLL_BOTTOM_JS = """
return (function(){
  const s = window.__aicvClaudeScroller;
  if (!s) return null;
  s.scrollTop = s.scrollHeight;
  return Math.round(s.scrollTop);
})();
"""

#: remonte le fil de ~60% d'ecran : le virtualiseur rend la fenetre suivante
_SCROLL_UP_JS = """
return (function(){
  const s = window.__aicvClaudeScroller;
  if (!s) return null;
  const step = Math.max(200, Math.round(s.clientHeight * 0.6));
  s.scrollTop = Math.max(0, s.scrollTop - step);
  return Math.round(s.scrollTop);
})();
"""

#: deploie un panneau repliable de statut (raisonnement/thinking).
#: Replie, le raisonnement complet n'est pas rendu : le panneau
#: `[data-cds-row-panel]` n'entre dans l'outerHTML qu'apres un clic sur le
#: toggle. Les etapes imbriquees (thinking-N) n'apparaissent qu'apres le
#: deploiement de la carte parente : un clic a la fois (le re-rendu React perd
#: les clics simultanes), la boucle de collecte alternant clic et capture.
_EXPAND_STATUS_JS = """
return (function(){
  const sels = [
    "[data-cds-row-toggle][aria-expanded='false']",
    "[data-test='thinking-toggle'][aria-expanded='false']",
    "[data-testid='thinking-block'] [aria-expanded='false']"
  ];
  for (const sel of sels) {
    const b = document.querySelector(sel);
    if (b) { b.click(); return 1; }
  }
  return 0;
})();
"""

#: retourne l'ensemble accumule (ordre de decouverte + HTML par cle)
_FINAL_ROWS_JS = """
return (function(){
  const acc = window.__aicvClaude;
  if (!acc) return {order: [], rows: {}};
  return {order: acc.order, rows: acc.rows};
})();
"""

#: selecteur de modele a reinjecter (perdu si on ne garde que les rangees)
_HEADER_JS = """
return (function(){
  const model = document.querySelector("[data-testid='model-selector-dropdown']");
  return model ? model.outerHTML : '';
})();
"""


class ClaudeService(BaseService):
    name = "claude"
    home_url = "https://claude.ai/chats"

    sidebar_scroll_selectors = (
        "[data-testid='sidebar-chats-list']",
        "nav[aria-label='Chats']",
        "aside nav",
        "aside",
    )
    sidebar_ready_selectors = (
        "a[href*='/chat/']",
        "[data-testid='sidebar-chats-list'] a",
    )
    login_url_parts = ("claude.ai/__clerc", "claude.ai/login", "accounts.google.com", "auth.openai", "/email/")
    login_selectors = (
        "a[href*='login']",
        "button:has-text('Log in')",
        "input[type='password']",
    )
    #: ecran vide Claude : transcript jamais charge (taches / conversations vides)
    empty_chat_selectors = ("[data-testid='empty-chat-screen']",)

    def build_parser(self) -> ClaudeParser:
        return ClaudeParser()

    def conversation_url(self, ref: ConversationRef) -> str:
        return ref.url or f"https://claude.ai/chat/{ref.id}"

    def extract_extras(self, ref: ConversationRef) -> Dict[str, Any]:
        extra: Dict[str, Any] = {}
        if ref.title:
            extra["title_hint"] = ref.title
        # claude.ai met a jour document.title avec le titre du chat
        page_title = self.session.evaluate("() => document.title")
        if page_title:
            extra["title_hint"] = extra.get("title_hint") or str(page_title).strip()
        return extra

    def scrape_conversation(self, ref: ConversationRef) -> ScrapedPage:
        """Charge le fil complet en accumulant les rangees virtualisees.

        Le pipeline commun ne voit que la fenetre rendue (~15 tours). On remonte
        le fil en memorisant chaque rangee (cle `data-index`), on reconstruit un
        HTML complet (sans doublon, trie par index) avant parsing.
        """
        page = super().scrape_conversation(ref)
        fragments = self._collect_conversation_rows()
        if fragments:
            header = self.session.eval_body(_HEADER_JS) or ""
            html = merge_transcript_rows(fragments, header_html=header)
            if html:
                page.html = html
        return page

    def _collect_conversation_rows(self) -> List[str]:
        """Remonte le fil en memorisant les rangees, retourne le HTML accumule."""
        scroll_cfg = self.config.get("scroll", {})
        pause_ms = max(200, int(scroll_cfg.get("pause_ms", 700)))
        max_rounds = max(120, int(scroll_cfg.get("max_rounds", 60)) * 4)
        stable_rounds = max(4, int(scroll_cfg.get("stable_rounds", 3)) + 1)

        self.session.eval_body(_RESET_ROWS_JS)
        if self.session.eval_body(_FIND_SCROLLER_JS) is None:
            return []
        self.session.eval_body(_SCROLL_BOTTOM_JS)
        self.session.wait_ms(pause_ms)

        target = 0
        last_total = -1
        stable = 0
        at_top = False
        for _ in range(max_rounds):
            # deploie le raisonnement visible avant de lire le DOM (sinon le
            # raisonnement integral n'est pas rendu dans l'outerHTML capte)
            self._expand_statuses()
            info = self.session.eval_body(_COLLECT_ROWS_JS) or {}
            total = int(info.get("total") or 0)
            setsize = int(info.get("setsize") or 0)
            if setsize:
                target = setsize
            if target and total >= target:
                break
            if at_top:
                stable = stable + 1 if total == last_total else 0
                if stable >= stable_rounds:
                    break
            last_total = total
            step = self.session.eval_body(_SCROLL_UP_JS)
            at_top = step is not None and int(step) <= 0
            self.session.wait_ms(pause_ms)

        data = self.session.eval_body(_FINAL_ROWS_JS) or {}
        rows = data.get("rows") or {}
        order = data.get("order") or []
        return [rows[key] for key in order if key in rows]

    def _expand_statuses(self, wait_ms: int = 600, max_clicks: int = 12) -> int:
        """Deploie les panneaux de raisonnement/thinking visibles.

        Claude ne rend le texte integral du raisonnement qu'une fois le panneau
        deplie. On clique un toggle a la fois (le re-rendu React annule les clics
        declenches simultanement) en attendant le contenu, puis la collecte
        capture l'outerHTML ; l'accumulateur gardant le rendu le plus long, une
        rangee capturee depliee n'est pas remplacee par un re-rendu replie.
        Retourne le nombre de panneaux effectivement ouverts.
        """
        opened = 0
        for _ in range(max_clicks):
            if not self.session.eval_body(_EXPAND_STATUS_JS):
                break
            opened += 1
            self.session.wait_ms(wait_ms)
        return opened
