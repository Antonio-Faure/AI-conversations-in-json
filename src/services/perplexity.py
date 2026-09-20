"""Scraper Perplexity (perplexity.ai).

Decouverte : l'API GraphQL de la library (persisted query) liste TOUT
(la liste DOM est virtualisee et la sidebar ne montre que le recent).
Fallback : collecteur DOM incrementiel si l'API change.

Le fil d'une conversation est lui aussi virtualise : le DOM ne monte qu'une
fenetre de messages (`min-height` reserves pour les autres). `scrape_conversation`
remonte donc le conteneur `.scrollable-container` en accumulant les messages
(HTML le plus long gagne) avant de reconstruire un HTML complet a parser.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..parsers.base import ParseError
from ..parsers.perplexity import PerplexityParser, merge_thread_messages
from ..schema import ConversationRef
from ..utils.logging import get_logger, log_fields
from .base import BaseService, ConversationUnavailableError, ScrapedPage

log = get_logger("services")

# -- collecte incrementale du fil (virtualisation) ---------------------------

#: messages user (bulle Tailwind 2026)
_USER_SELECTOR = "div[class~='group/user-bubble']"
#: reponse assistant (enveloppe du tour) ; le corps doit contenir `lm`
_ASSISTANT_SELECTOR = "div[data-workflow-final-text]"

#: remise a zero de l'accumulateur JS
_RESET_THREAD_JS = """
window.__aicvPpl = {rows: Object.create(null), pos: Object.create(null),
                    roles: Object.create(null), scroller: null,
                    ids: new WeakMap(), idSeq: 0, round: 0};
return 0;
"""

#: conteneur scrollable du fil (classe stable `scrollable-container`) ; on
#: choisit celui qui contient le plus de messages (il peut y en avoir d'autres)
_FIND_SCROLLER_JS = """
return (function(){
  let el = null, best = -1;
  const candidates = Array.from(document.querySelectorAll('.scrollable-container'));
  for (const c of candidates) {
    const n = c.querySelectorAll(__USER__).length
            + c.querySelectorAll(__ASSIST__).length;
    if (n > best) { el = c; best = n; }
  }
  if (!el) {
    const node = document.querySelector(__USER__) || document.querySelector(__ASSIST__);
    if (!node) return null;
    el = node;
    while (el) {
      const cs = getComputedStyle(el);
      if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll')
          && el.scrollHeight > el.clientHeight + 50 && el.clientHeight > 150) break;
      el = el.parentElement;
    }
  }
  if (!el) return null;
  window.__aicvPpl.scroller = el;
  const box = el.getBoundingClientRect();
  return {tag: el.tagName, client: el.clientHeight, height: el.scrollHeight,
          top: Math.round(el.scrollTop),
          cx: Math.round(box.left + box.width / 2),
          cy: Math.round(box.top + box.height / 2)};
})();
""".replace("__USER__", json.dumps(_USER_SELECTOR)).replace(
    "__ASSIST__", json.dumps(_ASSISTANT_SELECTOR)
)

#: va au bas du fil (les messages recents s'y chargent)
_SCROLL_BOTTOM_JS = """
return (function(){
  const s = window.__aicvPpl && window.__aicvPpl.scroller;
  if (!s) return null;
  s.scrollTop = s.scrollHeight;
  return Math.round(s.scrollTop);
})();
"""

#: remonte le fil : les messages plus anciens se prepent au-dessus
_SCROLL_UP_JS = """
return (function(){
  const s = window.__aicvPpl && window.__aicvPpl.scroller;
  if (!s) return null;
  const step = Math.max(300, Math.round(s.clientHeight * 0.85));
  s.scrollTop = Math.max(0, s.scrollTop - step);
  return Math.round(s.scrollTop);
})();
"""

#: memorise les messages montes. Identite stable par noeud DOM (WeakMap) : on
#: ne fusionne plus deux tours distincts au corps identique. L'ordre suit la
#: remontee du fil (les nouveaux sont plus anciens) via un rang decroissant,
#: ce qui reste fiable malgre les decalages de `scrollHeight` quand les
#: messages anciens se prepent.
_COLLECT_THREAD_JS = """
return (function(){
  const acc = window.__aicvPpl;
  if (!acc) return {count: 0, fresh: 0, top: 0};
  const s = acc.scroller;
  if (!acc.ids) { acc.ids = new WeakMap(); acc.idSeq = 0; }
  if (acc.round === undefined) acc.round = 0;
  acc.round -= 1;                       // remontee : le prochain lot est plus ancien
  const base = acc.round * 100000;
  let count = 0, fresh = 0;
  const nodes = Array.from(document.querySelectorAll(__USER__ + ',' + __ASSIST__));
  for (let i = 0; i < nodes.length; i++) {
    const node = nodes[i];
    const role = node.matches(__USER__) ? 'user' : 'assistant';
    let root = node;
    let text = (node.textContent || '').replace(/\\s+/g, ' ').trim();
    if (role === 'assistant') {
      const body = node.querySelector("[data-renderer='lm']");
      if (!body) continue;
      root = body;
      text = (body.textContent || '').replace(/\\s+/g, ' ').trim();
    }
    // un tour au contenu visuel seul (image generee, separateur `hr`) n'a pas
    // de texte : ne pas le perdre.
    const visual = root.querySelector('img, hr') !== null;
    if (!text && !visual) continue;
    let nid = acc.ids.get(node);
    if (nid === undefined) { nid = ++acc.idSeq; acc.ids.set(node, nid); }
    const key = role + ':' + nid;
    const html = node.outerHTML;
    count++;
    if (acc.rows[key] === undefined) {
      acc.rows[key] = html;
      acc.roles[key] = role;
      acc.pos[key] = base + i;           // ordre chronologique croissant
      fresh++;
    } else if (html.length > acc.rows[key].length) {
      acc.rows[key] = html;
    }
  }
  return {count: count, fresh: fresh, top: s ? Math.round(s.scrollTop) : 0,
          height: s ? s.scrollHeight : 0};
})();
""".replace("__USER__", json.dumps(_USER_SELECTOR)).replace(
    "__ASSIST__", json.dumps(_ASSISTANT_SELECTOR)
)

#: retourne l'ensemble accumule
_FINAL_THREAD_JS = """
return (function(){
  const acc = window.__aicvPpl;
  if (!acc) return {items: []};
  const items = [];
  for (const key of Object.keys(acc.rows)) {
    items.push({key: key, role: acc.roles[key] || '', html: acc.rows[key],
                pos: acc.pos[key] || 0});
  }
  return {items: items};
})();
"""


class PerplexityService(BaseService):
    name = "perplexity"
    home_url = "https://www.perplexity.ai/"
    #: la home ne montre que le recents ; la library liste tout
    discover_url = "https://www.perplexity.ai/library"

    #: endpoint + hash de la persisted query LibraryRecentThreadsPaginationQuery
    GRAPHQL_ENDPOINT = "/rest/perplexity_ask/graphql"
    GRAPHQL_HASH = "e207cce86b2c9b67fca3ea7d8450d675ec86c0c429b38e42e88f3b027e7c8729"

    sidebar_scroll_selectors = (
        "aside nav",
        "[data-testid='history']",
        "aside div.overflow-y-auto",
        "aside",
    )
    sidebar_ready_selectors = (
        "aside a[href*='/search/']",
        "a[href*='/search/']",
    )
    login_url_parts = ("perplexity.ai/.auth", "vercel", "sign-in", "login")
    login_selectors = (
        "button:has-text('Sign in')",
        "a[href*='login']",
        "input[type='password']",
    )

    def __init__(self, session, config=None):
        super().__init__(session, config)
        self._thread_models: Dict[str, str] = {}

    def build_parser(self) -> PerplexityParser:
        return PerplexityParser()

    def after_sidebar_open(self) -> None:
        """Ferme la banniere de consentement si elle revient."""
        for sel in (
            "button:has-text('Tout autoriser')",
            "button:has-text('Uniquement nécessaires')",
            "[data-testid='consent-dialog'] button",
        ):
            if self.session.click_if_present(sel, timeout_ms=1200):
                self.session.wait_ms(600)
                return

    def conversation_url(self, ref: ConversationRef) -> str:
        return ref.url or f"https://www.perplexity.ai/search/{ref.id}"

    # -- decouverte via API GraphQL ---------------------------------------------

    def discover_via_api(self) -> Optional[List[ConversationRef]]:
        """Liste complete des threads via la pagination GraphQL de la library.

        Retourne None si l'API ne repond pas comme attendu (fallback DOM).
        Pre-requis : la page de decouverte est deja ouverte (par _list_once).
        """
        refs: Dict[str, ConversationRef] = {}
        cursor: Optional[str] = None
        try:
            for page in range(80):  # 25/page : ~14 pages pour 350 threads
                data = self._graphql_page(cursor)
                if data is None:
                    return None
                try:
                    threads = data["data"]["viewer"]["recentGroup"]["threads"]
                except (KeyError, TypeError):
                    log_fields(log, 30, f"{self.name}: structure GraphQL inattendue")
                    return None
                edges = threads.get("edges") or []
                for edge in edges:
                    node = edge.get("node") or {}
                    cid = node.get("entryId") or node.get("slug")
                    if not cid:
                        continue
                    refs[cid] = ConversationRef(
                        service=self.name,
                        id=cid,
                        url=f"https://www.perplexity.ai/search/{cid}",
                        title=(node.get("name") or None),
                    )
                    display = node.get("displayModel") or {}
                    model_id = display.get("modelID") or node.get("modelID")
                    if model_id:
                        self._thread_models[cid] = str(model_id)
                info = threads.get("pageInfo") or {}
                log_fields(
                    log, 20, f"{self.name}: page API {page + 1}",
                    extra={"threads": len(edges), "cumul": len(refs)},
                )
                if not edges or not info.get("hasNextPage"):
                    break
                cursor = info.get("endCursor") or (edges[-1].get("cursor"))
                if not cursor:
                    break
        except Exception as exc:  # noqa: BLE001
            log_fields(log, 30, f"{self.name}: decouverte API echouee ({exc})")
            return None
        return list(refs.values()) if refs else None

    def _graphql_page(self, cursor: Optional[str]):
        payload = {
            "operationName": "LibraryRecentThreadsPaginationQuery",
            "variables": {
                "count": 25, "cursor": cursor, "includeSearchPreview": False,
                "includeTemporary": None, "searchTerm": None, "sortOrder": "NEWEST",
                "sources": None, "statuses": None, "threadTypes": None,
            },
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": self.GRAPHQL_HASH}},
        }
        js = (
            "return fetch(" + json.dumps(self.GRAPHQL_ENDPOINT) + ", "
            "{method: 'POST', headers: {'content-type': 'application/json'}, "
            "body: " + json.dumps(json.dumps(payload)) + "}).then(r => r.text())"
        )
        out = self.session.eval_body(js)
        if not out:
            log_fields(log, 30, f"{self.name}: reponse GraphQL vide")
            return None
        try:
            return json.loads(out)
        except (TypeError, ValueError) as exc:
            log_fields(log, 30, f"{self.name}: GraphQL non-JSON ({exc}): {str(out)[:120]}")
            return None

    def extract_extras(self, ref: ConversationRef) -> Dict[str, Any]:
        extra = super().extract_extras(ref)
        model = self._thread_models.get(ref.id)
        if model:
            extra["model"] = model
        return extra

    # -- collecte incrementale du fil -------------------------------------------

    def scrape_conversation(self, ref: ConversationRef) -> ScrapedPage:
        """Charge le fil complet en remontant le conteneur virtualise.

        Le pipeline commun ne monte qu'une fenetre de messages (~12). On
        accumule les tours en remontant `.scrollable-container`, puis on
        reconstruit un HTML complet avant parsing. On garde le HTML d'origine
        si l'accumulation n'apporte pas plus de messages (securite).
        """
        try:
            page = super().scrape_conversation(ref)
        except ParseError:
            # une conversation supprimee/privee redirige vers l'accueil : inutile
            # de la retenter a chaque passe.
            current = str(self.session.url() or "")
            if ref.id and ref.id not in current:
                raise ConversationUnavailableError(
                    f"{self.name}: conversation inaccessible ({ref.id} -> {current})"
                ) from None
            raise
        merged = self._collect_thread()
        if merged and merged.count("aicv-ppl-msg") > self._message_count(page.html):
            page.html = merged
        return page

    def _collect_thread(self) -> str:
        """Remonte le fil en memorisant les messages, retourne le HTML accumule."""
        scroll_cfg = self.config.get("scroll", {})
        pause_ms = max(250, int(scroll_cfg.get("pause_ms", 700)))
        max_rounds = max(160, int(scroll_cfg.get("max_rounds", 60)) * 4)
        stable_rounds = max(4, int(scroll_cfg.get("stable_rounds", 3)) + 1)

        self.session.eval_body(_RESET_THREAD_JS)
        found = self.session.eval_body(_FIND_SCROLLER_JS)
        if found is None:
            return ""
        # le fil Perplexity se recolle en bas : `scrollTop` programme est annule,
        # on utilise donc de vrais evenements molette (facade scroll_wheel).
        wheel = getattr(self.session, "scroll_wheel", None)
        cx = int(found.get("cx") or 0)
        cy = int(found.get("cy") or 0)
        client = max(200, int(found.get("client") or 600))
        if wheel is None:
            self.session.eval_body(_SCROLL_BOTTOM_JS)
            self.session.wait_ms(pause_ms)
        else:
            # descendre franchement pour charger les tours recents
            for _ in range(8):
                wheel(cx, cy, client)
                self.session.wait_ms(150)
            self.session.wait_ms(pause_ms)

        at_top = False
        stable = 0
        step = max(300, int(client * 0.85))
        for _ in range(max_rounds):
            info = self.session.eval_body(_COLLECT_THREAD_JS) or {}
            fresh = int(info.get("fresh") or 0)
            count = int(info.get("count") or 0)
            at_top = int(info.get("top") or 0) <= 1
            if at_top and fresh == 0 and count > 0:
                stable += 1
                if stable >= stable_rounds:
                    break
            else:
                stable = 0
            if wheel is None:
                if self.session.eval_body(_SCROLL_UP_JS) is None:
                    break
            else:
                wheel(cx, cy, -step)
            self.session.wait_ms(pause_ms)

        data = self.session.eval_body(_FINAL_THREAD_JS) or {}
        return merge_thread_messages(data.get("items") or [])

    @staticmethod
    def _message_count(html: str) -> int:
        """Nombre approximatif de messages montes dans un HTML Perplexity."""
        return html.count("group/user-bubble") + html.count("data-workflow-final-text")
