"""Scraper Perplexity (perplexity.ai).

Chemin **API** (defaut, JSON-only) : la decouverte et le fil passent par les
endpoints REST/GraphQL (`/rest/perplexity_ask/graphql`, `/rest/thread/<id>`)
avec les cookies du profil ; le payload brut est archive dans
`exports/perplexity/raw/<slug>.json` (artefact de re-parse), aucun HTML n'est
genere. Le navigateur n'est ouvert qu'une fois, pour recuperer les cookies.

Decouverte : l'API GraphQL de la library (persisted query) liste TOUT
(la liste DOM est virtualisee et la sidebar ne montre que le recent).

Repli **DOM** (opt-in `services.perplexity.dom: true`) : collecteur DOM
incrementiel. Le fil est virtualise (`min-height` reserves) : `scrape_conversation`
remonte le conteneur `.scrollable-container` en accumulant les messages (HTML
le plus long gagne) avant de reconstruire un HTML complet a parser.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from html import escape as html_escape
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from ..parsers.base import ParseError
from ..parsers.perplexity import PerplexityParser, merge_thread_messages
from ..schema import ConversationRef
from ..utils.logging import get_logger, log_fields
from .base import (
    BaseService,
    ConversationUnavailableError,
    ScrapedPage,
    ServiceNotLoggedIn,
)

log = get_logger("services")

# -- chemin API (REST + GraphQL, cookies du profil) --------------------------

#: base de l'API Perplexity
API_BASE = "https://www.perplexity.ai"
#: contenu d'un fil (`entries`, `thread_metadata`, pagination par curseur)
API_THREAD_URL = API_BASE + "/rest/thread/{cid}"
#: GraphQL (persisted queries) de la library et de la sidebar
API_GRAPHQL_URL = API_BASE + "/rest/perplexity_ask/graphql"
#: version applicative envoyee par l'app (`x-app-apiversion`)
API_VERSION = "2.18"
#: entrees demandees par page de fil (l'API en accepte >= 500 ; 100 reste doux)
API_PAGE_SIZE = 100
#: garde-fou de pagination du fil
API_MAX_PAGES = 50
#: 429 : le forfait gratuit limite les rafales ; on attend et on reessaie
API_RETRY_ATTEMPTS = 6
#: attente de base sur 429 (secondes), doublee a chaque tentative
API_RETRY_WAIT_S = 30
#: plafond de l'attente sur 429 (secondes)
API_RETRY_WAIT_MAX_S = 300

#: capacites de blocs demandees au fil (comme l'app : rend le payload complet)
API_BLOCK_USE_CASES = (
    "answer_modes", "media_items", "inline_entity_cards", "place_widgets",
    "finance_widgets", "news_widgets", "shopping_widgets",
    "search_result_widgets", "inline_images", "inline_assets",
    "placeholder_cards", "diff_blocks", "entity_group_v2", "refinement_filters",
    "canvas_mode", "maps_preview", "answer_tabs", "preserve_latex",
    "in_context_suggestions", "pending_followups", "inline_claims",
    "unified_assets", "workflow_steps", "workflow_widgets",
    "navigation_results", "background_agents",
)


class PerplexityHttpError(RuntimeError):
    """Reponse HTTP non exploitable de l'API Perplexity (statut conserve)."""

    def __init__(self, status: int, url: str):
        super().__init__(f"perplexity: HTTP {status} sur {url}")
        self.status = status
        self.url = url


def _retry_after_s(headers: Any) -> Optional[float]:
    """`Retry-After` numerique d'une reponse HTTP (None sinon)."""
    try:
        value = (headers or {}).get("Retry-After")
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


class _PerplexityHttpClient:
    """Requetes authentifiees (cookies du profil) sur l'API Perplexity.

    Les cookies (httpOnly inclus) viennent de la session navigateur ouverte une
    fois ; l'empreinte (User-Agent, langue) est reprise du profil pour rester
    coherent avec eux. Sur 429, on attend (`Retry-After` puis exponentiel) et on
    reessaie, comme le client ChatGPT.
    """

    def __init__(
        self,
        cookie_header: str,
        *,
        user_agent: Optional[str] = None,
        accept_language: Optional[str] = None,
        timeout_s: int = 45,
    ):
        self.cookie_header = cookie_header
        self.user_agent = user_agent or (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        self.accept_language = accept_language or "en-US,en;q=0.9"
        self.timeout_s = timeout_s

    def _headers(self, accept: str, content_type: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Cookie": self.cookie_header,
            "Accept": accept,
            "Accept-Language": self.accept_language,
            "Referer": API_BASE + "/",
            "x-app-apiversion": API_VERSION,
            "x-app-apiclient": "default",
        }
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _wait_429(self, attempt: int, headers: Any, url: str) -> bool:
        """Attend (backoff) avant un nouvel essai apres un 429 ; False sinon."""
        if attempt + 1 >= API_RETRY_ATTEMPTS:
            return False
        wait_s = _retry_after_s(headers) or min(
            API_RETRY_WAIT_MAX_S, API_RETRY_WAIT_S * (2**attempt)
        )
        log.warning(
            "perplexity: HTTP 429, nouvel essai dans %.0fs (%s/%s) %s",
            wait_s, attempt + 1, API_RETRY_ATTEMPTS, url,
        )
        time.sleep(wait_s)
        return True

    def _request(
        self, url: str, *, data: Optional[bytes] = None, accept: str = "application/json, text/plain, */*",
    ) -> bytes:
        """Requete GET/POST authentifiee ; 429 -> backoff ; autres erreurs levees."""
        for attempt in range(API_RETRY_ATTEMPTS):
            request = urllib.request.Request(
                url,
                data=data,
                method="POST" if data is not None else "GET",
                headers=self._headers(
                    accept, "application/json" if data is not None else None
                ),
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and self._wait_429(attempt, exc.headers, url):
                    continue
                raise PerplexityHttpError(exc.code, url) from exc
            except urllib.error.URLError as exc:
                raise RuntimeError(
                    f"perplexity: requete echouee ({url}): {exc}"
                ) from exc
        raise RuntimeError(f"perplexity: 429 persistant sur {url}")

    def get_json(self, url: str) -> Any:
        raw = self._request(url)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"perplexity: reponse non-JSON sur {url}") from exc

    def post_json(self, url: str, payload: Dict[str, Any]) -> Any:
        raw = self._request(url, data=json.dumps(payload).encode("utf-8"))
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"perplexity: reponse non-JSON sur {url}") from exc

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

#: ouvre les en-tetes d'etapes replies (UI 2026). Le nom de l'etape de workflow
#: (« Verification des fichiers disponibles ») n'est monte qu'apres un clic sur
#: le chevron du header « Recherche terminee » (`aria-expanded=false`) : sans
#: ouverture, le parser ne lit que le libelle de statut et `reasoning` est perdu.
_EXPAND_STEPS_JS = """
return (function(){
  const btns = Array.from(document.querySelectorAll(
      "[class*='step-header'] button[aria-expanded='false']"));
  let n = 0;
  for (const b of btns) {
    try { b.click(); n++; } catch (e) { /* bouton detache : ignore */ }
  }
  return n;
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
        # UI 2026 : plus de <aside> ; la liste est dans `group/sidebar-*`.
        "[class*='sidebar']",
        "aside nav",
        "[data-testid='history']",
        "aside div.overflow-y-auto",
        "aside",
    )
    sidebar_ready_selectors = (
        "[class*='sidebar'] a[href*='/search/']",
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
        self._thread_updated: Dict[str, str] = {}
        #: client HTTP de l'API (construit au premier appel en mode API)
        self._http: Optional[_PerplexityHttpClient] = None
        #: payload brut de la derniere conversation exportee en mode API
        self._raw_payload: Optional[Dict[str, Any]] = None

    def build_parser(self) -> PerplexityParser:
        return PerplexityParser()

    # -- chemin API (defaut) ----------------------------------------------------

    def _api_config(self) -> Dict[str, Any]:
        """Config du service (`services.perplexity` de la config globale)."""
        services = (self.config or {}).get("services") or {}
        return services.get(self.name) or {}

    @property
    def api_enabled(self) -> bool:
        """True si le chemin API est actif (defaut), False pour le repli DOM.

        Le DOM est le repli **opt-in** : `services.perplexity.dom: true` (ou
        `api: false`) force l'ancien chemin navigateur.
        """
        cfg = self._api_config()
        if cfg.get("dom"):
            return False
        return bool(cfg.get("api", True))

    def _api_client(self) -> _PerplexityHttpClient:
        """Client HTTP de l'API, cookies mints une fois via la session navigateur.

        `session.goto(home)` ouvre la home connectee (botasaurus passe
        Cloudflare) puis les cookies du contexte persistant sont repris ; tout
        le reste se fait en HTTP pur, avec l'empreinte (UA/langue) du profil.
        """
        if self._http is not None:
            return self._http
        self.session.goto(self.home_url)
        self.session.wait_ms(1500)
        cookies = self.session.cookies() or []
        cookie_header = "; ".join(
            f"{c.get('name')}={c.get('value')}"
            for c in cookies
            if c.get("name")
        )
        if not cookie_header:
            raise ServiceNotLoggedIn(
                "perplexity: aucun cookie de session -> `run.py --login perplexity`"
            )
        fingerprint = getattr(self.session, "fingerprint", None) or {}
        user_agent = str(fingerprint.get("user_agent") or "") or None
        lang = str(fingerprint.get("lang") or "")
        accept_language = f"{lang},{lang.split('-')[0]};q=0.9" if lang else None
        self._http = _PerplexityHttpClient(
            cookie_header,
            user_agent=user_agent,
            accept_language=accept_language,
        )
        log.debug("perplexity: client API pret (cookies=%s)", len(cookies))
        return self._http

    def _list_conversations_api(
        self, limit: Optional[int] = None
    ) -> List[ConversationRef]:
        """Decouverte paginee par GraphQL (`LibraryRecentThreadsPaginationQuery`)."""
        client = self._api_client()
        refs: Dict[str, ConversationRef] = {}
        cursor: Optional[str] = None
        for page in range(80):  # 25/page : ~16 pages pour 400 threads
            payload = {
                "operationName": "LibraryRecentThreadsPaginationQuery",
                "variables": {
                    "count": 25, "cursor": cursor, "includeSearchPreview": False,
                    "includeTemporary": None, "searchTerm": None,
                    "sortOrder": "NEWEST", "sources": None, "statuses": None,
                    "threadTypes": None,
                },
                "extensions": {
                    "persistedQuery": {"version": 1, "sha256Hash": self.GRAPHQL_HASH}
                },
            }
            try:
                data = client.post_json(API_GRAPHQL_URL, payload)
            except PerplexityHttpError as exc:
                if exc.status in (401, 403):
                    raise ServiceNotLoggedIn(
                        f"perplexity: non authentifie (HTTP {exc.status}) -> "
                        "`run.py --login perplexity`"
                    ) from exc
                raise
            try:
                threads = data["data"]["viewer"]["recentGroup"]["threads"]
            except (KeyError, TypeError) as exc:
                raise RuntimeError(
                    "perplexity: structure GraphQL inattendue (decouverte)"
                ) from exc
            edges = threads.get("edges") or []
            for edge in edges:
                node = edge.get("node") or {}
                cid = node.get("entryId") or node.get("slug")
                if not cid:
                    continue
                refs[cid] = ConversationRef(
                    service=self.name,
                    id=cid,
                    url=f"{API_BASE}/search/{cid}",
                    title=(node.get("name") or node.get("title") or None),
                )
                display = node.get("displayModel") or {}
                model_id = display.get("modelID") or node.get("modelID")
                if model_id:
                    self._thread_models[cid] = str(model_id)
                updated = node.get("updatedAt")
                if updated:
                    self._thread_updated[cid] = str(updated)
                if limit and len(refs) >= limit:
                    break
            log_fields(
                log, 20, f"{self.name}: page API {page + 1}",
                extra={"threads": len(edges), "cumul": len(refs)},
            )
            info = threads.get("pageInfo") or {}
            if limit and len(refs) >= limit:
                break
            if not edges or str(info.get("hasNextPage")).lower() != "true":
                break
            cursor = info.get("endCursor") or (edges[-1].get("cursor"))
            if not cursor:
                break
        if not refs:
            log_fields(
                log, 30,
                f"{self.name}: aucune conversation decouverte via l'API "
                f"(compte vide ou session anonyme ?)",
            )
        return list(refs.values())

    @staticmethod
    def _thread_url(cid: str, *, limit: int, cursor: Optional[str] = None) -> str:
        """URL du fil : `from_first=true` = ordre chronologique + pagination avant."""
        params = [
            ("with_parent_info", "true"),
            ("with_schematized_response", "true"),
            ("version", API_VERSION),
            ("source", "default"),
            ("limit", str(limit)),
            ("offset", "0"),
            ("from_first", "true"),
            ("with_first_entry", "true"),
            ("with_latest_entry", "false"),
        ]
        if cursor:
            params.append(("cursor", str(cursor)))
        query = urlencode(params)
        query += "".join(
            f"&supported_block_use_cases={name}" for name in API_BLOCK_USE_CASES
        )
        return f"{API_THREAD_URL.format(cid=cid)}?{query}"

    def _fetch_thread_api(
        self, client: _PerplexityHttpClient, conversation_id: str
    ) -> Dict[str, Any]:
        """Payload du fil complet (pagination par curseur tant que `has_next_page`).

        `has_next_page`/`next_cursor` sont serialises en chaines par l'API
        (`"True"`, JSON de curseur) : on les normalise. Les entrees sont
        dedupliquees par uuid (une page de bord peut recouvrir la precedente).
        """
        entries: List[Dict[str, Any]] = []
        seen: set = set()
        payload: Dict[str, Any] = {}
        cursor: Optional[str] = None
        for page in range(API_MAX_PAGES):
            try:
                payload = client.get_json(
                    self._thread_url(conversation_id, limit=API_PAGE_SIZE, cursor=cursor)
                )
            except PerplexityHttpError as exc:
                if exc.status == 404:
                    raise ConversationUnavailableError(
                        f"{self.name}: conversation inaccessible ({conversation_id}, HTTP 404)"
                    ) from exc
                if exc.status in (401, 403):
                    raise ServiceNotLoggedIn(
                        f"{self.name}: non authentifie (HTTP {exc.status}) -> "
                        "`run.py --login perplexity`"
                    ) from exc
                raise
            if not isinstance(payload, dict):
                raise RuntimeError(
                    f"{self.name}: payload de fil inattendu ({conversation_id})"
                )
            batch = payload.get("entries") or []
            for entry in batch:
                if not isinstance(entry, dict):
                    continue
                uid = entry.get("uuid")
                if uid and uid in seen:
                    continue
                if uid:
                    seen.add(uid)
                entries.append(entry)
            log_fields(
                log, 20, f"{self.name}: page de fil {page + 1}",
                extra={"entrees": len(batch), "cumul": len(entries)},
            )
            if str(payload.get("has_next_page")).lower() != "true":
                break
            next_cursor = payload.get("next_cursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = str(next_cursor)
        merged = dict(payload)
        merged["entries"] = entries
        return merged

    def _export_api(self, ref: ConversationRef) -> Tuple[Any, None]:
        """Fil via l'API -> Conversation ; payload brut memorise (pas de HTML)."""
        client = self._api_client()
        data = self._fetch_thread_api(client, ref.id)
        self.record_api_shape(data)
        conv = self.parser.parse("", conversation_id=ref.id, extra={"api": data})
        if not conv.url:
            conv.url = self.conversation_url(ref)
        self._raw_payload = data
        return conv, None

    def take_raw_payload(self) -> Optional[Dict[str, Any]]:
        """Payload API de la derniere conversation exportee (consomme)."""
        payload, self._raw_payload = self._raw_payload, None
        return payload

    def capture_message_screenshots(self, conversation, out_dir) -> int:
        """Pas de screenshots en mode API (le DOM n'est pas rendu)."""
        if self.api_enabled:
            return 0
        return super().capture_message_screenshots(conversation, out_dir)

    def list_conversations(self, limit: Optional[int] = None) -> List[ConversationRef]:
        if self.api_enabled:
            return self._list_conversations_api(limit=limit)
        return super().list_conversations(limit=limit)

    def export_conversation_with_html(self, ref: ConversationRef) -> tuple:
        """API (defaut) : payload JSON -> Conversation ; pas de HTML."""
        if not self.api_enabled:
            return super().export_conversation_with_html(ref)
        return self._export_api(ref)

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
                    # date de derniere mise a jour : sert de reference au parser
                    # pour deduire l'annee des horodatages visibles (sans annee).
                    updated = node.get("updatedAt")
                    if updated:
                        self._thread_updated[cid] = str(updated)
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
        updated = self._thread_updated.get(ref.id)
        if updated:
            # reference de datation : les horodatages visibles n'ont pas l'annee.
            extra["thread_updated_at"] = updated
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

        # 1bis) ouvrir les en-tetes d'etapes replies : le nom de l'etape n'est
        #       monte qu'apres un clic, et la revelation est asynchrone (prise en
        #       compte a la collecte suivante).
        opened = self.session.eval_body(_EXPAND_STEPS_JS)
        log_fields(log, 10, f"{self.name}: etapes de workflow ouvertes",
                   extra={"opened": opened})
        if opened:
            self.session.wait_ms(pause_ms)

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
            # ouvrir les etapes des tours fraichement montes (les plus anciens) :
            # le nom sera repris a la collecte du tour suivant, avant demonte.
            self.session.eval_body(_EXPAND_STEPS_JS)
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
