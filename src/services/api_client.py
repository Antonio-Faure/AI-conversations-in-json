"""Client HTTP commun aux services API (urllib, cookies, retry 429).

ChatGPT, Claude, Gemini, Mistral et Perplexity reimplementaient le meme
transport : en-tete `Cookie`, backoff sur 429 (`Retry-After` plafonne puis
exponentiel) et traduction des statuts en exceptions du service. Ce module
factorise cette mecanique ; chaque service garde ses constantes (nombre
d'essais, attente de base, plafond), ses en-tetes specifiques et sa facon de
traduire les erreurs HTTP non rejouees.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from ..http_client import DEFAULT_USER_AGENT
from ..utils.logging import get_logger
from .base import RateLimitedError, ServiceNotLoggedIn

log = get_logger("services.api_client")


def retry_after_s(headers: Any) -> Optional[float]:
    """`Retry-After` numerique d'une reponse HTTP (None sinon)."""
    try:
        value = (headers or {}).get("Retry-After")
        return float(value) if value else None
    except (TypeError, ValueError):
        return None


class CookieHttpClient:
    """Requetes HTTP authentifiees par cookie, avec backoff 429 parametrable.

    Les sous-classes fixent `service` (libelle des messages), `_log` (canal de
    journal du service) et les constantes de retry ; elles surchargent
    `_headers` (en-tetes specifiques au service) et `_http_error` (traduction
    des erreurs HTTP non rejouees).
    """

    #: libelle du service dans les messages de journal
    service: str = ""
    #: journal du service (les sous-classes gardent leur canal `services.<bot>`)
    _log = log
    #: nombre total de tentatives (premier essai compris)
    retry_attempts: int = 6
    #: attente de base sur 429 (secondes), doublee a chaque tentative
    retry_wait_s: int = 30
    #: plafond de l'attente sur 429 (secondes)
    retry_wait_max_s: int = 300
    #: langue par defaut des requetes (surchargee par Mistral)
    default_accept_language: str = "en-US,en;q=0.9"

    def __init__(
        self,
        cookie_header: Optional[str],
        *,
        user_agent: Optional[str] = None,
        accept_language: Optional[str] = None,
        timeout_s: int = 45,
    ):
        self.cookie_header = cookie_header
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.accept_language = accept_language or self.default_accept_language
        self.timeout_s = timeout_s

    def _headers(self, accept: str, *, url: Optional[str] = None) -> Dict[str, str]:
        """En-tetes par defaut d'une requete (surcharge par service)."""
        return {
            "User-Agent": self.user_agent,
            "Cookie": self.cookie_header,
            "Accept": accept,
            "Accept-Language": self.accept_language,
        }

    def _wait_429(self, attempt: int, headers: Any, url: str) -> bool:
        """Attend (backoff) avant un nouvel essai apres un 429 ; False sinon.

        `Retry-After` est respecte mais plafonne : un en-tete de plusieurs
        heures ne doit pas endormir le run.
        """
        if attempt + 1 >= self.retry_attempts:
            return False
        wait_s = max(
            0.0,
            min(
                self.retry_wait_max_s,
                retry_after_s(headers) or self.retry_wait_s * (2**attempt),
            ),
        )
        self._log.warning(
            "%s: HTTP 429, nouvel essai dans %.0fs (%s/%s) %s",
            self.service, wait_s, attempt + 1, self.retry_attempts, url,
        )
        time.sleep(wait_s)
        return True

    def _http_error(
        self, exc: urllib.error.HTTPError, url: str
    ) -> Tuple[int, bytes]:
        """Erreur HTTP non rejouee : `(statut, corps)` (surcharge par service).

        Le defaut rend le statut a l'appelant (`get_json`, `call_batch`) qui
        decide quoi lever ; un service peut aussi lever directement (Perplexity
        et son `PerplexityHttpError`).
        """
        return exc.code, exc.read() or b""

    def _send(
        self,
        url: str,
        accept: str = "application/json, text/plain, */*",
        *,
        data: Optional[bytes] = None,
        method: Optional[str] = None,
        **header_kwargs: Any,
    ) -> Tuple[int, bytes]:
        """Requete HTTP brute : `(statut, corps)` ; 429 -> attente puis essai.

        `header_kwargs` est transmis a `_headers` (surcharges de service).
        """
        for attempt in range(self.retry_attempts):
            request = urllib.request.Request(
                url,
                data=data,
                method=method,
                headers=self._headers(accept, url=url, **header_kwargs),
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and self._wait_429(attempt, exc.headers, url):
                    continue
                return self._http_error(exc, url)
            except urllib.error.URLError as exc:
                raise RuntimeError(
                    f"{self.service}: requete echouee ({url}): {exc}"
                ) from exc
        raise RateLimitedError(f"{self.service}: 429 persistant sur {url}")

    def get(
        self, url: str, accept: str = "application/json, text/plain, */*"
    ) -> Tuple[int, bytes]:
        """GET authentifie ; sur 429, attend et reessaie (limite de rafale)."""
        return self._send(url, accept)

    def get_json(self, url: str) -> Any:
        """Corps JSON d'un GET ; 401/403 -> session expiree, 429 -> arret."""
        status, raw = self.get(url)
        if status in (401, 403):
            raise ServiceNotLoggedIn(
                f"{self.service}: non authentifie (HTTP {status}) -> "
                f"`run.py --login {self.service}`"
            )
        if status == 429:
            # limite de rafale epuisee : arreter le service proprement (reprise
            # plus tard) au lieu de faire echouer chaque conversation
            raise RateLimitedError(f"{self.service}: 429 persistant sur {url}")
        if status != 200:
            raise RuntimeError(f"{self.service}: HTTP {status} sur {url}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{self.service}: reponse non-JSON sur {url}") from exc

    def fetch(
        self, url: str, accept: str = "*/*"
    ) -> Optional[Tuple[bytes, Optional[str]]]:
        """(octets, content-type) d'une URL authentifiee ; None si echec.

        Sur 429, attend et reessaie comme `get` ; toute autre erreur (HTTP ou
        reseau) rend None : l'appelant garde l'URL distante en repli.
        """
        for attempt in range(self.retry_attempts):
            request = urllib.request.Request(url, headers=self._headers(accept, url=url))
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    ctype = (response.headers.get("Content-Type") or "").split(";")[0]
                    return response.read(), (ctype.strip() or None)
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and self._wait_429(attempt, exc.headers, url):
                    continue
                self._log.debug(
                    "%s: fetch asset echoue (%s): HTTP %s", self.service, url, exc.code
                )
                return None
            except (urllib.error.URLError, OSError) as exc:
                self._log.debug(
                    "%s: fetch asset echoue (%s): %s", self.service, url, exc
                )
                return None
        return None
