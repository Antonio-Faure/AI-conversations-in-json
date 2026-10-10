"""Scraper Mistral (chat.mistral.ai) via navigateur (botasaurus anti-Cloudflare).

L'application /chat a fusionne dans /work : toutes les conversations vivent
sous `/work/<id>` (les anciennes URLs `/chat/<id>` sont reecrites).
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
from ..utils.logging import get_logger
from ..utils.urls import normalize_conversation_url

log = get_logger("services.mistral")

BASE = "https://chat.mistral.ai"

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

# -- capture des versions alternatives (editions/regenerations) ---------------

#: messages portant un switcher de versions (+ index de la version affichee)
_VARIANTS_FIND_JS = """
return (function(){
  const out = [];
  document.querySelectorAll('[data-message-version]').forEach(n => {
    const prev = n.querySelector("button[aria-label*='précédente' i]");
    const next = n.querySelector("button[aria-label*='suivante' i]");
    if (prev || next) {
      out.push({id: n.getAttribute('data-message-id') || '',
                v: parseInt(n.getAttribute('data-message-version'), 10) || 0});
    }
  });
  return out;
})();
"""

#: lit la version affichee d'un message (index + HTML du conteneur)
_VARIANTS_READ_JS = """
return (function(){
  const id = %s;
  const n = [...document.querySelectorAll('[data-message-version]')]
    .find(x => x.getAttribute('data-message-id') === id);
  if (!n) return null;
  return {v: parseInt(n.getAttribute('data-message-version'), 10) || 0,
          html: n.outerHTML};
})();
"""

#: clique « Version précédente / suivante » (false si absente ou desactivee)
_VARIANTS_CLICK_JS = """
return (function(){
  const id = %s, label = %s;
  const n = [...document.querySelectorAll('[data-message-version]')]
    .find(x => x.getAttribute('data-message-id') === id);
  if (!n) return false;
  const b = [...n.querySelectorAll('button')]
    .find(x => new RegExp(label, 'i').test(x.getAttribute('aria-label') || ''));
  if (!b || b.disabled) return false;
  b.click();
  return true;
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
    sidebar_ready_selectors = ("a[href^='/work/']", "a[href^='/chat/']")
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

    def build_parser(self) -> MistralParser:
        return MistralParser()

    def conversation_url(self, ref: ConversationRef) -> str:
        return normalize_conversation_url(ref.url) or f"{BASE}/work/{ref.id}"

    # -- decouverte (work uniquement) ------------------------------------------

    def _discover_once(self) -> Dict[str, ConversationRef]:
        collected: Dict[str, ConversationRef] = {}
        for attempt in range(3):
            self.session.goto(self.home_url)
            self.session.wait_ms(2000 if attempt == 0 else 3000)
            self._assert_not_blocked("mistral/work")
            if self.session.looks_logged_out(list(self.login_url_parts), list(self.login_selectors)):
                raise ServiceNotLoggedIn(
                    "mistral: pas de session valide -> `run.py --login mistral`"
                )
            refs = self._collect_sidebar_refs()
            # /chat est legacy (fusion) : on accepte encore les deux prefixes
            collected = {
                cid: ref
                for cid, ref in refs.items()
                if "/work/" in (ref.url or "") or "/chat/" in (ref.url or "")
            }
            if collected:
                break
        for ref in collected.values():
            ref.url = normalize_conversation_url(ref.url)
        return collected

    def list_conversations(self, limit: Optional[int] = None) -> List[ConversationRef]:
        refs = list(self._discover_once().values())
        return refs[:limit] if limit else refs

    # -- scraping --------------------------------------------------------------

    def scrape_conversation(self, ref: ConversationRef):
        """Charge le fil complet en accumulant les tours virtualises.

        Le pipeline commun scrolle la fenetre (`window`), que le fil Mistral
        n'utilise pas : seul le conteneur interne defile, et le DOM ne monte
        qu'une fenetre de tours. On accumule donc chaque tour en remontant ce
        conteneur, on reconstitue l'ordre, puis on remplace le HTML avant
        parsing si la collecte apporte des tours supplementaires.
        """
        page = super().scrape_conversation(ref)
        merged = self._collect_conversation()
        if merged and merged.count(TURN_SELECTOR) > page.html.count(TURN_SELECTOR):
            page.html = merged
        if self.config.get("variants"):
            try:
                page.extra["variants"] = self._capture_variants()
            except Exception:  # noqa: BLE001
                log.debug("mistral: capture des variantes ignoree", exc_info=True)
        return page

    # -- versions alternatives (editions/regenerations) ------------------------

    def _variant_read(self, message_id: str) -> Optional[Dict[str, Any]]:
        data = self.session.eval_body(_VARIANTS_READ_JS % json.dumps(message_id))
        return data if isinstance(data, dict) else None

    def _variant_click(self, message_id: str, label: str) -> bool:
        return bool(
            self.session.eval_body(
                _VARIANTS_CLICK_JS % (json.dumps(message_id), json.dumps(label))
            )
        )

    def _variant_wait(
        self, message_id: str, want: int, timeout_ms: int = 8000
    ) -> Optional[Dict[str, Any]]:
        """Attend que la version affichee passe a `want` (sinon rend l'etat)."""
        elapsed = 0
        while elapsed < timeout_ms:
            data = self._variant_read(message_id)
            if data is not None and data.get("v") == want:
                return data
            self.session.wait_ms(400)
            elapsed += 400
        return self._variant_read(message_id)

    def _capture_variants(self) -> Dict[str, Any]:
        """Parcourt les switchers de versions et capture chaque rendu.

        Retourne ``{message_id: {"active": position, "fragments": [html...]}}``
        (premier = plus ancienne edition). Restaure la version affichee au
        depart.
        """
        captured: Dict[str, Any] = {}
        for item in self.session.eval_body(_VARIANTS_FIND_JS) or []:
            message_id = str(item.get("id") or "")
            if not message_id:
                continue
            original = int(item.get("v") or 0)
            current = self._variant_read(message_id)
            if current is None:
                continue
            fragments: Dict[int, str] = {int(current["v"]): str(current["html"])}
            # monter jusqu'a la derniere version
            for _ in range(50):
                if not self._variant_click(message_id, "suivante"):
                    break
                nxt = self._variant_wait(message_id, int(current["v"]) + 1)
                if nxt is None or int(nxt["v"]) <= int(current["v"]):
                    break
                current = nxt
                fragments[int(current["v"])] = str(current["html"])
            # redescendre jusqu'a la premiere
            for _ in range(50):
                if not self._variant_click(message_id, "précédente"):
                    break
                prev = self._variant_wait(message_id, int(current["v"]) - 1)
                if prev is None or int(prev["v"]) >= int(current["v"]):
                    break
                current = prev
                fragments[int(current["v"])] = str(current["html"])
            # restaurer la version affichee au depart
            for _ in range(original):
                if not self._variant_click(message_id, "suivante"):
                    break
                nxt = self._variant_wait(message_id, int(current["v"]) + 1)
                if nxt is None:
                    break
                current = nxt
            ordered_versions = sorted(fragments)
            if len(ordered_versions) >= 2:
                active = (
                    ordered_versions.index(original)
                    if original in ordered_versions
                    else 0
                )
                captured[message_id] = {
                    "active": active,
                    "fragments": [fragments[i] for i in ordered_versions],
                }
        return captured

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

