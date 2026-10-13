"""Tests du login interactif (sans navigateur).

Le message affiche a l'utilisateur demande de fermer le navigateur quand la
connexion est terminee : cette fermeture doit sortir proprement (profil deja
sauvegarde), pas en erreur.
"""

from __future__ import annotations

import logging

import pytest
from playwright._impl._errors import TargetClosedError

from src.browser import BrowserSession
from src.browser_botasaurus import BotasaurusSession


class _Records(logging.Handler):
    """Capture les messages des loggers navigateur pour les assertions."""

    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.fixture()
def browser_logs():
    loggers = [logging.getLogger("aicv.browser"), logging.getLogger("aicv.browser_bota")]
    handler = _Records()
    # le conftest global coupe les logs `aicv` (CRITICAL) : on retablit le
    # niveau des loggers navigateur le temps du test pour capturer les messages.
    levels = {logger: logger.level for logger in loggers}
    for logger in loggers:
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
    try:
        yield handler.messages
    finally:
        for logger in loggers:
            logger.removeHandler(handler)
            logger.setLevel(levels[logger])


class _ClosedPage:
    """Page Playwright factice : le navigateur a ete ferme par l'utilisateur."""

    def __init__(self, closed_after: int = 0, url: str = "https://chatgpt.com/"):
        self.calls = 0
        self.closed_after = closed_after
        self.url_value = url
        self.goto_calls = []

    def goto(self, url, **kwargs):
        self.goto_calls.append(url)

    def wait_for_timeout(self, ms):
        self.calls += 1
        if self.calls > self.closed_after:
            raise TargetClosedError("Target page, context or browser has been closed")

    def is_closed(self):
        return self.calls > self.closed_after

    @property
    def url(self):
        # `page.url` est une propriete Playwright (pas une methode)
        return self.url_value

    def query_selector(self, selector):
        return None


class _LoginBrowserSession(BrowserSession):
    """BrowserSession sans Chromium : la page est un double de test."""

    def __init__(self, page):
        super().__init__(profile_dir=None, headless=False)
        self._test_page = page

    def start(self):
        return self

    @property
    def page(self):
        return self._test_page


def test_interactive_login_supporte_la_fermeture_du_navigateur(browser_logs):
    """Fermer le navigateur (consigne affichee) ne doit pas lever d'erreur."""
    session = _LoginBrowserSession(_ClosedPage(closed_after=0))
    session.interactive_login("chatgpt", "https://chatgpt.com/")
    assert any("ferme" in message.lower() for message in browser_logs)


def test_interactive_login_fermeture_pendant_l_attente(browser_logs):
    """Fermeture apres quelques tours (URL de login encore affichee)."""
    page = _ClosedPage(closed_after=1, url="https://auth.openai.com/log-in")
    session = _LoginBrowserSession(page)
    session.interactive_login("chatgpt", "https://chatgpt.com/")
    assert page.calls >= 2
    assert any("ferme" in message.lower() for message in browser_logs)


def test_interactive_login_sort_quand_la_connexion_est_detectee():
    """Cas nominal : URL de conversation + aucun selecteur de login -> sortie."""
    page = _ClosedPage(closed_after=10**9, url="https://chatgpt.com/c/abc")
    session = _LoginBrowserSession(page)
    session.interactive_login("chatgpt", "https://chatgpt.com/")
    assert page.calls == 1


class _ClosedBotasaurus(BotasaurusSession):
    """BotasaurusSession sans Chrome : le navigateur est deja ferme."""

    def __init__(self):
        super().__init__(profile_dir=None, headless=True)

    def start(self):
        return self

    def goto(self, url, **kwargs):
        return None

    def wait_ms(self, ms):
        pass

    def url(self):
        raise RuntimeError("browser closed")

    def _present(self, sel, wait_s=0.0):
        return False

    @property
    def driver(self):
        raise RuntimeError("browser closed")


def test_interactive_login_botasaurus_supporte_la_fermeture(browser_logs):
    """Meme fermeture propre cote botasaurus (`self.url()` peut lever)."""
    session = _ClosedBotasaurus()
    session.interactive_login("chatgpt", "https://chatgpt.com/")
    assert any("ferme" in message.lower() for message in browser_logs)
