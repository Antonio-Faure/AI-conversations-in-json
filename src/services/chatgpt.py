"""Scraper ChatGPT / OpenAI (chatgpt.com) via Playwright / Botasaurus.

Particularites :
  - sidebar scrollable infinie (nav[aria-label="Chat history"])
  - UI « DIL » (2026) : un tour par `[data-turn-key]`, messages dans
    `[data-chatgpt-search-unit-key="...:user|assistant"]` ; l'historique n'est
    charge qu'a la **molette reelle** (`scroll_wheel`, un `scrollTop` programme
    est annule)
  - timestamps : plus de `create_time` par message ; on recupere le
    `create_time`/`update_time` de conversation du script bootstrap
    (approximation de `started_at`/`last_message_at`). Modele : plus expose -> `None`

Chemin **API** (opt-in `services.chatgpt.api: true`) : les lectures passent par
l'API backend (`/backend-api/conversations[...]`) avec les cookies du profil
persistant ; le token (`accessToken`) et l'identifiant de compte sont recuperes
une fois via la session navigateur (`/api/auth/session`), puis tout se fait en
HTTP pur (comme Grok). Aucun HTML n'est genere : le payload brut de l'API est
archive par l'orchestrateur dans `exports/chatgpt/raw/<slug>.json`, pas de
screenshots. Le DOM reste le chemin par defaut et le repli.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from ..parsers.chatgpt import (
    ChatGPTParser,
    merge_turn_snapshots,
    order_fragments_by_time,
)
from ..schema import ConversationRef, normalize_timestamp
from ..utils.logging import get_logger
from .api_client import CookieHttpClient, Transport
from .base import BaseService, ScrapedPage, ServiceNotLoggedIn

log = get_logger("services.chatgpt")

#: base de l'API backend de ChatGPT
API_BASE = "https://chatgpt.com"
API_LIST_URL = API_BASE + "/backend-api/conversations"
API_CONVERSATION_URL = API_BASE + "/backend-api/conversations/{cid}"
API_SESSION_URL = API_BASE + "/api/auth/session"
#: endpoint qui resout un asset en URL signee (voir le mapper `parse_api`)
API_FILE_DOWNLOAD_PREFIX = API_BASE + "/backend-api/files/"
#: taille de page de la decouverte (l'API pagine par offset)
API_PAGE_SIZE = 100
#: nombre maxi de pages parcourues (garde-fou)
API_MAX_PAGES = 500
#: nombre de tours demandes par conversation (plafond serveur ~100)
API_NUM_TURNS = 100
#: 429 : limite de rafale du backend (observee apres ~40 lectures en ~3 min ;
#: la fenetre se rouvre en quelques minutes). On attend et on reessaie.
API_RETRY_ATTEMPTS = 8
#: attente de base sur 429 (secondes), doublee a chaque tentative
API_RETRY_WAIT_S = 45
#: plafond de l'attente sur 429 (secondes)
API_RETRY_WAIT_MAX_S = 300


def _is_file_download_url(url: str) -> bool:
    """URL de l'endpoint de resolution d'asset (`files/<id>/download`)."""
    return url.startswith(API_FILE_DOWNLOAD_PREFIX) and url.endswith("/download")


class _ChatGPTHttpClient(CookieHttpClient):
    """Requetes GET authentifiees sur l'API backend de ChatGPT.

    Les cookies viennent de la session navigateur (profil persistant) ; le
    token et l'identifiant de compte sont recuperes via `/api/auth/session`.
    Aucun navigateur n'est utilise pour les lectures. Le backoff 429 vient du
    client commun (`CookieHttpClient`).
    """

    service = "chatgpt"
    _log = log
    retry_attempts = API_RETRY_ATTEMPTS
    retry_wait_s = API_RETRY_WAIT_S
    retry_wait_max_s = API_RETRY_WAIT_MAX_S

    def __init__(
        self,
        cookie_header: str,
        *,
        access_token: Optional[str] = None,
        account_id: Optional[str] = None,
        user_agent: Optional[str] = None,
        accept_language: Optional[str] = None,
        timeout_s: int = 45,
        transport: Optional[Transport] = None,
    ):
        super().__init__(
            cookie_header,
            user_agent=user_agent,
            accept_language=accept_language,
            timeout_s=timeout_s,
            transport=transport,
        )
        self.access_token = access_token
        self.account_id = account_id

    def _headers(self, accept: str, *, url: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Cookie": self.cookie_header,
            "Accept": accept,
            "Accept-Language": self.accept_language,
            "Referer": API_BASE + "/",
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if self.account_id:
            headers["chatgpt-account-id"] = self.account_id
        return headers


class _ApiFetchSession:
    """Session facade : telechargement des assets via le client HTTP authentifie.

    `download_service_images` telecharge les images du markdown via
    `session.fetch`. En mode API, les URL `backend-api/files/<id>/download`
    sont resolues (JSON -> URL signee) puis telechargees par le client HTTP
    (cookies + Bearer + `chatgpt-account-id`), sans navigateur ; tout le reste
    est delegue a la session d'origine.
    """

    def __init__(self, service: "ChatGPTService", inner: Any):
        self._service = service
        self._inner = inner

    def fetch(self, url: str):
        if _is_file_download_url(url):
            return self._service.download_api_asset(url)
        inner_fetch = getattr(self._inner, "fetch", None)
        if callable(inner_fetch):
            return inner_fetch(url)
        return None

    def __getattr__(self, name: str) -> Any:
        inner = object.__getattribute__(self, "_inner")
        return getattr(inner, name)


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
  const nodes = document.querySelectorAll(
    '[data-chatgpt-search-unit-key], [data-chatgpt-selection-message-id], [data-message-id]');
  nodes.forEach((el) => {
    let id = el.getAttribute('data-chatgpt-search-message-ids');
    if (id) id = id.split(/\\s+/)[0];
    if (!id) id = el.getAttribute('data-chatgpt-selection-message-id');
    if (!id) id = el.getAttribute('data-dil-message-id');
    if (!id) id = el.getAttribute('data-message-id');
    if (!id) return;
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
        const msg = props.message || (props.data && props.data.message) || props.node
          || (Array.isArray(props.messages)
              ? props.messages.find((m) => m && m.id === id) : null);
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

#: selecteur des tours ChatGPT (UI « DIL » 2026 : un tour par `data-turn-key`)
TURN_SELECTOR = "[data-turn-key]"

#: JS d'extraction des timestamps de conversation depuis le script bootstrap.
#: La nouvelle UI n'expose plus `create_time` par message, mais le flux
#: react-router contient le `create_time`/`update_time` de la conversation
#: (approximation de started_at/last_message_at). Present meme apres hydratation.
EXTRACT_CONVERSATION_TIMES_JS = """
() => {
  const out = { created_at: null, last_message_at: null };
  const read = (name) => {
    const re = new RegExp(name + '[^0-9]{0,4}([0-9]+(?:[.][0-9]+)?)');
    for (const s of document.querySelectorAll('script')) {
      const t = s.textContent || '';
      if (t.indexOf(name) < 0) continue;
      const m = t.match(re);
      if (m) {
        let v = parseFloat(m[1]);
        if (v > 1e12) v = v / 1000;
        return v;
      }
    }
    return null;
  };
  out.created_at = read('create_time');
  out.last_message_at = read('update_time');
  return out;
}
"""

#: timestamps de conversation lus dans le HTML SSR (repli quand le script
#: bootstrap a ete retire apres hydratation)
CONVERSATION_CREATE_RE = re.compile(r"create_time[^0-9]{0,6}([0-9]+(?:\.[0-9]+)?)")
CONVERSATION_UPDATE_RE = re.compile(r"update_time[^0-9]{0,6}([0-9]+(?:\.[0-9]+)?)")

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
    let mid = n.getAttribute('data-turn-key');
    if (mid) return 't:' + mid;
    mid = n.getAttribute('data-chatgpt-search-message-ids');
    if (mid) return 'm:' + mid.split(/\\s+/)[0];
    mid = n.getAttribute('data-chatgpt-selection-message-id');
    if (mid) return 'm:' + mid;
    mid = n.getAttribute('data-message-id');
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
  // « Loading older messages… » : l'historique est encore en cours de fetch
  let loading = false;
  const sr = document.querySelectorAll('span.sr-only');
  for (let i = 0; i < sr.length; i++) {
    const t = sr[i].textContent || '';
    if (t.indexOf('Loading older') >= 0 || t.indexOf('Chargement') >= 0) {
      loading = true;
      break;
    }
  }
  return {
    dom: nodes.length,
    fresh: fresh,
    total: Object.keys(acc.seen).length,
    top: s ? Math.round(s.scrollTop) : 0,
    client: s ? s.clientHeight : 0,
    height: s ? s.scrollHeight : 0,
    loading: loading,
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
  const step = Math.max(200, Math.round(s.clientHeight * 0.5));
  s.scrollTop = Math.max(0, s.scrollTop - step);
  return Math.round(s.scrollTop);
})();
"""

#: coordonnees du centre du conteneur scrollable (pour la molette reelle)
_SCROLLER_RECT_JS = """
return (function(){
  const acc = window.__aicvChatgpt;
  const s = acc && acc.scroller;
  if (!s) return null;
  const r = s.getBoundingClientRect();
  return {
    x: Math.round(r.left + r.width / 2),
    y: Math.round(r.top + r.height / 2),
    client: s.clientHeight,
  };
})();
"""

#: deploie les panneaux d'activite visibles (raisonnement/outils).
#: Replie, le panneau n'expose pas les etapes d'outil dans le DOM : on clique
#: les boutons `aria-expanded=false` des en-tetes d'activite pour que le
#: contenu entre dans l'outerHTML capte (nouvelle UI : le libelle « Worked for
#: Xs » est dans un span frere, pas dans le bouton).
_EXPAND_COT_JS = """
return (function(){
  const re = /^(thought for|worked for|reasoned for|thinking|réflexion|raisonnement|a réfléchi|a travaillé|travail pendant|réfléchi pendant)[\\s:]/i;
  let count = 0;
  document.querySelectorAll('button[aria-expanded="false"]').forEach(function(b){
    const header = b.closest("[class*='activity']");
    const text = (b.innerText || b.textContent || '').trim();
    if (header || (text && re.test(text))) {
      b.click();
      count++;
    }
  });
  return count;
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

#: messages portant un bouton « Voir les versions » (plusieurs versions).
#: La nouvelle UI n'expose que la version active dans le fil : le dialogue
#: « Versions » (Radix) liste les autres. Il ne s'ouvre que sur un vrai clic
#: souris (`session.click_at`), pas sur un clic JS synthetique. La barre
#: d'actions (et donc le bouton) vit dans l'unite du message utilisateur : on
#: indexe les boutons plutot que de deduire l'id du message versionne. Le
#: libelle est localise (FR « Voir les versions », EN « See versions ») : on
#: repere aussi l'icone, stable (`clock-arrow-rotate-counterclockwise`).
_VERSIONS_FIND_JS = """
return (function(){
  let n = 0;
  document.querySelectorAll('button').forEach(function(b){
    const lab = b.getAttribute('aria-label') || '';
    const use = b.querySelector('svg use');
    const href = use ? (use.getAttribute('href') || '') : '';
    if (/voir les versions|see versions/i.test(lab)
        || href.indexOf('clock-arrow-rotate-counterclockwise') >= 0) n++;
  });
  return n;
})();
"""

#: centre le i-eme bouton « Voir les versions » et rend ses coordonnees
_VERSIONS_POS_JS = """
return (function(){
  const all = [...document.querySelectorAll('button')].filter(function(b){
    const lab = b.getAttribute('aria-label') || '';
    const use = b.querySelector('svg use');
    const href = use ? (use.getAttribute('href') || '') : '';
    return /voir les versions|see versions/i.test(lab)
        || href.indexOf('clock-arrow-rotate-counterclockwise') >= 0;
  });
  const b = all[__INDEX__];
  if (!b) return null;
  b.scrollIntoView({block: 'center', inline: 'nearest'});
  const r = b.getBoundingClientRect();
  return {x: Math.round(r.left + r.width / 2),
          y: Math.round(r.top + r.height / 2)};
})();
"""

#: identifiants de messages presents dans le fil (version active incluse)
_VERSIONS_THREAD_IDS_JS = """
return (function(){
  const out = [];
  document.querySelectorAll('[data-chatgpt-search-message-ids]').forEach(function(n){
    (n.getAttribute('data-chatgpt-search-message-ids') || '').split(/\\s+/)
      .forEach(function(id){ if (id) out.push(id); });
  });
  document.querySelectorAll('[data-chatgpt-selection-message-id], [data-dil-message-id]')
    .forEach(function(n){
      const id = n.getAttribute('data-chatgpt-selection-message-id')
              || n.getAttribute('data-dil-message-id');
      if (id) out.push(id);
    });
  return [...new Set(out)];
})();
"""

#: etat du dialogue « Versions » : position, navigation, version affichee.
#: `ready` = barre de navigation + contenu rendus (le dialogue s'affiche par
#: etapes : lire trop tot donne un dialogue vide).
_VERSIONS_DIALOG_JS = """
return (function(){
  const dlg = [...document.querySelectorAll('[role="dialog"]')]
    .find(d => (d.innerText || '').indexOf('Versions') >= 0);
  if (!dlg) return null;
  const m = (dlg.innerText || '').match(/Version\\s+(\\d+)/);
  const find = function(re){
    return [...dlg.querySelectorAll('button')].find(b =>
      re.test(b.getAttribute('aria-label') || '') || re.test((b.innerText || '').trim()));
  };
  const prev = find(/version précédente|previous version/i);
  const next = find(/version suivante|next version/i);
  const ids = [...new Set([...dlg.querySelectorAll(
      '[data-dil-message-id], [data-chatgpt-selection-message-id]')]
    .map(n => n.getAttribute('data-dil-message-id')
           || n.getAttribute('data-chatgpt-selection-message-id'))
    .filter(Boolean))];
  const md = dlg.querySelector(
    "[data-markdown-text-style='assistant-message'], [data-markdown-text-tone='user-message']");
  const head = md ? (md.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 80) : '';
  return {ready: !!(prev && next && md),
          label: m ? parseInt(m[1], 10) : null,
          prev: !!(prev && !prev.hasAttribute('disabled')),
          next: !!(next && !next.hasAttribute('disabled')),
          ids: ids, head: head};
})();
"""

#: HTML complet du dialogue (la version affichee est dedans)
_VERSIONS_FRAGMENT_JS = """
return (function(){
  const dlg = [...document.querySelectorAll('[role="dialog"]')]
    .find(d => (d.innerText || '').indexOf('Versions') >= 0);
  return dlg ? dlg.outerHTML : null;
})();
"""

#: coordonnees d'un bouton du dialogue (navigation / fermeture)
_VERSIONS_CLICK_JS = """
return (function(){
  const dlg = [...document.querySelectorAll('[role="dialog"]')]
    .find(d => (d.innerText || '').indexOf('Versions') >= 0);
  if (!dlg) return null;
  const re = new RegExp(__RE__, 'i');
  const b = [...dlg.querySelectorAll('button')].find(x =>
    re.test(x.getAttribute('aria-label') || '') || re.test((x.innerText || '').trim()));
  if (!b || b.hasAttribute('disabled')) return null;
  const r = b.getBoundingClientRect();
  return {x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)};
})();
"""


class ChatGPTService(BaseService):
    name = "chatgpt"
    home_url = "https://chatgpt.com/"

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

    def __init__(self, session, config: Optional[Dict[str, Any]] = None):
        super().__init__(session, config)
        #: client HTTP de l'API (construit au premier appel en mode API)
        self._http: Optional[_ChatGPTHttpClient] = None
        if self.api_enabled:
            # les assets du markdown (`files/<id>/download`) passent par le
            # client HTTP authentifie, pas par le navigateur
            self.session = _ApiFetchSession(self, session)

    def build_parser(self) -> ChatGPTParser:
        return ChatGPTParser()

    # -- chemin API (opt-in) ---------------------------------------------------

    def _session_transport(self) -> Optional[Transport]:
        """Transport via le reseau du navigateur, si la session le permet.

        Cloudflare challenge les clients HTTP bruts sur `/backend-api/*`
        (`Cf-Mitigated: challenge`) : les lectures passent alors par le
        contexte Chromium (cookies + empreinte TLS du navigateur). Repli
        urllib quand la session ne l'expose pas (ex. moteur botasaurus).
        """
        api_request = getattr(self.session, "api_request", None)
        if not callable(api_request):
            return None

        def transport(
            url: str,
            headers: Dict[str, str],
            data: Optional[bytes],
            method: Optional[str],
            timeout_s: int,
        ) -> Tuple[int, bytes, Optional[str]]:
            return api_request(
                url,
                headers=headers,
                data=data,
                method=method,
                timeout_ms=int(timeout_s * 1000),
            )

        return transport

    def _api_client(self) -> _ChatGPTHttpClient:
        """Client HTTP de l'API, token mint une fois via la session navigateur.

        `session.goto` ouvre la home connectee puis `/api/auth/session` fournit
        `accessToken` et l'identifiant de compte ; les cookies (httpOnly inclus)
        viennent du contexte persistant. Tout le reste est en HTTP pur.
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
                "chatgpt: aucun cookie de session -> `run.py --login chatgpt`"
            )
        access_token: Optional[str] = None
        account_id: Optional[str] = None
        fetched = self.session.fetch(API_SESSION_URL)
        if fetched:
            body = fetched[0]
            try:
                session_data = json.loads(
                    body.decode("utf-8", "replace")
                    if isinstance(body, (bytes, bytearray))
                    else str(body)
                )
            except json.JSONDecodeError:
                session_data = {}
            if isinstance(session_data, dict):
                token = session_data.get("accessToken")
                access_token = str(token) if token else None
                account = session_data.get("account") or {}
                account_id = str(account.get("id")) if account.get("id") else None
        if not access_token:
            raise ServiceNotLoggedIn(
                "chatgpt: session invalide (accessToken absent) -> "
                "`run.py --login chatgpt`"
            )
        # l'UA (et la langue) doivent coller au profil : les cookies sont lies
        # a l'empreinte du navigateur (un UA divergent -> HTTP 403).
        fingerprint = getattr(self.session, "fingerprint", None) or {}
        user_agent = str(fingerprint.get("user_agent") or "") or None
        lang = str(fingerprint.get("lang") or "")
        accept_language = f"{lang},{lang.split('-')[0]};q=0.9" if lang else None
        self._http = _ChatGPTHttpClient(
            cookie_header,
            access_token=access_token,
            account_id=account_id,
            user_agent=user_agent,
            accept_language=accept_language,
            transport=self._session_transport(),
        )
        log.debug(
            "chatgpt: client API pret (account=%s)", bool(account_id)
        )
        return self._http

    def download_api_asset(
        self, url: str
    ) -> Optional[Tuple[bytes, Optional[str]]]:
        """Resout un asset (`files/<id>/download`) puis telecharge les octets.

        L'endpoint renvoie ``{"download_url": <URL signee estuary>}`` : on suit
        ce lien avec le meme client authentifie. Retourne ``(octets,
        content-type)`` ou None (asset absent : l'URL reste dans le markdown).
        """
        client = self._api_client()
        try:
            status, raw = client.get(url)
        except RuntimeError as exc:
            log.debug("chatgpt: asset inaccessible (%s): %s", url, exc)
            return None
        if status != 200:
            log.debug("chatgpt: resolution d'asset HTTP %s (%s)", status, url)
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("chatgpt: reponse non-JSON pour l'asset %s", url)
            return None
        download_url = str(data.get("download_url") or "")
        if not download_url:
            return None
        return client.fetch(download_url)

    def _list_conversations_api(
        self, limit: Optional[int] = None
    ) -> List[ConversationRef]:
        """Decouverte paginee par API (`offset += limit` tant que plein)."""
        client = self._api_client()
        refs: Dict[str, ConversationRef] = {}
        offset = 0
        for _ in range(API_MAX_PAGES):
            url = (
                f"{API_LIST_URL}?exclude_conversation_origin=tpp&expand=false"
                f"&hide_snorlax=false&is_archived=false&is_starred=false"
                f"&limit={API_PAGE_SIZE}&order=updated&offset={offset}"
            )
            data = client.get_json(url)
            items = data.get("items") or []
            for item in items:
                cid = item.get("id")
                if not cid:
                    continue
                refs[str(cid)] = ConversationRef(
                    service=self.name,
                    id=str(cid),
                    url=f"{API_BASE}/c/{cid}",
                    title=item.get("title") or None,
                    raw={
                        # l'API renvoie des epoch (float) : normaliser pour que
                        # le tri de `conversation_list` reste homogene (ISO)
                        "started_at": normalize_timestamp(item.get("create_time")),
                        "last_message_at": normalize_timestamp(item.get("update_time")),
                    },
                )
                if limit and len(refs) >= limit:
                    break
            if limit and len(refs) >= limit:
                break
            if len(items) < API_PAGE_SIZE:
                break
            offset += API_PAGE_SIZE
        log.debug("chatgpt api: %s conversations decouvertes", len(refs))
        return list(refs.values())

    def _fetch_conversation_api(
        self, client: _ChatGPTHttpClient, conversation_id: str
    ) -> Dict[str, Any]:
        """Payload de conversation ; remonte les pages anciennes si besoin.

        L'API renvoie une fenetre de tours (`num_turns`, plafond ~100). Pour les
        longues conversations, `page_info.has_previous_page` signale des tours
        plus anciens charges via `before=<start_cursor>` (best effort : ce
        chemin n'a pas pu etre valide sur une conversation longue).
        """
        url = (
            f"{API_CONVERSATION_URL.format(cid=conversation_id)}"
            f"?num_turns={API_NUM_TURNS}&include_has_versions=true"
        )
        data = client.get_json(url)
        messages = list(data.get("messages") or [])
        page = data.get("page_info") or {}
        seen_cursors: set = set()
        for _ in range(API_MAX_PAGES):
            if not (page.get("has_previous_page") and page.get("start_cursor")):
                break
            cursor = str(page["start_cursor"])
            if cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
            older = client.get_json(url + f"&before={cursor}")
            older_messages = older.get("messages") or []
            known = {m.get("id") for m in messages}
            new = [m for m in older_messages if m.get("id") not in known]
            if not new:
                break
            messages = new + messages
            page = older.get("page_info") or {}
        merged = dict(data)
        merged["messages"] = messages
        return merged

    def list_conversations(self, limit: Optional[int] = None) -> List[ConversationRef]:
        if self.api_enabled:
            return self._list_conversations_api(limit=limit)
        return super().list_conversations(limit=limit)

    def export_conversation_with_html(self, ref: ConversationRef) -> tuple:
        """API (opt-in) : payload JSON -> Conversation ; pas de HTML.

        Le payload brut de l'API est memorise (`take_raw_payload`) pour que
        l'orchestrateur l'archive dans `exports/chatgpt/raw/<slug>.json` : il
        remplace le HTML comme artefact de re-parse.
        """
        if not self.api_enabled:
            return super().export_conversation_with_html(ref)
        client = self._api_client()
        data = self._fetch_conversation_api(client, ref.id)
        self.record_api_shape(data)
        conv = self.parser.parse(
            "", conversation_id=ref.id, extra={"api": data}
        )
        if not conv.url:
            conv.url = self.conversation_url(ref)
        self._raw_payload = data
        return conv, None

    def extract_extras(self, ref: ConversationRef) -> Dict[str, Any]:
        data = self.session.evaluate(EXTRACT_REACT_META_JS) or {}
        extra: Dict[str, Any] = {"messages": data.get("messages") or {}}
        if data.get("title"):
            extra["title_hint"] = data["title"]
        if ref.title:
            extra.setdefault("title_hint", ref.title)
        # timestamps de conversation (approximation, plus de create_time par message)
        times = self._conversation_times(ref)
        if times.get("created_at"):
            extra["created_at"] = times["created_at"]
        if times.get("last_message_at"):
            extra["last_message_at"] = times["last_message_at"]
        return extra

    def _conversation_times(self, ref: ConversationRef) -> Dict[str, Any]:
        """`create_time`/`update_time` de conversation (script bootstrap puis SSR).

        Le script inline est retire a l'hydratation (surtout sur les fils courts)
        : on refetch le HTML SSR en repli, qui contient toujours ces valeurs.
        """
        times = self.session.evaluate(EXTRACT_CONVERSATION_TIMES_JS) or {}
        if times.get("created_at") or times.get("last_message_at"):
            return times
        try:
            fetched = self.session.fetch(self.conversation_url(ref))
        except Exception:  # noqa: BLE001
            fetched = None
        if not fetched:
            return times
        body = fetched[0]
        text = (
            body.decode("utf-8", "replace")
            if isinstance(body, (bytes, bytearray))
            else str(body)
        )
        out: Dict[str, Any] = {}
        create = CONVERSATION_CREATE_RE.search(text)
        if create:
            out["created_at"] = float(create.group(1))
        update = CONVERSATION_UPDATE_RE.search(text)
        if update:
            out["last_message_at"] = float(update.group(1))
        return out

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
            # la chronologie par timestamp corrige les trous/ordre du scroll
            fragments = order_fragments_by_time(fragments, meta)
            page.html = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
                + "".join(fragments)
                + "</body></html>"
            )
        if meta:
            merged = dict(page.extra.get("messages") or {})
            merged.update(meta)
            page.extra["messages"] = merged
        if self.config.get("variants"):
            try:
                page.extra["variants"] = self._capture_variants()
            except Exception:  # noqa: BLE001
                log.debug("chatgpt: capture des variantes ignoree", exc_info=True)
        return page

    # -- versions alternatives (regenerations) ---------------------------------

    def _versions_dialog(self) -> Optional[Dict[str, Any]]:
        data = self.session.eval_body(_VERSIONS_DIALOG_JS)
        return data if isinstance(data, dict) else None

    def _versions_wait_dialog(self, timeout_ms: int = 8000) -> Optional[Dict[str, Any]]:
        """Attend que le dialogue « Versions » soit rendu (navigation + contenu)."""
        elapsed = 0
        while elapsed < timeout_ms:
            data = self._versions_dialog()
            if data is not None and data.get("ready"):
                return data
            self.session.wait_ms(250)
            elapsed += 250
        return self._versions_dialog()

    def _versions_wait_change(
        self, previous: Optional[Dict[str, Any]], timeout_ms: int = 5000
    ) -> Optional[Dict[str, Any]]:
        """Attend que la version affichee dans le dialogue change."""
        prev_key = (previous or {}).get("ids"), (previous or {}).get("head")
        elapsed = 0
        while elapsed < timeout_ms:
            data = self._versions_dialog()
            if data is not None and data.get("ready") and (
                (data.get("ids"), data.get("head")) != prev_key
            ):
                return data
            self.session.wait_ms(250)
            elapsed += 250
        return self._versions_dialog()

    def _versions_pos(self, index: int) -> Optional[Dict[str, Any]]:
        """Coordonnees du i-eme bouton « Voir les versions » (centre)."""
        body = _VERSIONS_POS_JS.replace("__INDEX__", str(int(index)))
        data = self.session.eval_body(body)
        return data if isinstance(data, dict) else None

    def _versions_click(self, pattern: str) -> bool:
        """Clic reel sur un bouton du dialogue (navigation / fermeture)."""
        pos = self.session.eval_body(
            _VERSIONS_CLICK_JS.replace("__RE__", json.dumps(pattern))
        )
        if not isinstance(pos, dict):
            return False
        self.session.click_at(int(pos.get("x") or 0), int(pos.get("y") or 0))
        self.session.wait_ms(300)
        return True

    def _capture_variants(self) -> Dict[str, Any]:
        """Capture les versions alternatives via le dialogue « Versions ».

        Retourne ``{message_id: {"active": index, "fragments": [html...]}}`` :
        fragments ordonnes du plus ancien au plus recent, ``active`` pointe la
        version affichee dans le fil (son id sert de cle). Le dialogue est
        referme en revenant a la version actuelle (aucune modification du fil).
        """
        captured: Dict[str, Any] = {}
        # le fil vient d'etre parcouru (collecte) : laisser le DOM se stabiliser
        self.session.wait_ms(600)
        count = int(self.session.eval_body(_VERSIONS_FIND_JS) or 0)
        for index in range(count):
            entry = self._capture_versions_at(index)
            if entry is None:
                # deuxieme tentative (rendu tardif, mise en page qui bouge)
                self.session.wait_ms(700)
                entry = self._capture_versions_at(index)
            if entry is None:
                log.info("chatgpt: versions non capturees (bouton %s/%s)", index + 1, count)
                continue
            key, data = entry
            captured[key] = data
        return captured

    def _capture_versions_at(self, index: int) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Capture un message versionne (un bouton) ; None si echec."""
        pos = self._versions_pos(index)
        if pos is None:
            return None
        self.session.wait_ms(300)
        pos = self._versions_pos(index) or pos
        self.session.click_at(int(pos.get("x") or 0), int(pos.get("y") or 0))
        dialog = self._versions_wait_dialog(4000)
        if dialog is None:
            pos = self._versions_pos(index)
            if pos is None:
                return None
            self.session.click_at(int(pos.get("x") or 0), int(pos.get("y") or 0))
            dialog = self._versions_wait_dialog(8000)
        if dialog is None:
            return None
        # reculer jusqu'a la premiere version
        for _ in range(50):
            if not dialog.get("prev"):
                break
            if not self._versions_click("version précédente|previous version"):
                break
            dialog = self._versions_wait_change(dialog)
            if dialog is None:
                break
        if dialog is None:
            # le dialogue s'est ferme/re-rendu pendant la navigation : le
            # fragment JS peut encore le trouver, mais son etat est perdu.
            log.warning(
                "chatgpt: dialogue « Versions » perdu pendant la navigation "
                "(bouton %s)",
                index + 1,
            )
            return None
        fragments: List[str] = []
        ids: List[str] = []
        for _ in range(50):
            html = self.session.eval_body(_VERSIONS_FRAGMENT_JS)
            if not html:
                break
            fragments.append(str(html))
            ids.append(next((i for i in (dialog.get("ids") or []) if i), ""))
            if not dialog.get("next"):
                break
            if not self._versions_click("version suivante|next version"):
                break
            dialog = self._versions_wait_change(dialog)
            if dialog is None:
                break
        # refermer : « Revenir a la version actuelle » (version du fil)
        self._versions_click("revenir à la version actuelle|return to current version")
        for _ in range(20):
            if self._versions_dialog() is None:
                break
            self.session.wait_ms(250)
        if self._versions_dialog() is not None and hasattr(self.session, "press"):
            self.session.press("Escape")
            self.session.wait_ms(300)
        if len(fragments) < 2:
            return None
        thread_ids = set(self.session.eval_body(_VERSIONS_THREAD_IDS_JS) or [])
        active = next((i for i, vid in enumerate(ids) if vid and vid in thread_ids), 0)
        key = ids[active] if active < len(ids) else ""
        if not key:
            return None
        return key, {"active": active, "fragments": fragments}

    def _collect_conversation_turns(self) -> Tuple[List[str], Dict[str, Any]]:
        """Remonte le fil en memorisant les tours, retourne (fragments, meta)."""
        scroll_cfg = self.config.get("scroll", {})
        pause_ms = max(200, int(scroll_cfg.get("pause_ms", 700)))
        stable_rounds = max(4, int(scroll_cfg.get("stable_rounds", 3)))
        # au sommet, ChatGPT charge l'historique par lots : il faut patienter
        # plus longtemps et exiger plus de rounds stables avant de conclure.
        top_pause_ms = max(pause_ms, int(scroll_cfg.get("top_pause_ms", 1500)))
        top_stable_rounds = max(
            stable_rounds, int(scroll_cfg.get("top_stable_rounds", 6))
        )
        max_rounds = max(80, int(scroll_cfg.get("max_rounds", 60)) * 3)

        self.session.eval_body(_RESET_TURNS_JS)
        if self.session.eval_body(_FIND_SCROLLER_JS) is None:
            return [], {}

        meta: Dict[str, Any] = {}

        def collect() -> Dict[str, Any]:
            # deploie le raisonnement des tours visibles avant de lire leur DOM
            # (sinon `_COLLECT_TURNS_JS` ne capture que le panneau replie).
            if self.session.eval_body(_EXPAND_COT_JS):
                self.session.wait_ms(350)
            info = self.session.eval_body(_COLLECT_TURNS_JS) or {}
            data = self.session.evaluate(EXTRACT_REACT_META_JS) or {}
            for message_id, entry in (data.get("messages") or {}).items():
                meta.setdefault(message_id, entry)
            return info

        def scroll_up(info: Dict[str, Any]) -> None:
            """Remonte le fil d'un cran.

            La nouvelle UI annule un `scrollTop` programme : on envoie une vraie
            molette au centre du conteneur (`thread-scroll-container`) pour
            declencher le chargement des tours anciens.
            """
            if hasattr(self.session, "scroll_wheel"):
                rect = self.session.eval_body(_SCROLLER_RECT_JS) or {}
                x = int(rect.get("x") or 0)
                y = int(rect.get("y") or 0)
                if x or y:
                    client = int(rect.get("client") or info.get("client") or 700)
                    self.session.scroll_wheel(x, y, -max(300, client // 2))
                    return
            self.session.eval_body(_SCROLL_UP_JS)

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
            top = int(info.get("top") or 0)
            loading = bool(info.get("loading"))
            if total == last_total and not loading:
                stable += 1
            else:
                stable = 0
            last_total = total
            at_top = top <= 1
            # tant que « Loading older messages… » est affiche, l'historique
            # n'est pas complet : on continue a envoyer la molette.
            if at_top and stable >= top_stable_rounds and not loading:
                break
            scroll_up(info)
            self.session.wait_ms(top_pause_ms if at_top else pause_ms)

        data = self.session.eval_body(_FINAL_TURNS_JS) or {}
        snapshots = data.get("snaps") or []
        html_by_key = data.get("map") or {}
        return merge_turn_snapshots(snapshots, html_by_key), meta
