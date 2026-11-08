"""Tests des garde-fous reseau des telechargements non fiables.

Les URL viennent des conversations : jamais le reseau local, jamais un corps
sans limite, et jamais les identifiants vers un autre hote apres redirection.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from src.utils import net_guard


def test_refuse_les_cibles_locales_ou_non_http():
    for url in (
        "http://127.0.0.1/x",
        "http://localhost/x",
        "http://10.0.0.1/x",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/x",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "",
    ):
        assert net_guard.is_public_url(url) is False, url


def test_accepte_une_adresse_publique():
    assert net_guard.is_public_url("https://1.1.1.1/") is True


def test_lecture_plafonnee():
    class Reponse:
        def __init__(self, body: bytes):
            self.body = body

        def read(self, size: int = -1) -> bytes:
            return self.body if size < 0 else self.body[:size]

    assert net_guard.read_capped(Reponse(b"ok"), limit=10) == b"ok"
    with pytest.raises(ValueError):
        net_guard.read_capped(Reponse(b"x" * 20), limit=10)


def test_redirection_vers_hote_prive_refusee():
    handler = net_guard.SafeRedirectHandler()
    request = urllib.request.Request("https://example.com/a")
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(
            request, None, 302, "Found", {}, "http://127.0.0.1/interne"
        )


def test_redirection_changee_retire_les_identifiants(monkeypatch):
    monkeypatch.setattr(net_guard, "is_public_url", lambda url: True)
    handler = net_guard.SafeRedirectHandler()
    request = urllib.request.Request(
        "https://chatgpt.com/a",
        headers={"Cookie": "session=1", "Authorization": "Bearer t", "Accept": "*/*"},
    )
    autre = handler.redirect_request(
        request, None, 302, "Found", {}, "https://cdn.example/b"
    )
    assert "Cookie" not in autre.headers
    assert "Authorization" not in autre.headers

    meme_hote = handler.redirect_request(
        request, None, 302, "Found", {}, "https://chatgpt.com/b"
    )
    assert meme_hote.headers.get("Cookie") == "session=1"
