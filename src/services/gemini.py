"""Scraper Gemini / Google (gemini.google.com) via Playwright.

Gemini lazy-loade l'historique complet derriere un bouton "Show all" dans la
sidebar : on le clique si present avant de scroller.
"""

from __future__ import annotations

from typing import Any, Dict

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
    #: ouvre la liste complete des conversations (sinon seul "Recent" est visible)
    expand_selectors = (
        "[data-test-id='history-show-all']",
        "button:has-text('Show all')",
        "button:has-text('Show more')",
    )
    login_url_parts = ("accounts.google.com", "service.login", "signin")
    login_selectors = (
        "a[href*='accounts.google.com']",
        "button:has-text('Sign in')",
        "input[type='password']",
    )

    def build_parser(self) -> GeminiParser:
        return GeminiParser()

    def after_sidebar_open(self) -> None:
        """Ouvre la liste complete (sinon seul le groupe 'Recent' est visible)."""
        for sel in self.expand_selectors:
            if self.session.click_if_present(sel):
                self.session.page.wait_for_timeout(1200)
                return

    def conversation_url(self, ref: ConversationRef) -> str:
        return ref.url or f"https://gemini.google.com/app/{ref.id}"

    def extract_extras(self, ref: ConversationRef) -> Dict[str, Any]:
        extra: Dict[str, Any] = {}
        if ref.title:
            extra["title_hint"] = ref.title
        return extra
