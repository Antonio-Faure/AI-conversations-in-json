"""Scraper Mistral (chat.mistral.ai) via navigateur (botasaurus anti-Cloudflare).

Mistral a deux modes a scraper :
  - /chat  : chatbot (historique principal)
  - /work  : mode agentique

Le contenu d'une conversation n'est rendu qu'apres activation du mode via
l'« app switcher » ; `_ensure_mode` s'en charge avant chaque navigation.
Les fils longs etant virtualises (seule une fenetre de tours est montee), le
fil est collecte de maniere incrementale en remontant le conteneur interne
(voir `_collect_conversation`).
L'authentification se fait par les cookies du profil persistant
(`profiles/mistral`, via `run.py --login mistral`).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..parsers.mistral import MistralParser, merge_message_fragments
from ..schema import ConversationRef
from ..services.base import BaseService, ServiceNotLoggedIn

BASE = "https://chat.mistral.ai"
MODES = ("chat", "work")

#: selecteur d'un tour (sert aussi de selecteur d'attente du rendu)
TURN_SELECTOR = "div[data-message-author-role]"

# -- collecte incrementale du fil (virtualisation) ----------------------------

#: remise a zero de l'accumulateur JS des tours
_RESET_JS = (
    "window.__aicvMistral = {rows: Object.create(null), edges: [], seq: []};"
    "window.__aicvMistralScroller = null;"
    "return 0;"
)

#: localise le conteneur scrollable du fil (le scroll window ne l'atteint pas)
_FIND_SCROLLER_JS = """
return (function(){
  const first = document.querySelector(__SELECTOR__);
  if (!first) return null;
  let el = first;
  while (el) {
    const cs = getComputedStyle(el);
    if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')
        && el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 150) {
      window.__aicvMistralScroller = el;
      return {client: el.clientHeight, height: el.scrollHeight,
              top: Math.round(el.scrollTop)};
    }
    el = el.parentElement;
  }
  return null;
})();
""".replace("__SELECTOR__", json.dumps(TURN_SELECTOR))

#: memorise les tours montes (cle = data-message-id) et les aretes d'ordre
_COLLECT_JS = """
return (function(){
  const acc = window.__aicvMistral;
  if (!acc) return null;
  const nodes = Array.from(document.querySelectorAll(__SELECTOR__));
  let previous = null;
  for (const node of nodes) {
    const id = node.getAttribute('data-message-id') || node.id || '';
    if (!id) continue;
    const html = node.outerHTML;
    const old = acc.rows[id];
    if (old === undefined) {
      acc.rows[id] = html;
      acc.seq.push(id);
    } else if (html.length > old.length) {
      acc.rows[id] = html;
    }
    if (previous && previous !== id) acc.edges.push([previous, id]);
    previous = id;
  }
  const s = window.__aicvMistralScroller;
  return {dom: nodes.length, total: acc.seq.length,
          top: s ? Math.round(s.scrollTop) : 0,
          height: s ? s.scrollHeight : 0};
})();
""".replace("__SELECTOR__", json.dumps(TURN_SELECTOR))

#: amene le fil en bas (les tours recents s'y chargent)
_SCROLL_BOTTOM_JS = """
return (function(){
  const s = window.__aicvMistralScroller;
  if (!s) return null;
  s.scrollTop = s.scrollHeight;
  return Math.round(s.scrollTop);
})();
"""

#: remonte le fil de ~85% d'ecran : le virtualiseur rend la fenetre precedente
_SCROLL_UP_JS = """
return (function(){
  const s = window.__aicvMistralScroller;
  if (!s) return null;
  const step = Math.max(200, Math.round(s.clientHeight * 0.85));
  s.scrollTop = Math.max(0, s.scrollTop - step);
  return Math.round(s.scrollTop);
})();
"""

#: retourne l'ensemble accumule (tours + aretes + ordre de decouverte)
_FINAL_JS = """
return (function(){
  const acc = window.__aicvMistral;
  if (!acc) return {rows: {}, edges: [], seq: []};
  return {rows: acc.rows, edges: acc.edges, seq: acc.seq};
})();
"""


class MistralService(BaseService):
    name = "mistral"
    home_url = BASE + "/work"

    sidebar_scroll_selectors = (
        "div.sidebar-dynamic-scroll-area-viewport",
        "div.overflow-y-auto",
        "aside",
    )
    sidebar_ready_selectors = ("a[href^='/chat/']", "a[href^='/work/']")
    # pas de selecteur href*=login : les conversations peuvent contenir de tels
    # liens (faux positif -> "session perdue"). L'URL de login suffit.
    login_url_parts = (
        "chat.mistral.ai/login",
        "chat.mistral.ai/auth",
        "signin",
        "login",
        "accounts.google",
    )
    login_selectors = (
        "button:has-text('Sign in')",
        "button:has-text('Se connecter')",
        "input[type='password']",
    )

    def __init__(self, session, config: Optional[Dict[str, Any]] = None):
        super().__init__(session, config)
        self._mode: Optional[str] = None

    def build_parser(self) -> MistralParser:
        return MistralParser()

    def conversation_url(self, ref: ConversationRef) -> str:
        mode = (ref.raw or {}).get("mode", "work")
        return ref.url or f"{BASE}/{mode}/{ref.id}"

    # -- activation du mode via l'app switcher ---------------------------------

    def _open_selected_app(self, mode: str) -> None:
        """Ouvre l'app switcher puis clique le lien du mode."""
        opened = self.session.eval_body(
            "const b=[...document.querySelectorAll('button')].find("
            "x=>/app switcher/i.test(x.getAttribute('aria-label')||''));"
            "if(b){b.click();return true}return false;"
        )
        if not opened:
            return
        self.session.wait_ms(1200)
        self.session.eval_body(
            "const a=document.querySelector(\"a[href='/%s']\");"
            "if(a){a.click();return true}return false;" % mode
        )

    def _ensure_mode(self, mode: str) -> None:
        """Charge le shell puis active le mode demande (chat|work)."""
        if self._mode == mode:
            return
        self.session.goto(self.home_url)
        self.session.wait_ms(3000)
        if mode != "work":
            self._open_selected_app(mode)
            self.session.wait_ms(4000)
        self._mode = mode

    # -- decouverte (chat + work) ----------------------------------------------

    def _discover_mode(self, mode: str) -> Dict[str, ConversationRef]:
        collected: Dict[str, ConversationRef] = {}
        for attempt in range(3):
            self._ensure_mode(mode)
            self.session.wait_ms(2000 if attempt == 0 else 3000)
            self._assert_not_blocked(f"mistral/{mode}")
            if self.session.looks_logged_out(list(self.login_url_parts), list(self.login_selectors)):
                raise ServiceNotLoggedIn(
                    "mistral: pas de session valide -> `run.py --login mistral`"
                )
            refs = self._collect_sidebar_refs()
            collected = {
                cid: ref
                for cid, ref in refs.items()
                if f"/{mode}/" in (ref.url or "")
            }
            if collected:
                break
        for ref in collected.values():
            ref.raw = dict(ref.raw or {})
            ref.raw["mode"] = mode
        return collected

    def list_conversations(self, limit: Optional[int] = None) -> List[ConversationRef]:
        refs: Dict[str, ConversationRef] = {}
        for mode in MODES:
            for cid, ref in self._discover_mode(mode).items():
                refs.setdefault(cid, ref)
            if limit and len(refs) >= limit:
                break
        return list(refs.values())

    # -- scraping --------------------------------------------------------------

    def scrape_conversation(self, ref: ConversationRef):
        """Charge le fil complet en accumulant les tours virtualises.

        Le pipeline commun scrolle la fenetre (`window`), que le fil Mistral
        n'utilise pas : seul le conteneur interne defile, et le DOM ne monte
        qu'une fenetre de tours. On accumule donc chaque tour en remontant ce
        conteneur, on reconstitue l'ordre, puis on remplace le HTML avant
        parsing si la collecte apporte des tours supplementaires.
        """
        self._ensure_mode((ref.raw or {}).get("mode", "work"))
        page = super().scrape_conversation(ref)
        merged = self._collect_conversation()
        if merged and merged.count(TURN_SELECTOR) > page.html.count(TURN_SELECTOR):
            page.html = merged
        return page

    def _collect_conversation(self) -> str:
        """Remonte le fil en memorisant les tours, retourne le HTML accumule."""
        scroll_cfg = self.config.get("scroll", {})
        pause_ms = max(250, int(scroll_cfg.get("pause_ms", 700)))
        max_rounds = max(120, int(scroll_cfg.get("max_rounds", 60)) * 4)
        stable_rounds = max(4, int(scroll_cfg.get("stable_rounds", 3)) + 1)

        self.session.eval_body(_RESET_JS)
        if self.session.eval_body(_FIND_SCROLLER_JS) is None:
            return ""
        # s'assurer d'etre au bas du fil (les tours recents s'y chargent)
        for _ in range(4):
            self.session.eval_body(_SCROLL_BOTTOM_JS)
            self.session.wait_ms(pause_ms)

        last_total = -1
        stable = 0
        at_top = False
        for _ in range(max_rounds):
            info = self.session.eval_body(_COLLECT_JS) or {}
            total = int(info.get("total") or 0)
            if at_top:
                stable = stable + 1 if total == last_total else 0
                if stable >= stable_rounds:
                    break
            last_total = total
            step = self.session.eval_body(_SCROLL_UP_JS)
            at_top = step is not None and int(step) <= 0
            self.session.wait_ms(pause_ms)

        data = self.session.eval_body(_FINAL_JS) or {}
        return merge_message_fragments(
            data.get("rows") or {},
            data.get("edges") or [],
            data.get("seq") or [],
        )

