"""Tests du client HTTP a cookies : liste blanche d'hotes (anti-fuite).

Une URL de media citee dans une conversation peut pointer vers n'importe quel
hote tiers : le cookie de session ne doit jamais suivre (audit securite F1).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.http_client import CookieAuthError, CookieClient, host_in_domains
from src.services.base import RateLimitedError
from src.services.grok import COOKIE_HOSTS as GROK_HOSTS, GrokService
from src.services.mistral import (
    COOKIE_HOSTS as MISTRAL_HOSTS,
    MistralService,
    _MistralHttpClient,
)


class _FakeResponse:
    """Reponse minimale pour `urllib.request.urlopen` simule."""

    status = 200
    headers = {"Content-Type": "image/png"}

    def __init__(self, body: bytes = b""):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@pytest.fixture()
def capture_requests(monkeypatch):
    """Capture `(url, en-tetes)` de chaque requete urllib simulee."""
    captured = []

    def fake_urlopen(request, timeout=None):
        headers = {k.lower(): v for k, v in request.header_items()}
        captured.append((request.full_url, headers))
        return _FakeResponse(b"\x89PNG\r\n\x1a\n" + b"0" * 8)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return captured


def _cookie_of(captured, url):
    for seen, headers in captured:
        if seen == url:
            return headers.get("cookie")
    raise AssertionError(f"requete absente: {url}")


def _write_cookies(tmp_path: Path, service: str, name: str, value: str) -> Path:
    cookies = tmp_path / "cookies"
    cookies.mkdir(exist_ok=True)
    (cookies / f"{service}.json").write_text(
        json.dumps({"cookies": {name: value}}), encoding="utf-8"
    )
    return cookies


class TestHostInDomains:
    def test_exact_et_sous_domaines(self):
        assert host_in_domains("https://grok.com/x", ("grok.com",))
        assert host_in_domains("https://assets.grok.com/x", ("grok.com",))
        assert host_in_domains("https://a.b.x.ai/x", ("x.ai",))

    def test_suffixe_piege_refuse(self):
        assert not host_in_domains("https://evilgrok.com/x", ("grok.com",))
        assert not host_in_domains("https://grok.com.evil.example/x", ("grok.com",))

    def test_domaine_normalise(self):
        assert host_in_domains("https://assets.grok.com./x", (".GROK.COM",))

    def test_sans_hote(self):
        assert not host_in_domains("", ("grok.com",))
        assert not host_in_domains("pas une url", ("grok.com",))


class TestCookieClientHosts:
    def test_cookie_joint_aux_hotes_legitimes_seulement(
        self, tmp_path, capture_requests
    ):
        cookies = _write_cookies(tmp_path, "grok", "sso", "secret")
        client = CookieClient("grok", cookies, cookie_domains=GROK_HOSTS)
        client.get("https://evil.example/tracker.png")
        client.get("https://assets.grok.com/users/u1/a1/content")
        client.get("https://grok.com/rest/app-chat/conversations?pageSize=50")
        assert _cookie_of(capture_requests, "https://evil.example/tracker.png") is None
        assert _cookie_of(
            capture_requests, "https://assets.grok.com/users/u1/a1/content"
        ) == "sso=secret"
        # les appels API de la plateforme ne changent pas de comportement
        assert _cookie_of(
            capture_requests, "https://grok.com/rest/app-chat/conversations?pageSize=50"
        ) == "sso=secret"


class TestGrokFetchSansFuite:
    def test_fetch_media_hors_plateforme_sans_cookie(self, tmp_path, capture_requests):
        cookies = _write_cookies(tmp_path, "grok", "sso", "secret")
        service = GrokService(None, {"cookies_dir": str(cookies)})
        assert service.session.fetch("https://evil.example/tracker.png")
        assert service.session.fetch("https://assets.grok.com/x/content")
        assert _cookie_of(capture_requests, "https://evil.example/tracker.png") is None
        assert _cookie_of(
            capture_requests, "https://assets.grok.com/x/content"
        ) == "sso=secret"


class TestCookieClientRetry:
    """429 : attente + nouvel essai ; 403 : erreur d'authentification (audit F4)."""

    def test_429_transitoire_reessaie(self, tmp_path, monkeypatch):
        import time as time_module
        import urllib.error
        import urllib.request

        cookies = _write_cookies(tmp_path, "grok", "sso", "secret")
        calls = {"n": 0}
        sleeps = []

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 429, "Too Many Requests",
                    {"Retry-After": "7"}, None,
                )
            return _FakeResponse(b"{}")

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(time_module, "sleep", sleeps.append)
        client = CookieClient("grok", cookies, cookie_domains=GROK_HOSTS)
        status, raw = client.get("https://grok.com/rest/app-chat/conversations")
        assert status == 200 and raw == b"{}"
        assert calls["n"] == 2
        assert sleeps == [7.0]  # Retry-After respecte

    def test_429_persistant_leve_rate_limited(self, tmp_path, monkeypatch):
        import time as time_module
        import urllib.error
        import urllib.request

        cookies = _write_cookies(tmp_path, "grok", "sso", "secret")

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 429, "Too Many Requests", {}, None
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(time_module, "sleep", lambda s: None)
        client = CookieClient("grok", cookies, cookie_domains=GROK_HOSTS)
        with pytest.raises(RateLimitedError):
            client.get_json("https://grok.com/rest/app-chat/conversations")

    def test_403_leve_cookie_auth_error(self, tmp_path, monkeypatch):
        import urllib.error
        import urllib.request

        cookies = _write_cookies(tmp_path, "grok", "sso", "secret")

        def fake_urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 403, "Forbidden", {}, None
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        client = CookieClient("grok", cookies, cookie_domains=GROK_HOSTS)
        with pytest.raises(CookieAuthError):
            client.get_json("https://grok.com/rest/app-chat/conversations")


class _FakeSession:
    """Session navigateur factice (le mode API ne doit pas la toucher)."""

    def __init__(self, fingerprint=None):
        self.fingerprint = fingerprint or {}
        self.calls = []

    def __getattr__(self, name):
        def _recorder(*args, **kwargs):
            self.calls.append(name)
            return None

        return _recorder


class TestMistralFetchSansFuite:
    def test_client_fetch_hors_plateforme_sans_cookie(self, capture_requests):
        client = _MistralHttpClient("ory_session_x=secret", cookie_domains=MISTRAL_HOSTS)
        client.fetch("https://evil.example/tracker.png")
        client.fetch("https://static-assets.mistralai.com/x.png")
        client.fetch("https://mistralaichatupprodswe.blob.core.windows.net/x")
        client.get("https://chat.mistral.ai/api/trpc/chat.last")
        assert _cookie_of(capture_requests, "https://evil.example/tracker.png") is None
        assert _cookie_of(
            capture_requests, "https://static-assets.mistralai.com/x.png"
        ) == "ory_session_x=secret"
        assert _cookie_of(
            capture_requests,
            "https://mistralaichatupprodswe.blob.core.windows.net/x",
        ) == "ory_session_x=secret"
        # les appels API de la plateforme ne changent pas de comportement
        assert _cookie_of(
            capture_requests, "https://chat.mistral.ai/api/trpc/chat.last"
        ) == "ory_session_x=secret"

    def test_session_facade_api_sans_cookie_pour_un_tiers(
        self, tmp_path, capture_requests
    ):
        cookies = _write_cookies(tmp_path, "mistral", "ory_session_x", "secret")
        service = MistralService(
            _FakeSession(),
            {"cookies_dir": str(cookies), "services": {"mistral": {"api": True}}},
        )
        assert service.session.fetch("https://evil.example/tracker.png")
        assert service.session.fetch("https://chat.mistral.ai/cdn-cgi/image/x")
        assert _cookie_of(capture_requests, "https://evil.example/tracker.png") is None
        assert _cookie_of(
            capture_requests, "https://chat.mistral.ai/cdn-cgi/image/x"
        ) == "ory_session_x=secret"
