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
