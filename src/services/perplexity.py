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
import re
from html import escape as html_escape
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
#: tour de progression Pro Search separe (etapes nommees, hors reponse `lm`)
_STEPS_SELECTOR = "div[data-workflow-items]"

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

#: etat du conteneur : a-t-on atteint le bas ?
_AT_BOTTOM_JS = """
return (function(){
  const s = window.__aicvPpl && window.__aicvPpl.scroller;
  if (!s) return null;
  return {top: Math.round(s.scrollTop), height: s.scrollHeight,
          client: s.clientHeight,
          at_bottom: s.scrollTop + s.clientHeight >= s.scrollHeight - 2};
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
  // Pro Search : un tour de progression separe (etapes nommees, ex.
  // « Verification des fichiers disponibles ») precede parfois la reponse dans
  // son propre `data-workflow-items`, hors de `data-workflow-final-text`. La
  // collecte ne gardait que bulles user + reponses : l'etape etait perdue.
  const isSteps = function(el) {
    if (el.matches(__USER__) || el.matches(__ASSIST__)) return false;
    if (el.closest(__ASSIST__)) return false;
    if (el.querySelector("[data-renderer='lm']")) return false;
    if (el.querySelector("div[class~='group/user-bubble']")) return false;
    const titles = Array.from(el.querySelectorAll('[title]'))
        .map(function(t){ return (t.getAttribute('title') || '').trim(); });
    return titles.some(function(t){ return t && !/^recherche termin/i.test(t); });
  };
  const nodes = Array.from(
    document.querySelectorAll(__USER__ + ',' + __ASSIST__ + ',' + __STEPS__)
  ).filter(function(el){ return el.matches(__STEPS__) ? isSteps(el) : true; });
  for (let i = 0; i < nodes.length; i++) {
    const node = nodes[i];
    const role = node.matches(__USER__) ? 'user'
               : (node.matches(__ASSIST__) ? 'assistant' : 'steps');
    let root = node;
    let turn = null;
    let text = (node.textContent || '').replace(/\\s+/g, ' ').trim();
    if (role === 'assistant') {
      const body = node.querySelector("[data-renderer='lm']");
      if (!body) continue;
      root = body;
      text = (body.textContent || '').replace(/\\s+/g, ' ').trim();
    } else if (role === 'user') {
      // 2026 : les pieces jointes (chips `[data-asset-chip]`) sont rendues dans
      // le conteneur de tour, a cote de la bulle `group/user-bubble`. Memoriser
      // le tour entier, sinon elles sont perdues (nom de fichier, miniature).
      turn = node.closest('[data-workflow-entry]');
      if (turn) root = turn;
    }
    // un tour au contenu visuel seul (image generee, separateur `hr`) n'a pas
    // de texte : ne pas le perdre.
    const visual = root.querySelector('img, hr') !== null;
    if (!text && !visual) continue;
    let nid = acc.ids.get(node);
    if (nid === undefined) { nid = ++acc.idSeq; acc.ids.set(node, nid); }
    const key = role + ':' + nid;
    const html = turn ? turn.outerHTML : node.outerHTML;
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
).replace("__STEPS__", json.dumps(_STEPS_SELECTOR))

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

# -- panneau « Sources N » de fil (context pane, hors messages) ---------------
#
# Perplexity agrège les sources du fil dans un panneau replié du context pane
# (bouton « Sources N », sous-catégories « Web » / « Fichiers »). Les cartes ne
# sont montées qu'à l'ouverture : sans clic, `sources` reste vide. Le service
# ouvre donc le panneau et sa catégorie Web pendant la capture et remonte les
# cartes (titre + URL), que le parser répartit ensuite sur les réponses.

#: retrouve le panneau et mémorise son conteneur dans `window.__aicvPplSourcesPane`
_SOURCES_PANEL_JS = """
return (function(){
  const norm = function(e){ return (e.textContent || '').replace(/\\s+/g, ' ').trim(); };
  const btn = Array.from(document.querySelectorAll('button'))
      .find(function(b){ return norm(b).startsWith('Sources'); });
  if (!btn) return null;
  let pane = btn;
  for (let i = 0; i < 12 && pane.parentElement; i++) {
    pane = pane.parentElement;
    if ((pane.className || '').toString().includes('top-headerHeight')) break;
  }
  window.__aicvPplSourcesPane = pane;
  return {expanded: btn.getAttribute('aria-expanded')};
})();
"""

#: ouvre le panneau replié
_EXPAND_SOURCES_JS = """
return (function(){
  const norm = function(e){ return (e.textContent || '').replace(/\\s+/g, ' ').trim(); };
  const btn = Array.from(document.querySelectorAll('button'))
      .find(function(b){ return norm(b).startsWith('Sources'); });
  if (!btn || btn.getAttribute('aria-expanded') !== 'false') return 0;
  btn.click();
  return 1;
})();
"""

#: ouvre la sous-catégorie « Web » (sources web ; « Fichiers » = pièces jointes)
_EXPAND_SOURCES_WEB_JS = """
return (function(){
  const norm = function(e){ return (e.textContent || '').replace(/\\s+/g, ' ').trim(); };
  const pane = window.__aicvPplSourcesPane;
  if (!pane) return 0;
  const web = Array.from(pane.querySelectorAll('button'))
      .find(function(b){ return norm(b).startsWith('Web'); });
  if (!web || web.getAttribute('aria-expanded') !== 'false') return 0;
  web.click();
  return 1;
})();
"""

#: cartes de la catégorie Web : titre (`.text-primary`), domaine, URL
_COLLECT_SOURCES_JS = """
return (function(){
  const norm = function(e){ return (e.textContent || '').replace(/\\s+/g, ' ').trim(); };
  const pane = window.__aicvPplSourcesPane;
  if (!pane) return {count: null, sources: []};
  const web = Array.from(pane.querySelectorAll('button'))
      .find(function(b){ return norm(b).startsWith('Web'); });
  // pas de categorie Web : aucune source web (les « Fichiers » sont des pieces
  // jointes deja rendues dans les tours, les URLs S3 seraient du bruit).
  if (!web) return {count: null, sources: []};
  const scope = web.parentElement || pane;
  let count = null;
  const m = norm(web).match(/(\\d+)/);
  if (m) count = parseInt(m[1], 10);
  const out = [], seen = new Set();
  for (const a of scope.querySelectorAll('a[href^="http"]')) {
    const url = a.href;
    if (seen.has(url)) continue;
    if (url.includes('ppl-ai-file-upload') || url.includes('perplexity.ai')) continue;
    seen.add(url);
    const titleEl = a.querySelector('.text-primary');
    const domainEl = a.querySelector('span.text-xs');
    out.push({url: url,
              title: titleEl ? titleEl.textContent.trim() : '',
              domain: domainEl ? domainEl.textContent.trim() : ''});
  }
  return {count: count, sources: out};
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
        merged_count = merged.count('class="aicv-ppl-msg"')
        base_count = self._message_count(page.html)
        log_fields(
            log, 20, f"{self.name}: thread collecte",
            extra={"monte": base_count, "accumule": merged_count},
        )
        # on prend le HTML accumule des qu'il apporte strictement plus de
        # messages que la fenetre montee (il en contient toujours au moins
        # autant, et il est ordonne) ; `_message_count` compte des noeuds, pas
        # des sous-chaines de classes Tailwind.
        if merged and merged_count > base_count:
            page.html = self._with_base_title(merged, page.html)
        # panneau « Sources N » de fil : ouvert puis agrege hors des tours (il
        # ne figure pas dans le HTML accumule, limite aux messages).
        thread_sources = self._collect_thread_sources()
        if thread_sources:
            page.extra["thread_sources"] = thread_sources
            log_fields(
                log, 20, f"{self.name}: sources de fil collectees",
                extra={"web": len(thread_sources.get("sources") or [])},
            )
        return page

    def _collect_thread_sources(self) -> Optional[Dict[str, Any]]:
        """Ouvre le panneau « Sources N » et remonte les cartes de la categorie Web.

        Sans clic, les cartes ne sont pas rendues (panneau replié) : on ouvre le
        panneau puis la sous-categorie « Web » avant d'extraire titre + URL. Les
        pieces jointes (categorie « Fichiers », URLs S3) sont ignorees : elles
        sont deja rendues dans le texte des tours.
        """
        try:
            panel = self.session.eval_body(_SOURCES_PANEL_JS)
        except Exception:  # noqa: BLE001 - DOM inattendu : pas de sources
            log.debug("perplexity: lecture du panneau Sources impossible", exc_info=True)
            return None
        if not panel:
            return None
        try:
            if panel.get("expanded") == "false":
                if self.session.eval_body(_EXPAND_SOURCES_JS):
                    self.session.wait_ms(900)
            if self.session.eval_body(_EXPAND_SOURCES_WEB_JS):
                self.session.wait_ms(1300)
            data = self.session.eval_body(_COLLECT_SOURCES_JS) or {}
        except Exception:  # noqa: BLE001
            log.debug("perplexity: ouverture du panneau Sources echouee", exc_info=True)
            return None
        sources = [
            card for card in (data.get("sources") or [])
            if isinstance(card, dict) and card.get("url")
        ]
        if not sources:
            return None
        return {"count": data.get("count"), "sources": sources}

    @staticmethod
    def _with_base_title(merged: str, base: str) -> str:
        """Recopie le ``<title>`` de la page dans le HTML accumule.

        Le HTML accumule ne contient que les messages : sans ce titre, le
        parser prendrait le premier ``<h1>`` d'une reponse pour le nom du fil.
        """
        match = re.search(r"<title[^>]*>(.*?)</title>", base, re.S | re.I)
        if not match:
            return merged
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        if not title:
            return merged
        head = "<head><title>%s</title></head>" % html_escape(title)
        return merged.replace("<html><body>", "<html>" + head + "<body>", 1)

    def _collect_thread(self) -> str:
        """Charge tout le fil virtualise : bas detecte, puis remontee accumulee.

        Le fil Perplexity se recolle en bas : `scrollTop` programme est annule,
        on utilise donc de vrais evenements molette (facade ``scroll_wheel``).
        On descend d'abord jusqu'au bas reel (sinon on raterait les derniers
        tours d'un fil long), puis on remonte en memorisant chaque message.
        """
        scroll_cfg = self.config.get("scroll", {})
        pause_ms = max(250, int(scroll_cfg.get("pause_ms", 700)))
        max_rounds = max(160, int(scroll_cfg.get("max_rounds", 60)) * 4)
        stable_rounds = max(4, int(scroll_cfg.get("stable_rounds", 3)) + 1)

        self.session.eval_body(_RESET_THREAD_JS)
        found = self.session.eval_body(_FIND_SCROLLER_JS)
        log_fields(log, 10, f"{self.name}: scroller du fil", extra={"found": found})
        if found is None:
            return ""
        wheel = getattr(self.session, "scroll_wheel", None)
        cx = int(found.get("cx") or 0)
        cy = int(found.get("cy") or 0)
        client = max(200, int(found.get("client") or 600))
        step = max(300, int(client * 0.85))

        # 1) descendre jusqu'au bas reel. On ne memorise rien ici : l'ordre de
        #    collecte n'est fiable que sur une remontee monotone (rangs
        #    decroissants attribues a chaque lot plus ancien).
        self._scroll_to_bottom(
            wheel, cx, cy, step, pause_ms, max_rounds, stable_rounds
        )

        # 2) remonter en memorisant chaque message jusqu'au haut stable.
        at_top = False
        stable = 0
        for rnd in range(max_rounds):
            info = self.session.eval_body(_COLLECT_THREAD_JS) or {}
            fresh = int(info.get("fresh") or 0)
            count = int(info.get("count") or 0)
            at_top = int(info.get("top") or 0) <= 1
            log_fields(
                log, 10, f"{self.name}: remontee {rnd}",
                extra={"top": info.get("top"), "height": info.get("height"),
                       "count": count, "fresh": fresh, "at_top": at_top},
            )
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
        log_fields(
            log, 10, f"{self.name}: fin de collecte",
            extra={"items": len(data.get("items") or [])},
        )
        return merge_thread_messages(data.get("items") or [])

    def _scroll_to_bottom(
        self, wheel, cx, cy, step, pause_ms, max_rounds, stable_rounds
    ) -> None:
        """Descend le fil jusqu'a un bas stable (hauteur et position figees)."""
        last_height = -1
        stable = 0
        for _ in range(max_rounds):
            state = self.session.eval_body(_AT_BOTTOM_JS) or {}
            height = int(state.get("height") or 0)
            if bool(state.get("at_bottom")) and height == last_height:
                stable += 1
                if stable >= stable_rounds:
                    return
            else:
                stable = 0
            last_height = height
            if wheel is None:
                if self.session.eval_body(_SCROLL_BOTTOM_JS) is None:
                    return
            else:
                wheel(cx, cy, step)
            self.session.wait_ms(pause_ms)

    @staticmethod
    def _message_count(html: str) -> int:
        """Nombre de messages montes dans un HTML Perplexity.

        On compte des **noeuds** via BeautifulSoup : un simple
        ``html.count("data-workflow-final-text")`` etait fausse par les classes
        Tailwind arbitraires des ancetres (``[&[data-workflow-final-text]+div]``),
        qui gonflaient le compte de la fenetre brute (~30 pour 9 reponses) et
        faisaient jeter le HTML accumule riche.
        """
        if not html:
            return 0
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        return len(
            soup.select(
                "div[class~='group/user-bubble'], div[data-workflow-final-text]"
            )
        )
