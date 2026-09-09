"""Scraper Claude / Anthropic (claude.ai) via Playwright."""

from __future__ import annotations

from typing import Any, Dict

from ..parsers.claude import ClaudeParser
from ..schema import ConversationRef
from .base import BaseService


class ClaudeService(BaseService):
    name = "claude"
    home_url = "https://claude.ai/chats"

    sidebar_scroll_selectors = (
        "[data-testid='sidebar-chats-list']",
        "nav[aria-label='Chats']",
        "aside nav",
        "aside",
    )
    sidebar_ready_selectors = (
        "a[href*='/chat/']",
        "[data-testid='sidebar-chats-list'] a",
    )
    login_url_parts = ("claude.ai/__clerc", "claude.ai/login", "accounts.google.com", "auth.openai", "/email/")
    login_selectors = (
        "a[href*='login']",
        "button:has-text('Log in')",
        "input[type='password']",
    )

    def build_parser(self) -> ClaudeParser:
        return ClaudeParser()

    def conversation_url(self, ref: ConversationRef) -> str:
        return ref.url or f"https://claude.ai/chat/{ref.id}"

    def extract_extras(self, ref: ConversationRef) -> Dict[str, Any]:
        extra: Dict[str, Any] = {}
        if ref.title:
            extra["title_hint"] = ref.title
        # claude.ai met a jour document.title avec le titre du chat
        page_title = self.session.evaluate("() => document.title")
        if page_title:
            extra["title_hint"] = extra.get("title_hint") or str(page_title).strip()
        return extra
