"""Tests des aides cookies (src/cookies.py)."""

from __future__ import annotations

from src.cookies import _domain_matches, cookie_names_match


class TestDomainMatches:
    def test_sous_domaine_et_parent(self):
        assert _domain_matches(".mistral.ai", "mistral.ai")
        assert _domain_matches("chat.mistral.ai", "mistral.ai")
        assert _domain_matches("chat.mistral.ai", "chat.mistral.ai")
        assert not _domain_matches("example.com", "mistral.ai")

    def test_vide_matche_tout(self):
        assert _domain_matches("nimporte.quoi", "")


class TestCookieNamesMatch:
    def test_exact(self):
        assert cookie_names_match(["session"], ["session"])

    def test_prefixe_suffixe_variable(self):
        assert cookie_names_match(["ory_session_coolcurranf83m3srkfl"], ["ory_session_"])

    def test_non_correspondant(self):
        assert not cookie_names_match(["__cf_bm", "csrftoken"], ["ory_session_"])


def test_capture_restreint_les_permissions(tmp_path, monkeypatch):
    """Les cookies d'auth sont ecrits en 0600 (pas lisibles par les autres)."""
    import json
    import stat

    from src import cookies as cookies_module

    path = cookies_module.cookies_path(tmp_path, "grok")
    path.write_text(
        json.dumps({"app_url": "https://grok.com/", "domain": "grok.com"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cookies_module, "_capture_via_browser", lambda *args, **kwargs: {"s": "v"}
    )
    assert cookies_module.capture_cookies("grok", tmp_path, {}) == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
