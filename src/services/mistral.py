"""Scraper Mistral (chat.mistral.ai) via API tRPC (cookies) ou navigateur.

L'application /chat a fusionne dans /work : toutes les conversations vivent
sous `/work/<id>` (les anciennes URLs `/chat/<id>` sont reecrites).

Chemin **API** (defaut) : les lectures passent par les procedures tRPC
(`/api/trpc/...`) avec les cookies de `cookies/mistral.json` et l'UA du profil ;
le batch non-streaming (`trpc-accept: application/json`) est utilise. Aucun
navigateur n'est ouvert, aucun HTML n'est genere : le payload brut
(messages + chat + canvas) est archive par l'orchestrateur dans
`exports/mistral/raw/<slug>.json`. Les versions alternatives (editions,
regenerations) sont exposees par l'API. `services.mistral.api: false` force le
chemin DOM historique (repli), qui reste celui des drivers d'envoi.

Chemin **DOM** (repli opt-in) : les fils longs etant virtualises (seule une
fenetre de tours est montee), le fil est collecte de maniere incrementale en
remontant le conteneur interne (voir `_collect_conversation`).
L'authentification se fait par les cookies du profil persistant
(`profiles/mistral`, via `run.py --login mistral`).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..http_client import (
    CookieAuthError,
    DEFAULT_USER_AGENT,
    host_in_domains,
    load_cookie_header,
)
from ..parsers.mistral import MistralParser, merge_message_fragments
from ..schema import Conversation, ConversationRef
from ..services.base import (
    BaseService,
    ConversationUnavailableError,
    EmptyConversationError,
    ServiceNotLoggedIn,
)
from ..utils.logging import get_logger
from ..utils.urls import normalize_conversation_url

log = get_logger("services.mistral")

BASE = "https://chat.mistral.ai"

#: hotes qui recoivent les cookies de session : la plateforme (`mistral.ai`),
#: son CDN (`mistralai.com`) et les conteneurs Azure de ses pieces jointes.
#: Les medias d'une conversation peuvent citer n'importe quel hote tiers : hors
#: de cette liste, le telechargement se fait sans cookie (best-effort).
COOKIE_HOSTS = (
    "mistral.ai",
    "mistralai.com",
    "mistralaichatupprodswe.blob.core.windows.net",
    "mistralaiblackforestprod.blob.core.windows.net",
)

# -- API tRPC (cookies) -------------------------------------------------------

#: procedures tRPC utilisees (batch = un seul appel HTTP)
API_LIST_PROC = "chat.last"
API_CONVERSATION_PROCS = ("message.all", "chat.byId", "canva.all")
API_SETTINGS_PROC = "settings.get"
#: nombre maxi de pages de decouverte (garde-fou ; 20 items par page)
API_MAX_PAGES = 200
#: 429 : attente et nouvel essai (Retry-After puis exponentiel)
API_RETRY_ATTEMPTS = 6
API_RETRY_WAIT_S = 30
API_RETRY_WAIT_MAX_S = 300
#: filtre de liste identique a l'application (chats prives modifiables)
API_LIST_FILTER: Dict[str, Any] = {
    "chatVisibility": "private",
    "chatPermission": "write",
    "includeProjectChats": False,
    "includeScheduledTaskChats": False,
    "direction": "forward",
}


class _TrpcNotFound(RuntimeError):
    """Objet demande absent (procedure tRPC en erreur NOT_FOUND)."""


def _retry_after_s(headers: Any) -> Optional[float]:
    """`Retry-After` numerique d'une reponse HTTP (None sinon)."""
    try:
        value = (headers or {}).get("Retry-After")
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


class _MistralHttpClient:
    """Requetes HTTP authentifiees (cookies) sur l'API tRPC de Mistral.

    Les procedures sont appelees en batch non-streaming
    (`trpc-accept: application/json`) : la reponse est un tableau JSON, une
    entree par procedure (`{"result": {"data": {"json": ...}}}` ou
    `{"error": ...}`). Aucun navigateur n'est utilise.
    """

    def __init__(
        self,
        cookie_header: str,
        *,
        user_agent: Optional[str] = None,
        accept_language: Optional[str] = None,
        timeout_s: int = 45,
        cookie_domains: Optional[Tuple[str, ...]] = None,
    ):
        self.cookie_header = cookie_header
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.accept_language = accept_language or "fr-FR,fr;q=0.9"
        self.timeout_s = timeout_s
        #: domaines autorises a recevoir le cookie (vide = aucun filtrage)
        self.cookie_domains = tuple(
            str(d).strip().lstrip(".").lower()
            for d in (cookie_domains or ())
            if str(d or "").strip()
        )

    def _headers(
        self, accept: str, trpc: bool = False, url: Optional[str] = None
    ) -> Dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": accept,
            "Accept-Language": self.accept_language,
            "Referer": BASE + "/work",
        }
        # hote tiers : requete sans cookie (best-effort, l'asset peut etre public)
        if not self.cookie_domains or host_in_domains(url or "", self.cookie_domains):
            headers["Cookie"] = self.cookie_header
        if trpc:
            # batch non-streaming : le serveur renvoie un tableau JSON simple
            headers["trpc-accept"] = "application/json"
            headers["x-trpc-source"] = "nextjs-react"
        return headers

    def _wait_429(self, attempt: int, headers: Any, url: str) -> bool:
        """Attend (backoff) avant un nouvel essai apres un 429 ; False sinon."""
        if attempt + 1 >= API_RETRY_ATTEMPTS:
            return False
        wait_s = _retry_after_s(headers) or min(
            API_RETRY_WAIT_MAX_S, API_RETRY_WAIT_S * (2**attempt)
        )
        log.warning(
            "mistral: HTTP 429, nouvel essai dans %.0fs (%s/%s) %s",
            wait_s, attempt + 1, API_RETRY_ATTEMPTS, url,
        )
        time.sleep(wait_s)
        return True

    def get(
        self, url: str, accept: str = "application/json, text/plain, */*", *,
        trpc: bool = False,
    ) -> Tuple[int, bytes]:
        """GET authentifie ; sur 429, attend et reessaie."""
        for attempt in range(API_RETRY_ATTEMPTS):
            request = urllib.request.Request(
                url, headers=self._headers(accept, trpc=trpc, url=url)
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and self._wait_429(attempt, exc.headers, url):
                    continue
                return exc.code, exc.read() or b""
            except urllib.error.URLError as exc:
                raise RuntimeError(
                    f"mistral: requete echouee ({url}): {exc}"
                ) from exc
        raise RuntimeError(f"mistral: 429 persistant sur {url}")

    def fetch(
        self, url: str, accept: str = "*/*"
    ) -> Optional[Tuple[bytes, Optional[str]]]:
        """(octets, content-type) d'une URL authentifiee ; None si echec.

        Utilise pour telecharger les assets du markdown (images, pieces
        jointes) sans navigateur.
        """
        for attempt in range(API_RETRY_ATTEMPTS):
            request = urllib.request.Request(
                url, headers=self._headers(accept, url=url)
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_s
                ) as response:
                    ctype = (response.headers.get("Content-Type") or "").split(";")[0]
                    return response.read(), (ctype.strip() or None)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and self._wait_429(attempt, exc.headers, url):
                    continue
                log.debug("mistral: fetch asset echoue (%s): HTTP %s", url, exc.code)
                return None
            except (urllib.error.URLError, OSError) as exc:
                log.debug("mistral: fetch asset echoue (%s): %s", url, exc)
                return None
        return None

    @staticmethod
    def _trpc_url(procedures: Tuple[str, ...], inputs: List[Any]) -> str:
        """URL d'un batch tRPC (`?batch=1&input=...`, meme encodage que l'app).

        Une entree peut etre une valeur simple ou un couple ``(valeur, meta)``
        quand le serveur attend un type superjson (ex. `cursor` -> Date).
        """
        payload: Dict[str, Any] = {}
        for index, value in enumerate(inputs):
            if isinstance(value, tuple) and len(value) == 2:
                body, meta = value
                entry: Dict[str, Any] = {"json": body}
                if meta:
                    entry["meta"] = meta
            else:
                entry = {"json": value}
            payload[str(index)] = entry
        query = urllib.parse.urlencode(
            {"batch": "1", "input": json.dumps(payload, separators=(",", ":"))}
        )
        return f"{BASE}/api/trpc/{','.join(procedures)}?{query}"

    def call_batch(self, procedures: Tuple[str, ...], inputs: List[Any]) -> List[Any]:
        """Entrees brutes d'un batch tRPC (resultat ou erreur par procedure)."""
        url = self._trpc_url(procedures, inputs)
        status, raw = self.get(url, trpc=True)
        if status in (401, 403):
            raise ServiceNotLoggedIn(
                f"mistral: non authentifie (HTTP {status}) -> `run.py --login mistral`"
            )
        if status not in (200, 207):
            raise RuntimeError(f"mistral: HTTP {status} sur {url}")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"mistral: reponse non-JSON sur {url}") from exc
        if not isinstance(data, list):
            raise RuntimeError(f"mistral: reponse tRPC inattendue sur {url}")
        return data

    @staticmethod
    def entry_json(entry: Any, procedure: str) -> Any:
        """`data.json` d'une entree de batch ; leve sur erreur tRPC."""
        if not isinstance(entry, dict):
            raise RuntimeError(f"mistral: entree tRPC invalide ({procedure})")
        error = entry.get("error")
        if error:
            data = error.get("json") if isinstance(error, dict) else None
            code = ""
            if isinstance(data, dict):
                code = str(data.get("code") or (data.get("data") or {}).get("code") or "")
            if code == "NOT_FOUND" or "NOT_FOUND" in json.dumps(error)[:400]:
                raise _TrpcNotFound(f"mistral: {procedure} introuvable")
            raise RuntimeError(f"mistral: erreur tRPC {procedure}: {str(error)[:200]}")
        result = entry.get("result") or {}
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, dict) or "json" not in data:
            raise RuntimeError(f"mistral: reponse tRPC sans donnees ({procedure})")
        return data.get("json")

    def call_one(self, procedure: str, value: Any) -> Any:
        """Appel d'une procedure unique ; retourne `data.json`."""
        entries = self.call_batch((procedure,), [value])
        return self.entry_json(entries[0], procedure)


class _ApiFetchSession:
    """Session facade API : telechargement des assets via le client HTTP.

    `download_service_images`/`download_service_files` passent par
    `session.fetch` ; en mode API on telecharge avec les cookies (client HTTP)
    au lieu de lancer le navigateur. Le reste est delegue a la session
    d'origine (jamais utilisee en mode API).
    """

    def __init__(self, service: "MistralService", inner: Any):
        self._service = service
        self._inner = inner

    def fetch(self, url: str):
        try:
            client = self._service._api_client()
        except Exception:  # noqa: BLE001 (session absente : repli http_loader)
            return None
        return client.fetch(url, accept="image/*,*/*")

    def __getattr__(self, name: str) -> Any:
        inner = object.__getattribute__(self, "_inner")
        return getattr(inner, name)

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

    def __init__(self, session, config: Optional[Dict[str, Any]] = None):
        super().__init__(session, config)
        #: client HTTP de l'API (construit au premier appel en mode API)
        self._http: Optional[_MistralHttpClient] = None
        #: payload brut de la derniere conversation exportee en mode API
        self._raw_payload: Optional[Dict[str, Any]] = None
        #: alias de modele -> nom affiche (`settings.get`, cache du run)
        self._model_display_map: Optional[Dict[str, str]] = None
        if self.api_enabled:
            # les assets du markdown passent par le client HTTP, pas le navigateur
            self.session = _ApiFetchSession(self, session)

    def conversation_url(self, ref: ConversationRef) -> str:
        return normalize_conversation_url(ref.url) or f"{BASE}/work/{ref.id}"

    # -- chemin API (defaut) -----------------------------------------------------

    def _api_config(self) -> Dict[str, Any]:
        """Config du service (`services.mistral` de la config globale)."""
        services = (self.config or {}).get("services") or {}
        return services.get(self.name) or {}

    @property
    def api_enabled(self) -> bool:
        """True si le chemin API est actif (defaut ; `api: false` -> DOM)."""
        return bool(self._api_config().get("api", True))

    def _api_client(self) -> _MistralHttpClient:
        """Client HTTP de l'API (cookies de `cookies/mistral.json`).

        L'UA (et la langue) viennent du profil pour coller a l'empreinte
        navigateur qui a produit les cookies.
        """
        if self._http is not None:
            return self._http
        cookies_dir = Path((self.config or {}).get("cookies_dir") or "cookies")
        try:
            cookie_header = load_cookie_header(cookies_dir, self.name)
        except CookieAuthError as exc:
            raise ServiceNotLoggedIn(str(exc)) from exc
        fingerprint = getattr(self.session, "fingerprint", None) or {}
        user_agent = str(fingerprint.get("user_agent") or "") or None
        lang = str(fingerprint.get("lang") or "")
        accept_language = f"{lang},{lang.split('-')[0]};q=0.9" if lang else None
        self._http = _MistralHttpClient(
            cookie_header,
            user_agent=user_agent,
            accept_language=accept_language,
            cookie_domains=COOKIE_HOSTS,
        )
        log.debug("mistral: client API pret")
        return self._http

    def _api_model_map(self) -> Dict[str, str]:
        """Alias de modele -> nom affiche (`settings.get`, une fois par run)."""
        if self._model_display_map is not None:
            return self._model_display_map
        mapping: Dict[str, str] = {}
        try:
            data = self._api_client().call_one(API_SETTINGS_PROC, None)
            entries = (data or {}).get("vibeWorkModelConfigs")
            if isinstance(entries, dict):
                # `settings.get` encapsule chaque reglage : {"value": ...}
                entries = entries.get("value")
            for entry in entries or []:
                if not isinstance(entry, dict):
                    continue
                alias = str(entry.get("model_alias") or "").strip()
                display = str(entry.get("model_display_name") or "").strip()
                if alias and display:
                    mapping.setdefault(alias, display)
        except Exception:  # noqa: BLE001 (repli : alias brut)
            log.debug("mistral: map des modeles indisponible", exc_info=True)
        self._model_display_map = mapping
        return mapping

    def _list_conversations_api(
        self, limit: Optional[int] = None
    ) -> List[ConversationRef]:
        """Decouverte paginee par API (`chat.last` + `nextCursor`)."""
        client = self._api_client()
        refs: Dict[str, ConversationRef] = {}
        cursor: Optional[str] = None
        seen_cursors: set = set()
        for _ in range(API_MAX_PAGES):
            payload = dict(API_LIST_FILTER)
            if cursor:
                # `cursor` est un Date superjson : le serveur exige le meta
                payload["cursor"] = cursor
                value: Any = (payload, {"values": {"cursor": ["Date"]}, "v": 1})
            else:
                value = payload
            data = client.call_one(API_LIST_PROC, value)
            items = data.get("items") or []
            for item in items:
                cid = str(item.get("id") or "")
                if not cid:
                    continue
                refs[cid] = ConversationRef(
                    service=self.name,
                    id=cid,
                    url=f"{BASE}/work/{cid}",
                    title=item.get("userTitle")
                    or item.get("generatedTitle")
                    or item.get("title")
                    or None,
                    raw={"last_message_at": item.get("updatedAt")},
                )
                if limit and len(refs) >= limit:
                    break
            cursor = data.get("nextCursor") or None
            if cursor and cursor in seen_cursors:
                break
            if cursor:
                seen_cursors.add(cursor)
            if (limit and len(refs) >= limit) or not cursor or not items:
                break
        log.debug("mistral api: %s conversations decouvertes", len(refs))
        return list(refs.values())

    def _fetch_conversation_api(
        self, client: _MistralHttpClient, conversation_id: str
    ) -> Dict[str, Any]:
        """Payload de conversation (messages + chat + canvas + map des modeles).

        ``message.all`` renvoie toutes les versions ; ``chat.byId`` absent
        (NOT_FOUND) marque la conversation indisponible.
        """
        entries = client.call_batch(
            API_CONVERSATION_PROCS,
            [
                {"chatId": conversation_id},
                {"id": conversation_id},
                {"chatId": conversation_id},
            ],
        )
        messages = client.entry_json(entries[0], API_CONVERSATION_PROCS[0])
        if isinstance(messages, dict) and messages.get("nextCursor"):
            # aucune pagination observee (meme sur 99 messages) : signaler si
            # le serveur se met a tronquer les longs fils
            log.warning(
                "mistral: message.all pagine (nextCursor non gere) %s",
                conversation_id,
            )
        try:
            chat = client.entry_json(entries[1], API_CONVERSATION_PROCS[1])
        except _TrpcNotFound as exc:
            raise ConversationUnavailableError(
                f"mistral: conversation introuvable ({conversation_id})"
            ) from exc
        try:
            canvases = client.entry_json(entries[2], API_CONVERSATION_PROCS[2])
        except _TrpcNotFound:
            canvases = {"items": []}
        return {
            "messages": messages,
            "chat": chat,
            "canvases": canvases,
            "model_display_map": self._api_model_map(),
        }

    def take_raw_payload(self) -> Optional[Dict[str, Any]]:
        """Payload API de la derniere conversation exportee (consomme)."""
        payload, self._raw_payload = self._raw_payload, None
        return payload

    def export_conversation_with_html(self, ref: ConversationRef) -> tuple:
        """API (defaut) : payload JSON -> Conversation ; pas de HTML.

        Le payload brut (messages + chat + canvas) est memorise
        (`take_raw_payload`) pour que l'orchestrateur l'archive dans
        `exports/mistral/raw/<slug>.json` : il remplace le HTML comme artefact
        de re-parse.
        """
        if not self.api_enabled:
            return super().export_conversation_with_html(ref)
        client = self._api_client()
        data = self._fetch_conversation_api(client, ref.id)
        self.record_api_shape(data)
        if not (data.get("messages") or {}).get("items"):
            raise EmptyConversationError(
                f"mistral: conversation sans message ({ref.id})"
            )
        conv: Conversation = self.parser.parse(
            "", conversation_id=ref.id, extra={"api": data}
        )
        if not conv.url:
            conv.url = self.conversation_url(ref)
        self._raw_payload = data
        return conv, None

    def capture_message_screenshots(self, conversation, out_dir) -> int:
        """Pas de screenshots en mode API (le DOM n'est pas rendu)."""
        if self.api_enabled:
            return 0
        return super().capture_message_screenshots(conversation, out_dir)

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
        if self.api_enabled:
            return self._list_conversations_api(limit=limit)
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

