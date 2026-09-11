"""Client HTTP authentifie par cookies (pour les plateformes sans navigateur).

Grok bloque le Chromium automatise mais accepte les requetes HTTP avec les
cookies de session (captures via `run.py --login grok`). Ce client minimal
(urllib, sans dependance) porte le cookie jar et gere JSON + HTML.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class CookieAuthError(RuntimeError):
    """Cookies absents ou invalides pour un service authentifie par cookies."""


def load_cookie_header(cookies_dir: Path | str, service: str) -> str:
    """Construit l'en-tete `Cookie:` depuis cookies/<service>.json."""
    path = Path(cookies_dir) / f"{service}.json"
    if not path.exists():
        raise CookieAuthError(
            f"{service}: cookies absents ({path}) -> lancer `run.py --login {service}`"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CookieAuthError(f"{service}: cookies JSON invalide ({exc})") from exc
    cookies = data.get("cookies") or {}
    pairs = [(str(k), str(v)) for k, v in cookies.items() if str(v or "").strip()]
    if not pairs:
        raise CookieAuthError(
            f"{service}: aucun cookie renseigne ({path}) -> `run.py --login {service}`"
        )
    return "; ".join(f"{name}={value}" for name, value in pairs)


class CookieClient:
    """Requetes GET avec cookies persistants et User-Agent navigateur."""

    def __init__(
        self,
        service: str,
        cookies_dir: Path | str,
        user_agent: Optional[str] = None,
        timeout_s: int = 45,
    ):
        self.service = service
        self.cookie_header = load_cookie_header(cookies_dir, service)
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.timeout_s = timeout_s

    def _headers(self, accept: str) -> Dict[str, str]:
        return {
            "User-Agent": self.user_agent,
            "Cookie": self.cookie_header,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
        }

    def get(self, url: str, accept: str = "application/json, text/plain, */*") -> Tuple[int, bytes]:
        request = urllib.request.Request(url, headers=self._headers(accept))
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() or b""
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{self.service}: requete echouee ({url}): {exc}") from exc

    def get_json(self, url: str) -> Any:
        status, raw = self.get(url)
        if status == 401:
            raise CookieAuthError(
                f"{self.service}: non authentifie (401) -> `run.py --login {self.service}`"
            )
        if status != 200:
            raise RuntimeError(f"{self.service}: HTTP {status} sur {url}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{self.service}: reponse non-JSON sur {url}") from exc

    def get_text(self, url: str, accept: str = "text/html,application/xhtml+xml") -> str:
        status, raw = self.get(url, accept=accept)
        if status != 200:
            raise RuntimeError(f"{self.service}: HTTP {status} sur {url}")
        return raw.decode("utf-8", errors="replace")
