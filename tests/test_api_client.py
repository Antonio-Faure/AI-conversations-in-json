"""Tests du client HTTP commun aux services API (`src/services/api_client.py`).

Verrouille le contrat extrait : en-tetes par defaut, backoff 429 parametrable
et traduction des statuts en exceptions du service (401/403 -> session
expiree, 429 epuise -> arret propre).
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request

import pytest

from src.http_client import DEFAULT_USER_AGENT
from src.services.api_client import CookieHttpClient, retry_after_s
from src.services.base import RateLimitedError, ServiceNotLoggedIn


class FakeResponse:
    """Reponse minimale pour `urllib.request.urlopen` simule."""

    def __init__(self, body: bytes = b"{}", content_type: str = "application/json"):
        self.status = 200
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _Client(CookieHttpClient):
    """Sous-classe minimale : en-tete `Content-Type` quand un corps est envoye."""

    service = "test"

    def _headers(
        self,
        accept: str,
        *,
        url: str | None = None,
        content_type: str | None = None,
    ) -> dict:
        headers = super()._headers(accept, url=url)
        if content_type:
            headers["Content-Type"] = content_type
        return headers


def test_retry_after_s():
    assert retry_after_s({"Retry-After": "12"}) == 12.0
    assert retry_after_s({"Retry-After": "bogus"}) is None
    assert retry_after_s({}) is None
    assert retry_after_s(None) is None


def test_entetes_par_defaut():
    headers = CookieHttpClient("a=b")._headers("application/json")
    assert headers["Cookie"] == "a=b"
    assert headers["User-Agent"] == DEFAULT_USER_AGENT
    assert headers["Accept"] == "application/json"
    assert headers["Accept-Language"] == "en-US,en;q=0.9"


def test_wait_429_exponentiel_plafonne(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    client = CookieHttpClient("a=b")
    assert client._wait_429(0, {}, "https://exemple/x") is True
    assert sleeps == [30.0]
    assert client._wait_429(5, {}, "https://exemple/x") is False  # 6 essais


def test_get_json_401_leve_service_not_logged_in(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ServiceNotLoggedIn):
        CookieHttpClient("a=b").get_json("https://exemple/x")


def test_get_json_429_persistant_leve_rate_limited(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    with pytest.raises(RateLimitedError):
        CookieHttpClient("a=b").get_json("https://exemple/x")


def test_get_json_reponse_non_json(monkeypatch):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout=None: FakeResponse(body=b"<html>"),
    )
    with pytest.raises(RuntimeError, match="reponse non-JSON"):
        CookieHttpClient("a=b").get_json("https://exemple/x")


def test_send_post_transmet_corps_et_en_tetes(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["method"] = request.get_method()
        seen["body"] = request.data
        seen["content_type"] = request.headers.get("Content-type")
        return FakeResponse(body=b'{"ok": 1}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    status, raw = _Client("a=b")._send(
        "https://exemple/x", data=b"{}", method="POST", content_type="application/json"
    )
    assert status == 200 and raw == b'{"ok": 1}'
    assert seen["method"] == "POST"
    assert seen["body"] == b"{}"
    assert seen["content_type"] == "application/json"


def test_fetch_rend_octets_et_content_type(monkeypatch):
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout=None: FakeResponse(
            body=b"png", content_type="image/png; charset=binary"
        ),
    )
    assert CookieHttpClient("a=b").fetch("https://exemple/x") == (b"png", "image/png")


def test_fetch_erreur_http_rend_none(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert CookieHttpClient("a=b").fetch("https://exemple/x") is None


def test_send_erreur_reseau_leve_runtime_error(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("dns")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="requete echouee"):
        CookieHttpClient("a=b").get("https://exemple/x")


class TestTransportNavigateur:
    """Transport alternatif (reseau du navigateur) : Cloudflare challenge urllib."""

    def test_send_utilise_le_transport(self):
        seen = {}

        def transport(url, headers, data, method, timeout_s):
            seen.update(
                url=url, headers=headers, data=data, method=method, timeout_s=timeout_s
            )
            return 200, b'{"ok": 1}', "application/json"

        client = CookieHttpClient("sid=1", user_agent="UA-test", transport=transport)
        assert client.get_json("https://chatgpt.com/backend-api/conversations") == {
            "ok": 1
        }
        assert seen["url"] == "https://chatgpt.com/backend-api/conversations"
        assert seen["headers"]["Cookie"] == "sid=1"
        assert seen["headers"]["User-Agent"] == "UA-test"
        assert seen["data"] is None and seen["method"] is None
        assert seen["timeout_s"] == 45

    def test_transport_403_leve_service_not_logged_in(self):
        client = CookieHttpClient(
            "sid=1",
            transport=lambda url, headers, data, method, timeout_s: (
                403,
                b"<html>challenge</html>",
                "text/html",
            ),
        )
        with pytest.raises(ServiceNotLoggedIn):
            client.get_json("https://chatgpt.com/backend-api/conversations")

    def test_transport_prime_sur_urllib(self, monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("urllib ne doit pas etre appele avec un transport")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        client = CookieHttpClient(
            "sid=1",
            transport=lambda url, headers, data, method, timeout_s: (200, b"{}", None),
        )
        assert client.get_json("https://exemple/x") == {}
