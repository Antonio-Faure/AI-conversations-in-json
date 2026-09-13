"""Scraper ChatGPT / OpenAI (chatgpt.com) via Playwright.

Particularites :
  - sidebar scrollable infinie (nav[aria-label="Chat history"])
  - timestamps absents du DOM : recuperes depuis les props React
    (`memoizedProps.message.create_time`) via page.evaluate
  - modele par message : attribut data-message-model-slug (+ props React)
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from ..parsers.chatgpt import ChatGPTParser, merge_turn_snapshots
from ..schema import ConversationRef
from .base import BaseService, ScrapedPage

#: JS de collecte des donnees internes React par message.
#: create_time est un epoch (secondes) ou {t: secondes, s: nanos} selon les builds.
EXTRACT_REACT_META_JS = """
() => {
  const out = { messages: {}, title: null };
  const fiberKey = (el) =>
    Object.keys(el).find((k) => k.startsWith('__reactFiber$') || k.startsWith('__reactInternalInstance$'));
  const normTime = (ct) => {
    if (ct == null) return null;
    if (typeof ct === 'number') return ct > 1e12 ? ct / 1000 : ct;
    if (typeof ct === 'object' && typeof ct.t === 'number')
      return ct.t + (ct.s || 0) / 1e9 + (ct.ns || 0) / 1e9;
    return null;
  };
  const nodes = document.querySelectorAll('[data-message-id]');
  nodes.forEach((el) => {
    const id = el.getAttribute('data-message-id');
    const entry = {
      time: null,
      model: el.getAttribute('data-message-model-slug') || null,
      role: el.getAttribute('data-message-author-role') || null,
    };
    try {
      const key = fiberKey(el);
      let fiber = key ? el[key] : null;
      for (let hops = 0; fiber && hops < 40; hops++) {
        const props = fiber.memoizedProps || {};
        const msg = props.message || (props.data && props.data.message) || props.node;
        if (msg && msg.author && msg.author.role) {
          const t = normTime(msg.create_time || msg.timestamp || msg.created_at);
          if (t) entry.time = t;
          const mm = (msg.metadata || {}).model;
          if (mm && msg.author.role === 'assistant') entry.model = mm;
          entry.role = msg.author.role;
          break;
        }
        fiber = fiber.return;
      }
    } catch (e) { /* DOM/React interne different : on garde les attributs data-* */ }
    out.messages[id] = entry;
  });
  const h = document.querySelector("[data-testid='history-title'], header h1");
  if (h) out.title = (h.textContent || '').trim() || null;
  return out;
}
"""

#: selecteur des tours ChatGPT (conversation-turn-N, images generees incluses)
TURN_SELECTOR = "[data-testid^='conversation-turn']"

#: remise a zero de l'accumulateur JS des tours
_RESET_TURNS_JS = """
window.__aicvChatgpt = {map: Object.create(null), snaps: [], seen: Object.create(null)};
return 0;
"""

#: localise le conteneur scrollable du fil (le scroll `window` ne l'atteint pas)
_FIND_SCROLLER_JS = """
return (function(){
  const acc = window.__aicvChatgpt;
  const turn = document.querySelector(__SELECTOR__);
  if (!acc || !turn) return null;
  let el = turn;
  while (el) {
    const cs = getComputedStyle(el);
    if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')
        && el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 150) {
      acc.scroller = el;
      return {tag: el.tagName, client: el.clientHeight, height: el.scrollHeight};
    }
    el = el.parentElement;
  }
  return null;
})();
""".replace("__SELECTOR__", json.dumps(TURN_SELECTOR))

#: memorise la fenetre de tours visible (cle = message-id ou contenu)
_COLLECT_TURNS_JS = """
return (function(){
  const acc = window.__aicvChatgpt;
  if (!acc) return null;
  const nodes = document.querySelectorAll(__SELECTOR__);
  const keyOf = function(n){
    let mid = n.getAttribute('data-message-id');
    if (!mid) {
      const holder = n.querySelector('[data-message-id]');
      if (holder) mid = holder.getAttribute('data-message-id');
    }
    if (mid) return 'm:' + mid;
    const imgs = [];
    const list = n.querySelectorAll('img');
    for (let i = 0; i < list.length; i++)
      imgs.push(list[i].getAttribute('src') || list[i].getAttribute('alt') || '');
    const text = (n.innerText || n.textContent || '')
      .replace(/\\s+/g, ' ').trim().slice(0, 200);
    return 'h:' + text + '|' + imgs.join('|');
  };
  const keys = [];
  let fresh = 0;
  for (let i = 0; i < nodes.length; i++) {
    const key = keyOf(nodes[i]);
    keys.push(key);
    if (!(key in acc.seen)) { acc.seen[key] = 1; fresh++; }
    // garde le rendu le plus complet : un tour capture pendant son chargement
    // (placeholder « streaming ») est remplace par le rendu final plus long
    const html = nodes[i].outerHTML;
    const previous = acc.map[key];
    if (previous === undefined || html.length > previous.length) acc.map[key] = html;
  }
  if (fresh > 0 && keys.length) acc.snaps.push(keys);
  const s = acc.scroller;
  return {
    dom: nodes.length,
    fresh: fresh,
    total: Object.keys(acc.seen).length,
    top: s ? Math.round(s.scrollTop) : 0,
    client: s ? s.clientHeight : 0,
    height: s ? s.scrollHeight : 0,
  };
})();
""".replace("__SELECTOR__", json.dumps(TURN_SELECTOR))

#: va au bas du fil (les tours recents s'y chargent)
_SCROLL_BOTTOM_JS = """
return (function(){
  const acc = window.__aicvChatgpt;
  const s = acc && acc.scroller;
  if (!s) return null;
  s.scrollTop = s.scrollHeight;
  return Math.round(s.scrollTop);
})();
"""

#: remonte le fil par petits pas (charge les tours anciens)
_SCROLL_UP_JS = """
return (function(){
  const acc = window.__aicvChatgpt;
  const s = acc && acc.scroller;
  if (!s) return null;
  const step = Math.max(200, Math.round(s.clientHeight * 0.8));
  s.scrollTop = Math.max(0, s.scrollTop - step);
  return Math.round(s.scrollTop);
})();
"""

#: retourne les fenetres memorisees + le HTML de chaque tour
_FINAL_TURNS_JS = """
return (function(){
  const acc = window.__aicvChatgpt;
  if (!acc) return {snaps: [], map: {}};
  return {snaps: acc.snaps, map: acc.map};
})();
"""


class ChatGPTService(BaseService):
    name = "chatgpt"
    home_url = "https://chatgpt.com/"

    sidebar_scroll_selectors = (
        "nav[aria-label='Chat history']",
        "aside nav",
        ".nav-scrollable-container",
        "aside",
    )
    sidebar_ready_selectors = (
        "nav[aria-label='Chat history'] a[href*='/c/']",
        "a[href*='/c/']",
    )
    login_url_parts = ("chatgpt.com/auth", "openai.com/auth", "signin", "login")
    login_selectors = (
        "a[data-testid='login-button']",
        "a:has-text('Log in')",
        "a[href*='/auth/login']",
        "button:has-text('Log in')",
        "input[type='password']",
    )
    #: ecran d'accueil vide : conversation non chargeable (supprimee/inaccessible)
    empty_chat_selectors = (
        "text=/ready when you are/i",
        "text=/ready to dive in/i",
    )

    def build_parser(self) -> ChatGPTParser:
        return ChatGPTParser()

    def extract_extras(self, ref: ConversationRef) -> Dict[str, Any]:
        data = self.session.evaluate(EXTRACT_REACT_META_JS) or {}
        extra: Dict[str, Any] = {"messages": data.get("messages") or {}}
        if data.get("title"):
            extra["title_hint"] = data["title"]
        if ref.title:
            extra.setdefault("title_hint", ref.title)
        return extra

    def scrape_conversation(self, ref: ConversationRef) -> ScrapedPage:
        """Charge le fil complet en accumulant les tours pendant le scroll.

        ChatGPT virtualise les fils longs : le pipeline commun ne voit que la
        fenetre rendue. On collecte chaque tour (cle message-id/contenu) en
        remontant le conteneur de conversation, on fusionne sans doublon, puis
        on remplace le HTML par l'ensemble reconstruit avant parsing.
        """
        page = super().scrape_conversation(ref)
        fragments, meta = self._collect_conversation_turns()
        if fragments:
            page.html = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
                + "".join(fragments)
                + "</body></html>"
            )
        if meta:
            merged = dict(page.extra.get("messages") or {})
            merged.update(meta)
            page.extra["messages"] = merged
        return page

    def _collect_conversation_turns(self) -> Tuple[List[str], Dict[str, Any]]:
        """Remonte le fil en memorisant les tours, retourne (fragments, meta)."""
        scroll_cfg = self.config.get("scroll", {})
        pause_ms = max(200, int(scroll_cfg.get("pause_ms", 700)))
        stable_rounds = max(4, int(scroll_cfg.get("stable_rounds", 3)))
        max_rounds = max(80, int(scroll_cfg.get("max_rounds", 60)) * 3)

        self.session.eval_body(_RESET_TURNS_JS)
        if self.session.eval_body(_FIND_SCROLLER_JS) is None:
            return [], {}

        meta: Dict[str, Any] = {}

        def collect() -> Dict[str, Any]:
            info = self.session.eval_body(_COLLECT_TURNS_JS) or {}
            data = self.session.evaluate(EXTRACT_REACT_META_JS) or {}
            for message_id, entry in (data.get("messages") or {}).items():
                meta.setdefault(message_id, entry)
            return info

        # s'assurer d'etre au bas du fil (les tours recents s'y chargent)
        for _ in range(4):
            self.session.eval_body(_SCROLL_BOTTOM_JS)
            self.session.wait_ms(pause_ms)
            collect()

        last_total = -1
        stable = 0
        for _ in range(max_rounds):
            info = collect()
            total = int(info.get("total") or 0)
            if total == last_total:
                stable += 1
            else:
                stable = 0
            last_total = total
            if int(info.get("top") or 0) <= 0 and stable >= stable_rounds:
                break
            self.session.eval_body(_SCROLL_UP_JS)
            self.session.wait_ms(pause_ms)

        data = self.session.eval_body(_FINAL_TURNS_JS) or {}
        snapshots = data.get("snaps") or []
        html_by_key = data.get("map") or {}
        return merge_turn_snapshots(snapshots, html_by_key), meta
