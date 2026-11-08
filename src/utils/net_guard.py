"""Garde-fous reseau pour les telechargements de contenu non fiable.

Les URL viennent des conversations (markdown, pieces jointes, `download_url`
d'une API) : elles ne doivent jamais faire sortir une requete vers la machine
locale, le reseau prive ou les metadonnees cloud, ni charger un corps sans
limite. Ce module centralise la verification d'adresse, la lecture plafonnee
et un gestionnaire de redirection qui ne reemet pas les identifiants vers un
autre hote.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import urlsplit

#: taille maximale d'un corps telecharge (image, piece jointe, JSON d'API)
MAX_REMOTE_BYTES = 32 * 1024 * 1024
#: en-tetes qui ne doivent jamais suivre une redirection vers un autre hote
_SECRET_HEADERS = ("cookie", "authorization")


def _is_public_ip(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def is_public_url(url: str) -> bool:
    """L'URL pointe-t-elle un hote public en http(s) ?

    Le nom est resolu : une URL `http://127.0.0.1/...` ou un domaine qui
    resout vers le LAN est refusee (SSRF). Le rebinding DNS reste possible
    entre la verification et la connexion, mais le cas courant est ferme.
    """
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(
            parts.hostname, port, type=socket.SOCK_STREAM
        )
    except (OSError, ValueError):
        return False
    addresses = {info[4][0] for info in infos if info[4]}
    return bool(addresses) and all(_is_public_ip(addr) for addr in addresses)


def read_capped(response, limit: int = MAX_REMOTE_BYTES) -> bytes:
    """Lit le corps en refusant tout ce qui depasse `limit` octets."""
    try:
        body = response.read(limit + 1)
    except TypeError:
        # reponse factice (tests) dont `read()` n'accepte pas de taille
        body = response.read()
    body = body or b""
    if len(body) > limit:
        raise ValueError(f"reponse trop volumineuse (> {limit} octets)")
    return body


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirections : hote public exige, identifiants retires au changement."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_public_url(newurl):
            raise urllib.error.HTTPError(
                newurl, code, "redirection refusee", headers, fp
            )
        new_request = super().redirect_request(
            req, fp, code, msg, headers, newurl
        )
        if new_request is not None:
            old_host = urlsplit(req.full_url).hostname
            new_host = urlsplit(newurl).hostname
            if old_host != new_host:
                for name in list(new_request.headers):
                    if name.lower() in _SECRET_HEADERS:
                        del new_request.headers[name]
        return new_request


_installed = False


def _install_once() -> None:
    """Installe l'opener sur (redirections sures) une seule fois."""
    global _installed
    if not _installed:
        urllib.request.install_opener(
            urllib.request.build_opener(SafeRedirectHandler())
        )
        _installed = True


def open_url(request: urllib.request.Request, timeout: float):
    """Ouvre une requete avec les garde-fous de redirection."""
    _install_once()
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_capped(
    url: str,
    headers: Optional[dict] = None,
    timeout: float = 20,
    limit: int = MAX_REMOTE_BYTES,
):
    """GET non authentifie garde : `(octets, content-type)` ou None."""
    if not is_public_url(url):
        return None
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with open_url(request, timeout) as response:
            ctype = (response.headers.get("Content-Type") or "").split(";")[0]
            return read_capped(response, limit), ctype.strip() or None
    except (OSError, ValueError, urllib.error.URLError):
        return None
