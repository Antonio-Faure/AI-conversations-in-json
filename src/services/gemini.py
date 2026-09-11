"""Scraper Gemini / Google (gemini.google.com) via Playwright.

Gemini lazy-loade l'historique complet derriere un bouton "Show all" dans la
sidebar : on le clique si present avant de scroller.
"""

from __future__ import annotations

from ..parsers.gemini import GeminiParser
from ..schema import ConversationRef
from .base import BaseService


class GeminiService(BaseService):
    name = "gemini"
    home_url = "https://gemini.google.com/app"

    sidebar_scroll_selectors = (
        "conversation-history",
        "mat-nav-list",
        "aside mat-nav-list",
        "aside",
    )
    sidebar_ready_selectors = (
        "conversation-history a[href*='/app/']",
        "a[href*='/app/']",
    )
    #: sidebar demarree repliee : bouton "Ouvrir la barre laterale" / "Open sidebar"
    sidebar_open_selectors = (
        "button[aria-label*='barre latérale' i]",
        "button[aria-label*='sidebar' i]",
    )
    #: ouvre la liste complete des conversations (sinon seul "Recent" est visible)
    expand_selectors = (
        "[data-test-id='history-show-all']",
        "button:has-text('Show all')",
        "button:has-text('Show more')",
        "button:has-text('Tout afficher')",
        "button:has-text('Tout afficher plus')",
    )
    login_url_parts = ("accounts.google.com", "service.login", "signin")
    # NB: pas de selecteur sur a[href*='accounts.google.com'] : quand on est
    # connecte, le menu du compte (SignOutOptions) matche et fait un faux
    # positif. La redirection vers accounts.google.com suffit (URL).
    login_selectors = (
        "button:has-text('Sign in')",
        "input[type='password']",
    )

    def build_parser(self) -> GeminiParser:
        return GeminiParser()

    def after_sidebar_open(self) -> None:
        """Ouvre la sidebar (elle peut demarrer repliee) puis la liste complete."""
        for sel in self.sidebar_open_selectors:
            if self.session.click_if_present(sel, timeout_ms=1500):
                self.session.wait_ms(2500)
                break
        for sel in self.expand_selectors:
            if self.session.click_if_present(sel):
                self.session.wait_ms(1200)
                return

    def conversation_url(self, ref: ConversationRef) -> str:
        return ref.url or f"https://gemini.google.com/app/{ref.id}"
