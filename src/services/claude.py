"""Scraper Claude / Anthropic (claude.ai).

Chemin **API** (defaut) : les lectures passent par l'API REST avec les cookies
du profil persistant (mint une fois via la session navigateur, comme le client
ChatGPT). La liste vient de
`/api/organizations/<org>/chat_conversations_v2` et le fil de
`/api/organizations/<org>/chat_conversations/<id>?tree=True&rendering_mode=
messages&render_all_tools=true` ; l'org id est lu via `/api/organizations`.
Aucun HTML n'est genere : le payload brut de l'API est archive par
l'orchestrateur dans `exports/claude/raw/<slug>.json` (artefact de re-parse) et
il n'y a pas de screenshots. Le chemin API est desactivable
(`services.claude.api: false`) : le DOM reste alors le repli (transcript
virtualise accumule par scroll).

Chemin **DOM** (repli) : Claude virtualise le transcript ; le DOM ne rend qu'une
fenetre de messages autour de la position de scroll (les autres sont remplaces
par un spacer). Le service accumule les rangees `[data-testid='transcript-row']`
en remontant le fil, puis reconstruit un HTML complet avant de le parser.

Avant chaque capture DOM, les panneaux de raisonnement/thinking repliables sont
deplies (`[data-cds-row-toggle]`) : Claude n'insere le raisonnement integral
dans le DOM (`[data-cds-row-panel]`) qu'une fois le panneau ouvert.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..http_client import CookieAuthError, DEFAULT_USER_AGENT, load_cookie_header
from ..parsers.claude import ClaudeParser, merge_transcript_rows
from ..schema import Conversation, ConversationRef
from ..utils.logging import get_logger
from .base import (
    BaseService,
    ConversationUnavailableError,
    EmptyConversationError,
    RateLimitedError,
    ScrapedPage,
    ServiceNotLoggedIn,
)

log = get_logger("services.claude")

#: base de l'API claude.ai
API_BASE = "https://claude.ai"
API_ORGANIZATIONS_URL = API_BASE + "/api/organizations"
API_ACCOUNT_URL = API_BASE + "/api/account"
#: liste paginee des conversations (v2 : data + has_more)
API_LIST_URL = API_BASE + "/api/organizations/{org}/chat_conversations_v2"
#: fil complet d'une conversation (arbre : versions natives incluses)
API_CONVERSATION_URL = API_BASE + "/api/organizations/{org}/chat_conversations/{cid}"
API_CONVERSATION_QUERY = "?tree=True&rendering_mode=messages&render_all_tools=true"
#: taille de page de la decouverte
API_PAGE_SIZE = 30
#: nombre maxi de pages parcourues (garde-fou)
API_MAX_PAGES = 200
#: 429 : limite de rafale de l'API (comme le backend ChatGPT) ; on attend et
#: on reessaie (`Retry-After` sinon exponentiel plafonne).
API_RETRY_ATTEMPTS = 8
#: attente de base sur 429 (secondes), doublee a chaque tentative
API_RETRY_WAIT_S = 45
#: plafond de l'attente sur 429 (secondes)
API_RETRY_WAIT_MAX_S = 300

#: rangee de message du transcript 2026 (une par message, virtualisee)
TURN_SELECTOR = "[data-testid='transcript-row']"


def _retry_after_s(headers: Any) -> Optional[float]:
    """`Retry-After` numerique d'une reponse HTTP (None sinon)."""
    try:
        value = (headers or {}).get("Retry-After")
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


class _ClaudeHttpClient:
    """Requetes GET authentifiees sur l'API claude.ai (cookies du profil).

    Les cookies (httpOnly inclus) viennent du contexte persistant ; l'UA et la
    langue doivent coller au profil (les cookies sont lies a l'empreinte du
    navigateur). Aucun navigateur n'est utilise pour les lectures.
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
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.accept_language = accept_language or "en-US,en;q=0.9"
        self.timeout_s = timeout_s
        #: organisation courante (resolue au premier appel)
        self.org_id: Optional[str] = None

    def _headers(self, accept: str) -> Dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Cookie": self.cookie_header,
            "Accept": accept,
            "Accept-Language": self.accept_language,
            "Referer": API_BASE + "/",
            "anthropic-client-platform": "web_claude_ai",
            "anthropic-client-version": "1.0.0",
        }

    def _wait_429(self, attempt: int, headers: Any, url: str) -> bool:
        """Attend (backoff) avant un nouvel essai apres un 429 ; False sinon.

        `Retry-After` est respecte mais plafonne : un en-tete de plusieurs
        heures ne doit pas endormir le run.
        """
        if attempt + 1 >= API_RETRY_ATTEMPTS:
            return False
        wait_s = max(
            0.0,
            min(
                API_RETRY_WAIT_MAX_S,
                _retry_after_s(headers) or API_RETRY_WAIT_S * (2**attempt),
            ),
        )
        log.warning(
            "claude: HTTP 429, nouvel essai dans %.0fs (%s/%s) %s",
            wait_s, attempt + 1, API_RETRY_ATTEMPTS, url,
        )
        time.sleep(wait_s)
        return True

    def get(
        self, url: str, accept: str = "application/json, text/plain, */*"
    ) -> Tuple[int, bytes]:
        """GET authentifie ; sur 429, attend et reessaie (limite de rafale)."""
        for attempt in range(API_RETRY_ATTEMPTS):
            request = urllib.request.Request(url, headers=self._headers(accept))
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and self._wait_429(attempt, exc.headers, url):
                    continue
                return exc.code, exc.read() or b""
            except urllib.error.URLError as exc:
                raise RuntimeError(f"claude: requete echouee ({url}): {exc}") from exc
        raise RateLimitedError(f"claude: 429 persistant sur {url}")

    def get_json(self, url: str) -> Any:
        status, raw = self.get(url)
        if status in (401, 403):
            raise ServiceNotLoggedIn(
                f"claude: non authentifie (HTTP {status}) -> `run.py --login claude`"
            )
        if status == 429:
            # limite de rafale epuisee : arreter le service proprement
            raise RateLimitedError(f"claude: 429 persistant sur {url}")
        if status != 200:
            raise RuntimeError(f"claude: HTTP {status} sur {url}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"claude: reponse non-JSON sur {url}") from exc

    def fetch(
        self, url: str, accept: str = "*/*"
    ) -> Optional[Tuple[bytes, Optional[str]]]:
        """(octets, content-type) d'une URL authentifiee ; None si echec.

        Utilise pour telecharger les pieces jointes (images `preview`, PDF
        `document_pdf`) : l'API exige les cookies du profil. Sur 429, attend et
        reessaie comme `get`.
        """
        for attempt in range(API_RETRY_ATTEMPTS):
            request = urllib.request.Request(url, headers=self._headers(accept))
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    ctype = (response.headers.get("Content-Type") or "").split(";")[0]
                    return response.read(), (ctype.strip() or None)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and self._wait_429(attempt, exc.headers, url):
                    continue
                log.debug("claude: fetch asset echoue (%s): HTTP %s", url, exc.code)
                return None
            except (urllib.error.URLError, OSError) as exc:
                log.debug("claude: fetch asset echoue (%s): %s", url, exc)
                return None
        return None


class _ApiFetchSession:
    """Session facade : assets claude.ai telecharges par le client HTTP.

    `download_service_images`/`download_service_files` passent par
    `session.fetch`. En mode API, les URL d'assets (`/api/<org>/files/...`)
    sont telechargees avec les cookies du profil par le client HTTP, sans
    navigateur ; tout le reste (harvest des cookies, repli) est delegue a la
    session d'origine.
    """

    def __init__(self, service: "ClaudeService", inner: Any):
        self._service = service
        self._inner = inner

    def fetch(self, url: str):
        if url.startswith(API_BASE + "/"):
            return self._service.download_api_asset(url)
        inner_fetch = getattr(self._inner, "fetch", None)
        if callable(inner_fetch):
            return inner_fetch(url)
        return None

    def __getattr__(self, name: str) -> Any:
        inner = object.__getattribute__(self, "_inner")
        return getattr(inner, name)


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

#: va en haut du fil : charge les chunks precedents et corrige l'indexation.
#: Claude monte d'abord le dernier chunk du transcript avec des `data-index`
#: locaux (0..N) et un `aria-setsize` tronque ; atteindre le haut charge les
#: chunks precedents et fait passer aux index globaux. Sans cette etape la
#: collecte s'arrete au premier chunk (messages recents) et les index entrent
#: en collision avec ceux des messages plus anciens.
_SCROLL_TOP_JS = """
return (function(){
  const s = window.__aicvClaudeScroller;
  if (!s) return null;
  s.scrollTop = 0;
  return 0;
})();
"""

#: taille reelle du fil (aria-setsize) sans rien accumuler
_PEEK_SETSIZE_JS = """
return (function(){
  const holder = document.querySelector(__SETSIZE__);
  if (holder) return parseInt(holder.getAttribute('aria-setsize'), 10) || 0;
  return 0;
})();
""".replace(
    "__SETSIZE__",
    json.dumps("[data-testid='transcript-row'] [aria-setsize]"),
)

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
    #: chemin API actif par defaut (`services.claude.api: false` -> DOM)
    api_default = True

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

    def __init__(self, session, config: Optional[Dict[str, Any]] = None):
        super().__init__(session, config)
        #: client HTTP de l'API (construit au premier appel en mode API)
        self._http: Optional[_ClaudeHttpClient] = None
        if self.api_enabled:
            # les assets du markdown (`/api/<org>/files/...`) passent par le
            # client HTTP authentifie, pas par le navigateur
            self.session = _ApiFetchSession(self, session)

    def build_parser(self) -> ClaudeParser:
        return ClaudeParser()

    # -- chemin API (defaut) ---------------------------------------------------

    def _api_client(self) -> _ClaudeHttpClient:
        """Client HTTP de l'API, cookies mint une fois via la session navigateur.

        `session.goto` ouvre la home connectee (profil persistant) et
        `session.cookies()` fournit les cookies (httpOnly inclus) ; l'org id
        vient de `/api/organizations`. Tout le reste est en HTTP pur.
        """
        if self._http is not None:
            return self._http
        cookie_header = ""
        cookies_dir = Path((self.config or {}).get("cookies_dir") or "cookies")
        try:
            # cookies deja captures (cookies/claude.json) : aucun navigateur requis
            cookie_header = load_cookie_header(cookies_dir, self.name)
        except CookieAuthError:
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
                "claude: aucun cookie de session -> `run.py --login claude`"
            )
        # l'UA (et la langue) doivent coller au profil : les cookies sont lies
        # a l'empreinte du navigateur (un UA divergent -> challenge/403).
        fingerprint = getattr(self.session, "fingerprint", None) or {}
        user_agent = str(fingerprint.get("user_agent") or "") or None
        lang = str(fingerprint.get("lang") or "")
        accept_language = f"{lang},{lang.split('-')[0]};q=0.9" if lang else None
        client = _ClaudeHttpClient(
            cookie_header,
            user_agent=user_agent,
            accept_language=accept_language,
        )
        client.org_id = self._resolve_org_id(client)
        self._http = client
        log.debug("claude: client API pret (org=%s)", bool(client.org_id))
        return self._http

    def _resolve_org_id(self, client: _ClaudeHttpClient) -> str:
        """Org id de l'API (`/api/organizations`, repli `/api/account`)."""
        try:
            data = client.get_json(API_ORGANIZATIONS_URL)
        except RateLimitedError:
            # ne pas rejouer le backoff sur le repli : le service doit s'arreter
            raise
        except RuntimeError as exc:
            log.debug("claude: /api/organizations indisponible: %s", exc)
            data = None
        if isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict) and entry.get("uuid"):
                    return str(entry["uuid"])
        account = client.get_json(API_ACCOUNT_URL)
        for membership in account.get("memberships") or []:
            org = (membership or {}).get("organization") or {}
            if org.get("uuid"):
                return str(org["uuid"])
        raise ServiceNotLoggedIn(
            "claude: organisation introuvable (session invalide ?) -> "
            "`run.py --login claude`"
        )

    def download_api_asset(
        self, url: str
    ) -> Optional[Tuple[bytes, Optional[str]]]:
        """Telecharge un asset claude.ai (`/api/<org>/files/...`) avec cookies."""
        try:
            return self._api_client().fetch(url)
        except ServiceNotLoggedIn:
            raise
        except RuntimeError as exc:
            log.debug("claude: asset inaccessible (%s): %s", url, exc)
            return None

    def _list_conversations_api(
        self, limit: Optional[int] = None
    ) -> List[ConversationRef]:
        """Decouverte paginee par API (`offset += limit` tant que `has_more`)."""
        client = self._api_client()
        refs: Dict[str, ConversationRef] = {}
        offset = 0
        for _ in range(API_MAX_PAGES):
            url = (
                f"{API_LIST_URL.format(org=client.org_id)}"
                f"?limit={API_PAGE_SIZE}&offset={offset}"
                f"&archived=false&consistency=eventual"
            )
            data = client.get_json(url)
            items = data.get("data") or []
            for item in items:
                cid = item.get("uuid")
                if not cid:
                    continue
                refs[str(cid)] = ConversationRef(
                    service=self.name,
                    id=str(cid),
                    url=f"{API_BASE}/chat/{cid}",
                    title=item.get("name") or None,
                    raw={
                        "started_at": item.get("created_at"),
                        "last_message_at": item.get("updated_at"),
                    },
                )
                if limit and len(refs) >= limit:
                    break
            if limit and len(refs) >= limit:
                break
            if not data.get("has_more") or not items:
                break
            offset += API_PAGE_SIZE
        log.debug("claude api: %s conversations decouvertes", len(refs))
        return list(refs.values())

    def _fetch_conversation_api(
        self, client: _ClaudeHttpClient, conversation_id: str
    ) -> Dict[str, Any]:
        """Payload de conversation (arbre complet, versions natives incluses)."""
        url = API_CONVERSATION_URL.format(
            org=client.org_id, cid=conversation_id
        ) + API_CONVERSATION_QUERY
        status, raw = client.get(url)
        if status == 404:
            raise ConversationUnavailableError(
                f"claude: conversation {conversation_id} indisponible (HTTP 404)"
            )
        if status in (401, 403):
            raise ServiceNotLoggedIn(
                f"claude: non authentifie (HTTP {status}) -> `run.py --login claude`"
            )
        if status == 429:
            raise RateLimitedError(f"claude: 429 persistant sur {url}")
        if status != 200:
            raise RuntimeError(f"claude: HTTP {status} sur {url}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"claude: reponse non-JSON sur {url}") from exc

    # -- pipeline commun ---------------------------------------------------------

    def conversation_url(self, ref: ConversationRef) -> str:
        return ref.url or f"{API_BASE}/chat/{ref.id}"

    def extract_extras(self, ref: ConversationRef) -> Dict[str, Any]:
        extra: Dict[str, Any] = {}
        if ref.title:
            extra["title_hint"] = ref.title
        # claude.ai met a jour document.title avec le titre du chat
        page_title = self.session.evaluate("() => document.title")
        if page_title:
            extra["title_hint"] = extra.get("title_hint") or str(page_title).strip()
        return extra

    def list_conversations(self, limit: Optional[int] = None) -> List[ConversationRef]:
        if self.api_enabled:
            return self._list_conversations_api(limit=limit)
        return super().list_conversations(limit=limit)

    def export_conversation_with_html(self, ref: ConversationRef) -> tuple:
        """API (defaut) : payload JSON -> Conversation ; pas de HTML.

        Le payload brut de l'API est memorise (`take_raw_payload`) pour que
        l'orchestrateur l'archive dans `exports/claude/raw/<slug>.json` : il
        remplace le HTML comme artefact de re-parse.
        """
        if not self.api_enabled:
            return super().export_conversation_with_html(ref)
        client = self._api_client()
        data = self._fetch_conversation_api(client, ref.id)
        self.record_api_shape(data)
        if not data.get("chat_messages"):
            raise EmptyConversationError(
                f"claude: conversation sans message chargeable ({ref.id})"
            )
        conv: Conversation = self.parser.parse(
            "",
            conversation_id=ref.id,
            extra={"api": data, "title_hint": ref.title},
        )
        if not conv.url:
            conv.url = self.conversation_url(ref)
        self._raw_payload = data
        return conv, None

    # -- chemin DOM (repli opt-in) ---------------------------------------------

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
        # charger tout le fil (index globaux) avant de collecter : sinon seul le
        # dernier chunk est vu et ses index locaux collisionnent avec les
        # messages plus anciens.
        self._load_full_transcript(pause_ms, max_rounds)
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

    def _load_full_transcript(self, pause_ms: int, max_rounds: int) -> None:
        """Charge tout le fil avant collecte (index globaux).

        Claude monte d'abord le dernier chunk du transcript avec des
        ``data-index`` locaux et un ``aria-setsize`` tronque (taille du chunk).
        Atteindre le haut du fil charge les chunks precedents et corrige
        l'indexation ; on repete tant que ``aria-setsize`` progresse, puis on
        s'arrete une fois stabilise. Sans cette etape, la collecte s'arrete au
        premier chunk et les index locaux entrent en collision avec ceux des
        messages plus anciens (ordre fausse, messages perdus).
        """
        rounds = max(6, min(20, max_rounds))
        last = -1
        stable = 0
        for _ in range(rounds):
            self.session.eval_body(_SCROLL_TOP_JS)
            self.session.wait_ms(pause_ms)
            setsize = int(self.session.eval_body(_PEEK_SETSIZE_JS) or 0)
            if setsize == last:
                stable += 1
                if stable >= 2:
                    return
            else:
                stable = 0
            last = setsize

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
