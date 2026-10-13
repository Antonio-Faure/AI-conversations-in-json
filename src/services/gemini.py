"""Scraper Gemini / Google (gemini.google.com) via Playwright.

Gemini lazy-loade l'historique complet derriere un bouton "Show all" dans la
sidebar : on le clique si present avant de scroller.

Chemin **API** (opt-in `services.gemini.api: true`) : les lectures passent par
les RPC `batchexecute` de l'app (`MaZiqc` = liste, `hNvQHb` = fil) avec les
cookies du profil persistant et les jetons tournants extraits de la page
(`SNlM0e`, `F.sid`, `bl`). Le DOM reste le chemin par defaut et le repli ; en
mode API, aucun HTML n'est genere : le payload brut est archive par
l'orchestrateur dans `exports/gemini/raw/<slug>.json` (pas de screenshots).
"""

from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from ..parsers.base import ParseError
from ..parsers.gemini import TURN_SELECTOR, GeminiParser, merge_turn_fragments
from ..schema import ConversationRef, normalize_timestamp
from ..utils.logging import get_logger
from .base import (
    BaseService,
    RateLimitedError,
    ScrapedPage,
    ServiceNotLoggedIn,
)

log = get_logger("services.gemini")

#: base de l'app et endpoint des RPC
API_BASE = "https://gemini.google.com"
API_RPC_PATH = "/_/BardChatUi/data/batchexecute"
#: RPC de la liste des conversations (page de 100 max cote serveur)
API_LIST_RPC = "MaZiqc"
#: RPC du fil complet d'une conversation
API_CONVERSATION_RPC = "hNvQHb"
#: taille de page de la decouverte (le serveur renvoie jusqu'a 100 par appel)
API_LIST_PAGE_SIZE = 100
#: taille de page des tours : le serveur renvoie les tours du plus recent au
#: plus ancien, decoupes en pages internes de 10 (verifie) ; demander 10 donne
#: une page par appel, donc un ordre reconstruit de facon deterministe.
API_TURNS_PAGE_SIZE = 10
#: nombre maxi de pages parcourues (garde-fou)
API_MAX_PAGES = 200
#: 429 / erreur applicative : on attend puis on reessaie
API_RETRY_ATTEMPTS = 6
#: attente de base (secondes), doublee a chaque tentative
API_RETRY_WAIT_S = 30
#: plafond de l'attente (secondes)
API_RETRY_WAIT_MAX_S = 300

#: jetons tournants de l'app, lus dans `window.WIZ_global_data` (repli : script
#: inline quand l'hydratation a retire l'objet global).
TOKENS_JS = """
() => {
  const w = window.WIZ_global_data || {};
  const out = {at: w.SNlM0e || null,
               sid: w.FdrFJe != null ? String(w.FdrFJe) : null,
               bl: w.cfb2h || null};
  if (!out.at) {
    for (const s of document.querySelectorAll('script')) {
      const t = s.textContent || '';
      const m = t.match(/"SNlM0e":"([^"]+)"/);
      if (m) { out.at = m[1]; break; }
    }
  }
  return out;
}
"""


def _domain_matches(host: str, domain: str) -> bool:
    """Vrai si le cookie (domaine ``.google.com``) s'applique a l'hote."""
    domain = domain.lstrip(".")
    return host == domain or host.endswith("." + domain)


def _retry_after_s(headers: Any) -> Optional[float]:
    """`Retry-After` numerique d'une reponse HTTP (None sinon)."""
    try:
        value = (headers or {}).get("Retry-After")
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


class _ApiAuthError(RuntimeError):
    """Jeton de session refuse par l'API (401/403) : a rafraichir."""


def _decode_batchexecute(text: str, rpcid: str) -> Optional[Any]:
    """Payload d'un RPC dans une reponse `batchexecute` (prefixe `)]}'`).

    La reponse est une suite de lignes JSON : les lignes de resultat
    ``["wrb.fr", "<rpcid>", "<payload JSON echappe>", ...]`` portent le payload
    (decode) ; les autres (``di``, ``af.httprm``, ``e``) sont ignorees. Une
    reponse d'erreur applicative (``["er", ..., 401, ...]``) retourne None.
    """
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            rows = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, list) or len(row) < 3:
                continue
            if row[0] != "wrb.fr" or row[1] != rpcid:
                continue
            payload = row[2]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    return None
            return payload
    return None


def _is_auth_error(text: str) -> bool:
    """Vrai si la reponse `batchexecute` porte une erreur 401/403 applicative."""
    return bool(re.search(r'"er"[^\n]*\b(?:401|403)\b', text or ""))


def _soft_error_code(text: str) -> Optional[int]:
    """Code d'une erreur applicative transitoire (quota/limite), sinon None.

    Gemini repond parfois ``["wrb.fr", "<rpc>", null, ..., [13,null,
    [["type.googleapis.com/...BardErrorInfo",[1096]]]]]`` (payload nul + code) :
    c'est une limite temporaire, comme un 429 applicatif. On extrait le code
    pour le journal.
    """
    match = re.search(r"BardErrorInfo\"\s*,\s*\[\s*(\d+)", text or "")
    if match:
        return int(match.group(1))
    match = re.search(r'"er"[^\n]*?,\s*(4\d\d|5\d\d)\s*,', text or "")
    return int(match.group(1)) if match else None


class _GeminiHttpClient:
    """Requetes `batchexecute` authentifiees (cookies du profil + jetons).

    Les jetons (`at`, `f.sid`, `bl`) sont extraits une fois de la page
    connectee par le service ; les appels suivants sont du HTTP pur. L'en-tete
    `Cookie` est filtre par domaine comme le fait Chromium (un en-tete complet
    melangeant les domaines fait repondre 401).
    """

    def __init__(
        self,
        cookies: List[Dict[str, Any]],
        tokens: Dict[str, Any],
        *,
        user_agent: str,
        accept_language: str = "en-US,en;q=0.9",
        timeout_s: int = 45,
    ):
        self.cookies = list(cookies or [])
        self.at = str(tokens.get("at") or "")
        self.sid = str(tokens.get("sid") or "")
        self.bl = str(tokens.get("bl") or "")
        self.user_agent = user_agent
        self.accept_language = accept_language
        self.timeout_s = timeout_s
        self._reqid = random.randint(100000, 900000)

    def _cookie_header(self, url: str) -> str:
        host = urllib.parse.urlsplit(url).hostname or ""
        pairs = [
            f"{c.get('name')}={c.get('value')}"
            for c in self.cookies
            if c.get("name")
            and _domain_matches(host, str(c.get("domain") or host))
        ]
        return "; ".join(pairs)

    def _headers(self, url: str) -> Dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Cookie": self._cookie_header(url),
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Accept": "*/*",
            "Accept-Language": self.accept_language,
            "Origin": API_BASE,
            "Referer": API_BASE + "/",
            "X-Same-Domain": "1",
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
            "gemini: HTTP 429, nouvel essai dans %.0fs (%s/%s) %s",
            wait_s, attempt + 1, API_RETRY_ATTEMPTS, url,
        )
        time.sleep(wait_s)
        return True

    def _wait_soft_error(self, attempt: int, code: Optional[int]) -> bool:
        """Attend (backoff) apres une erreur applicative transitoire."""
        if attempt + 1 >= API_RETRY_ATTEMPTS:
            return False
        wait_s = min(API_RETRY_WAIT_MAX_S, API_RETRY_WAIT_S * (2**attempt))
        log.warning(
            "gemini: erreur applicative %s (limite temporaire), nouvel essai "
            "dans %.0fs (%s/%s)",
            code, wait_s, attempt + 1, API_RETRY_ATTEMPTS,
        )
        time.sleep(wait_s)
        return True

    def post(self, rpcid: str, payload: Any, source_path: str) -> Tuple[int, str]:
        """POST `batchexecute` ; sur 429, attend et reessaie."""
        url = API_BASE + API_RPC_PATH
        params = urllib.parse.urlencode(
            {
                "rpcids": rpcid,
                "source-path": source_path,
                "bl": self.bl,
                "f.sid": self.sid,
                "hl": "fr",
                "_reqid": str(self._reqid),
                "rt": "c",
            }
        )
        self._reqid += 100
        body = urllib.parse.urlencode(
            {
                "f.req": json.dumps(
                    [[[rpcid, json.dumps(payload, separators=(",", ":")), None, "generic"]]],
                    separators=(",", ":"),
                ),
                "at": self.at,
            }
        )
        for attempt in range(API_RETRY_ATTEMPTS):
            request = urllib.request.Request(
                f"{url}?{params}",
                data=body.encode("utf-8"),
                headers=self._headers(url),
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    return response.status, response.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and self._wait_429(attempt, exc.headers, url):
                    continue
                return exc.code, (exc.read() or b"").decode("utf-8", "replace")
            except urllib.error.URLError as exc:
                raise RuntimeError(f"gemini: requete echouee ({url}): {exc}") from exc
        raise RateLimitedError(f"gemini: 429 persistant sur {url}")

    def rpc(self, rpcid: str, payload: Any, source_path: str) -> Any:
        """Payload decode d'un RPC ; gere 429, erreurs douces et 401.

        - 429 HTTP : attente (Retry-After puis backoff) dans `post` ; s'il
          persiste, `RateLimitedError` (arret propre du service) ;
        - erreur applicative transitoire (payload nul + `BardErrorInfo`, ex.
          limite de debit) : attente puis nouvel essai ;
        - 401/403 : `_ApiAuthError` (le service relit les jetons).
        """
        for attempt in range(API_RETRY_ATTEMPTS):
            status, text = self.post(rpcid, payload, source_path)
            if status in (401, 403):
                raise _ApiAuthError(
                    f"gemini: non authentifie (HTTP {status}) -> `run.py --login gemini`"
                )
            if status == 429:
                raise RateLimitedError(f"gemini: 429 persistant sur {rpcid}")
            if status != 200:
                raise RuntimeError(f"gemini: HTTP {status} sur {rpcid}")
            data = _decode_batchexecute(text, rpcid)
            if data is not None:
                return data
            if _is_auth_error(text):
                raise _ApiAuthError(
                    "gemini: jeton de session refuse -> `run.py --login gemini`"
                )
            if not self._wait_soft_error(attempt, _soft_error_code(text)):
                raise RateLimitedError(
                    f"gemini: limite de debit persistante sur {rpcid} "
                    f"(code {_soft_error_code(text)})"
                )
        raise RuntimeError(f"gemini: erreur applicative sur {rpcid}")


#: remise a zero de l'accumulateur JS des tours de conversation
_RESET_TURNS_JS = (
    "window.__aicvTurns = Object.create(null);"
    "window.__aicvLen = Object.create(null);"
    "window.__aicvOrder = [];"
    "return 0;"
)
#: memorise les tours visibles puis remonte le fil d'environ un ecran a chaque
#: passage. Gemini virtualise l'historique : sauter directement en haut
#: (`scrollTop = 0`) peut ne jamais rendre les fenetres intermediaires. On
#: defile donc de proche en proche, en memorisant chaque tour des qu'il apparait.
#: L'identite d'un tour est la requete utilisateur (stable d'un rendu a l'autre)
#: et non l'`id` du conteneur (regenere a chaque re-rendu virtualise).
#: Le DOM final peut manquer la reponse d'un tour encore en rendu : on ecrase
#: alors l'exemplaire memorise des que la reponse apparait, et on renvoie une
#: signature (tours repondus) pour ne pas conclure a la stabilite trop tot.
_COLLECT_TURNS_JS = """
return (function(){
  const nodes = Array.from(document.querySelectorAll(__SELECTOR__));
  const fresh = [];
  const answered = nodes.map(function(n){
    return n.querySelector('model-response message-content') ? '1' : '0';
  });
  for (const n of nodes) {
    const user = n.querySelector('user-query');
    const label = user ? (user.innerText || user.textContent || '') : '';
    const norm = label.replace(/\\s+/g, ' ').trim().slice(0, 200);
    const key = norm ? ('u\u0000' + norm) : ('id\u0000' + (n.getAttribute('id') || ''));
    const html = n.outerHTML;
    const answer = n.querySelector('model-response message-content');
    const hasAnswer = answer ? true : false;
    // longueur du texte de reponse : sert a reconnaitre une version plus
    // complete quand le rendu (listes, tableaux) arrive apres le 1er passage.
    const answerLen = answer ? (answer.innerText || answer.textContent || '').length : 0;
    const previous = window.__aicvTurns[key];
    if (previous === undefined) {
      window.__aicvTurns[key] = html;
      window.__aicvLen[key] = answerLen;
      window.__aicvOrder.push(key);
      fresh.push(key);
    } else if (
      hasAnswer &&
      (previous.indexOf('<model-response') === -1 || answerLen > (window.__aicvLen[key] || 0))
    ) {
      // version plus complete (reponse rendue, contenu enrichi) : on ecrase
      // l'exemplaire memorise pour ne pas garder une reponse partielle.
      window.__aicvTurns[key] = html;
      window.__aicvLen[key] = answerLen;
    }
  }
  // Gemini scrolle dans <infinite-scroller> (pas la fenetre) : on remonte le
  // conteneur scrollable par petits pas pour declencher le lazy-load.
  let scrollHost = nodes.length ? nodes[0].parentElement : null;
  while (scrollHost) {
    if (scrollHost.scrollHeight > scrollHost.clientHeight + 5) break;
    scrollHost = scrollHost.parentElement;
  }
  let atTop = true;
  if (scrollHost) {
    const step = Math.max(120, Math.round(scrollHost.clientHeight * 0.8));
    scrollHost.scrollTop = Math.max(0, scrollHost.scrollTop - step);
    atTop = scrollHost.scrollTop <= 0;
  }
  return [fresh.length, window.__aicvOrder.length, nodes.length, answered.join(''), atTop ? 1 : 0];
})();
""".replace("__SELECTOR__", json.dumps(TURN_SELECTOR))
#: amene le fil en bas (certains fils s'ouvrent sur les premiers tours)
_SCROLL_BOTTOM_JS = """
return (function(){
  const nodes = document.querySelectorAll(__SELECTOR__);
  if (nodes.length) nodes[nodes.length - 1].scrollIntoView({block: 'end', inline: 'nearest'});
  return nodes.length;
})();
""".replace("__SELECTOR__", json.dumps(TURN_SELECTOR))
#: ordre final du DOM (le virtualiseur conserve les tours charges) : sert
#: d'ordre de reference du fil, independamment de l'ordre de decouverte.
_FINAL_TURNS_JS = """
return (function(){
  const nodes = Array.from(document.querySelectorAll(__SELECTOR__));
  return {
    order: nodes.map(function(n){ return n.outerHTML; }),
    seen: window.__aicvOrder.map(function(k){ return window.__aicvTurns[k]; })
  };
})();
""".replace("__SELECTOR__", json.dumps(TURN_SELECTOR))


class GeminiService(BaseService):
    name = "gemini"
    home_url = "https://gemini.google.com/app"

    sidebar_ready_selectors = (
        "conversation-history a[href*='/app/']",
        "a[href*='/app/']",
    )
    #: sidebar demarree repliee : bouton "Ouvrir la barre laterale" / "Open sidebar"
    sidebar_open_selectors = (
        "button[aria-label*='barre latérale' i]",
        "button[aria-label*='sidebar' i]",
    )
    #: ouvre la liste complete des conversations (sinon seul "Recent" est visible)
    expand_selectors = (
        "[data-test-id='history-show-all']",
        "button:has-text('Show all')",
        "button:has-text('Show more')",
        "button:has-text('Tout afficher')",
        "button:has-text('Tout afficher plus')",
    )
    login_url_parts = ("accounts.google.com", "service.login", "signin")
    # NB: pas de selecteur sur a[href*='accounts.google.com'] : quand on est
    # connecte, le menu du compte (SignOutOptions) matche et fait un faux
    # positif. La redirection vers accounts.google.com suffit (URL).
    login_selectors = (
        "button:has-text('Sign in')",
        "input[type='password']",
    )

    def __init__(self, session, config: Optional[Dict[str, Any]] = None):
        super().__init__(session, config)
        #: client HTTP de l'API (construit au premier appel en mode API)
        self._http: Optional[_GeminiHttpClient] = None

    def build_parser(self) -> GeminiParser:
        return GeminiParser()

    # -- chemin API (opt-in) ---------------------------------------------------

    def _api_config(self) -> Dict[str, Any]:
        """Config du service (`services.gemini` de la config globale)."""
        services = (self.config or {}).get("services") or {}
        return services.get(self.name) or {}

    @property
    def api_enabled(self) -> bool:
        """True si le chemin API est active (`services.gemini.api: true`)."""
        return bool(self._api_config().get("api"))

    def _api_client(self) -> _GeminiHttpClient:
        """Client HTTP de l'API, jetons extraits une fois de la page connectee.

        Gemini n'accepte pas les appels `batchexecute` sans les jetons tournants
        de la page (`SNlM0e`/`F.sid`/`bl`) : on ouvre la home connectee, on
        attend que l'app soit rendue, on lit `window.WIZ_global_data`, puis tout
        le reste se fait en HTTP pur avec les cookies du profil persistant.
        """
        if self._http is not None:
            return self._http
        self.session.goto(self.home_url)
        # les jetons sont valides une fois l'app hydratee : on attend la sidebar
        try:
            self.session.wait_for_any(
                list(self.sidebar_ready_selectors) or ["body"], timeout_ms=20000
            )
        except Exception:  # noqa: BLE001 (session factice des tests)
            pass
        self.session.wait_ms(500)
        tokens = self.session.evaluate(TOKENS_JS) or {}
        if not isinstance(tokens, dict) or not tokens.get("at"):
            raise ServiceNotLoggedIn(
                "gemini: jetons de session absents -> `run.py --login gemini`"
            )
        cookies = self.session.cookies() or []
        if not cookies:
            raise ServiceNotLoggedIn(
                "gemini: aucun cookie de session -> `run.py --login gemini`"
            )
        # l'UA (et la langue) doivent coller au profil : les cookies sont lies a
        # l'empreinte du navigateur.
        fingerprint = getattr(self.session, "fingerprint", None) or {}
        user_agent = str(fingerprint.get("user_agent") or "") or (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        lang = str(fingerprint.get("lang") or "")
        accept_language = f"{lang},{lang.split('-')[0]};q=0.9" if lang else "fr-FR,fr;q=0.9"
        self._http = _GeminiHttpClient(
            cookies,
            tokens,
            user_agent=user_agent,
            accept_language=accept_language,
        )
        log.debug("gemini: client API pret (at=%s)", bool(tokens.get("at")))
        return self._http

    def _rpc(self, rpcid: str, payload: Any, source_path: str) -> Any:
        """RPC avec rafraichissement des jetons si la session est refusee.

        Les jetons tournants peuvent expirer pendant un long run : sur 401/403
        on les relit une fois depuis la page connectee avant de rejouer l'appel.
        """
        client = self._api_client()
        try:
            return client.rpc(rpcid, payload, source_path)
        except _ApiAuthError as exc:
            log.warning("gemini: %s -> rafraichissement des jetons", exc)
            self._http = None
            client = self._api_client()
            return client.rpc(rpcid, payload, source_path)

    def _list_conversations_api(
        self, limit: Optional[int] = None
    ) -> List[ConversationRef]:
        """Decouverte paginee par API (RPC `MaZiqc`, curseur opaque)."""
        refs: Dict[str, ConversationRef] = {}
        cursor: Optional[str] = None
        seen_cursors: set = set()
        for _ in range(API_MAX_PAGES):
            payload = [
                API_LIST_PAGE_SIZE,
                cursor,
                [None, None, 1, None, None, None, None, None, None, 1],
            ]
            data = self._rpc(API_LIST_RPC, payload, "/app")
            items = data[2] if isinstance(data, list) and len(data) > 2 else []
            for item in items or []:
                if not isinstance(item, list) or not item:
                    continue
                raw_id = str(item[0] or "")
                cid = raw_id[2:] if raw_id.startswith("c_") else raw_id
                if not cid:
                    continue
                stamp = item[5] if len(item) > 5 else None
                last_message_at = None
                if isinstance(stamp, list) and stamp and isinstance(stamp[0], (int, float)):
                    nanos = stamp[1] if len(stamp) > 1 and isinstance(stamp[1], (int, float)) else 0
                    last_message_at = normalize_timestamp(
                        float(stamp[0]) + float(nanos) / 1e9
                    )
                refs[cid] = ConversationRef(
                    service=self.name,
                    id=cid,
                    url=f"{API_BASE}/app/{cid}",
                    title=str(item[1]) if len(item) > 1 and item[1] else None,
                    raw={"last_message_at": last_message_at},
                )
                if limit and len(refs) >= limit:
                    break
            if limit and len(refs) >= limit:
                break
            next_cursor = (
                str(data[1])
                if isinstance(data, list) and len(data) > 1 and data[1]
                else None
            )
            if not next_cursor or not items:
                break
            if next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        log.debug("gemini api: %s conversations decouvertes", len(refs))
        return list(refs.values())

    def _fetch_conversation_api(self, cid: str) -> List[Any]:
        """Tours du fil, dans l'ordre chronologique.

        Le RPC renvoie les tours du plus recent au plus ancien (pages et
        intra-page) : on suit le curseur puis on inverse la concatenation pour
        retrouver l'ordre du fil (celui que rend l'app une fois remontee).
        """
        turns: List[Any] = []
        cursor: Optional[str] = None
        seen_cursors: set = set()
        for _ in range(API_MAX_PAGES):
            payload = [f"c_{cid}", API_TURNS_PAGE_SIZE, cursor, 1, [0], [4], None, 1]
            data = self._rpc(API_CONVERSATION_RPC, payload, f"/app/{cid}")
            page = data[0] if isinstance(data, list) and data else []
            next_cursor = (
                str(data[1])
                if isinstance(data, list) and len(data) > 1 and data[1]
                else None
            )
            if next_cursor and (next_cursor == cursor or next_cursor in seen_cursors):
                # curseur deja vu : le serveur rejoue la meme page, ne pas
                # concatener les tours une seconde fois (garde-fou)
                break
            turns.extend(page or [])
            if not next_cursor or not page:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        turns.reverse()
        return turns

    def list_conversations(self, limit: Optional[int] = None) -> List[ConversationRef]:
        if self.api_enabled:
            return self._list_conversations_api(limit=limit)
        return super().list_conversations(limit=limit)

    def export_conversation_with_html(self, ref: ConversationRef) -> tuple:
        """API (opt-in) : payload JSON -> Conversation ; pas de HTML.

        Le payload brut (`turns` dans l'ordre du fil) est memorise
        (`take_raw_payload`) pour que l'orchestrateur l'archive dans
        `exports/gemini/raw/<slug>.json` : il remplace le HTML comme artefact
        de re-parse.
        """
        if not self.api_enabled:
            return super().export_conversation_with_html(ref)
        turns = self._fetch_conversation_api(ref.id)
        if not turns:
            raise ParseError(f"gemini: conversation vide ou introuvable ({ref.id})")
        payload = {
            "conversation_id": ref.id,
            "title": ref.title,
            "turns": turns,
        }
        self.record_api_shape(payload)
        conv = self.parser.parse(
            "",
            conversation_id=ref.id,
            extra={"api": payload, "title_hint": ref.title},
        )
        if not conv.url:
            conv.url = self.conversation_url(ref)
        self._raw_payload = payload
        return conv, None

    def capture_message_screenshots(self, conversation, out_dir) -> int:
        """Pas de screenshots en mode API (le DOM n'est pas rendu)."""
        if self.api_enabled:
            return 0
        return super().capture_message_screenshots(conversation, out_dir)

    def after_sidebar_open(self) -> None:
        """Ouvre la sidebar (elle peut demarrer repliee) puis la liste complete."""
        for sel in self.sidebar_open_selectors:
            if self.session.click_if_present(sel, timeout_ms=1500):
                self.session.wait_ms(2500)
                break
        for sel in self.expand_selectors:
            if self.session.click_if_present(sel):
                self.session.wait_ms(1200)
                return

    def conversation_url(self, ref: ConversationRef) -> str:
        return ref.url or f"https://gemini.google.com/app/{ref.id}"

    def scrape_conversation(self, ref: ConversationRef) -> ScrapedPage:
        """Charge le fil complet en remontant, puis remplace le HTML.

        Le pipeline commun scrolle vers le bas (le fil est deja en bas) : Gemini
        ne charge les anciens tours qu'en remontant le conteneur de conversation.
        On collecte donc les tours au fil du scroll vers le haut, on fusionne
        les exemplaires virtualises, et on parse l'ensemble accumule.
        """
        page = super().scrape_conversation(ref)
        seen, order = self._collect_conversation_turns()
        if seen or order:
            html = merge_turn_fragments(order, seen)
            if html:
                page.html = html
        return page

    def _collect_conversation_turns(self) -> Tuple[List[str], List[str]]:
        """Remonte le fil par increments en memorisant les tours.

        Retourne ``(seen, order)`` : l'accumulateur des tours decouverts et
        l'ordre logique du DOM final (reference pour la fusion).
        """
        scroll_cfg = self.config.get("scroll", {})
        pause_ms = int(scroll_cfg.get("pause_ms", 700))
        max_rounds = max(20, int(scroll_cfg.get("max_rounds", 60)))
        stable_rounds = 3

        self.session.eval_body(_RESET_TURNS_JS)
        # s'assure d'abord d'avoir le bas du fil (le fil peut s'ouvrir en haut)
        for _ in range(3):
            self.session.eval_body(_SCROLL_BOTTOM_JS)
            self.session.wait_ms(pause_ms)
        last_total = -1
        last_signature = None
        stable = 0
        for _ in range(max_rounds):
            result = self.session.eval_body(_COLLECT_TURNS_JS)
            self.session.wait_ms(pause_ms)
            total = int(result[1]) if result else 0
            signature = str(result[3]) if result and len(result) > 3 else ""
            at_top = bool(result[4]) if result and len(result) > 4 else False
            # stabilite = en haut du fil ET plus de tours charges/repondus
            if at_top and total == last_total and signature == last_signature:
                stable += 1
            else:
                stable = 0
            last_total = total
            last_signature = signature
            if at_top and stable >= stable_rounds:
                break

        data = self.session.eval_body(_FINAL_TURNS_JS) or {}
        # `seen` = accumulateur (versions completes, ordre de decouverte) ;
        # `order` = DOM final (ordre logique du fil). La fusion prend `order`
        # comme reference d'ordre et complete avec `seen` les tours absents.
        seen = [f for f in data.get("seen") or [] if f]
        order = [f for f in data.get("order") or [] if f]
        return seen, order
