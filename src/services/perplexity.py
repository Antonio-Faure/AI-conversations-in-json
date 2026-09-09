"""Scraper Perplexity (perplexity.ai) via Playwright."""

from __future__ import annotations

from typing import Any, Dict

from ..parsers.perplexity import PerplexityParser
from ..schema import ConversationRef
from .base import BaseService


class PerplexityService(BaseService):
    name = "perplexity"
    home_url = "https://www.perplexity.ai/"

    sidebar_scroll_selectors = (
        "aside nav",
        "[data-testid='history']",
        "aside div.overflow-y-auto",
        "aside",
    )
    sidebar_ready_selectors = (
        "aside a[href*='/search/']",
        "a[href*='/search/']",
    )
    login_url_parts = ("perplexity.ai/.auth", "vercel", "sign-in", "login")
    login_selectors = (
        "button:has-text('Sign in')",
        "a[href*='login']",
        "input[type='password']",
    )

    def build_parser(self) -> PerplexityParser:
        return PerplexityParser()

    def conversation_url(self, ref: ConversationRef) -> str:
        return ref.url or f"https://www.perplexity.ai/search/{ref.id}"

    def extract_extras(self, ref: ConversationRef) -> Dict[str, Any]:
        extra: Dict[str, Any] = {}
        if ref.title:
            extra["title_hint"] = ref.title
        return extra
