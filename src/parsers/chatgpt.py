"""Parser DOM ChatGPT (chatgpt.com).

Structure reelle observee (2024-2026):
  - sidebar : <nav aria-label="Chat history"> avec <a href="/c/<uuid>"> par conversation
  - messages : <div data-message-id="<uuid>" data-message-author-role="user|assistant"
               data-message-model-slug="gpt-4o"> <div class="markdown ...">...</div>
  - timestamps : absents du DOM visible -> injectes par le service via les donnees
    internes React (`memoizedProps.message.create_time`), passees en `extra`.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..schema import Conversation
from .base import BaseParser, ParseError

CONVERSATION_ID_RE = re.compile(r"/c/([0-9a-fA-F-]{16,}|[A-Za-z0-9_-]{16,})")


class ChatGPTParser(BaseParser):
    service_name = "chatgpt"

    link_selectors = (
        "nav[aria-label='Chat history'] a[href*='/c/']",
        "aside nav a[href*='/c/']",
        "[data-testid='sidebar'] a[href*='/c/']",
        "main nav a[href*='/c/']",
        "a[href^='/c/']",
    )
    conversation_id_pattern = CONVERSATION_ID_RE

    message_selectors = (
        "div[data-message-author-role='assistant']",
        "div[data-message-id]",
        "[data-testid^='conversation-turn']",
    )

    TURN_SELECTORS = (
        "[data-message-author-role][data-message-id]",
        "[data-message-author-role]",
        "div[data-message-id]",
        "[data-testid^='conversation-turn']",
        "main article[role='article']",
    )
    CONTENT_SELECTORS = (
        "[data-testid^='conversation-turn-text']",
        ".markdown",
        "div.whitespace-pre-wrap",
        ".prose",
        "p",
    )
    TITLE_SELECTORS = (
        "[data-testid='history-title']",
        "header [class*='truncate']",
        "header h1",
    )
    MODEL_HEADER_SELECTORS = (
        "[data-testid='model-switcher-dropdown-button']",
        "header button[aria-haspopup]",
    )

    def parse(
        self,
        html: str,
        *,
        conversation_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Conversation:
        extra = extra or {}
        soup = self.make_soup(html)
        message_meta: Dict[str, Dict[str, Any]] = extra.get("messages") or {}

        turns = self.select_all_any(soup, self.TURN_SELECTORS)
        turns = [t for t in turns if t.get("style") != "display: none;"]
        messages: List[Any] = []
        models: List[str] = []

        for turn in turns:
            role = self._role_of(turn)
            content_el = self.select_first(turn, self.CONTENT_SELECTORS)
            content = self.text_of(content_el)
            if not content:
                continue
            mid = turn.get("data-message-id")
            meta: Dict[str, Any] = {}
            timestamp = None
            slug = turn.get("data-message-model-slug")
            if mid and mid in message_meta:
                mm = message_meta[mid]
                timestamp = mm.get("time")
                slug = mm.get("model") or slug
            if role == "assistant":
                meta["tokens"] = None
                if slug:
                    models.append(slug)
            messages.append(
                self.msg(
                    role,
                    content,
                    timestamp,
                    meta,
                    message_id=str(mid or ""),
                    model=slug,
                )
            )

        # le <title> du DOM reste "ChatGPT" tant que la page n'a pas hydrate :
        # le titre de la sidebar (title_hint, via ref) est fiable, on le
        # privilegie ; le titre DOM n'est garde que s'il est specifique.
        dom_title = self.extract_title(soup, *self.TITLE_SELECTORS)
        generic = {"chatgpt", "new chat", "nouvelle conversation", ""}
        if dom_title and dom_title.strip().lower() in generic:
            dom_title = None
        title = extra.get("title_hint") or dom_title

        conv_id = conversation_id or extra.get("conversation_id")
        if not conv_id:
            m = CONVERSATION_ID_RE.search(html)
            conv_id = m.group(1) if m else "unknown"

        model = extra.get("model") or self.majority(models)
        if not model:
            header = self.select_first(soup, self.MODEL_HEADER_SELECTORS)
            text = self.text_of(header)
            if text and len(text) < 60:
                model = text

        if not messages:
            raise ParseError(
                "chatgpt: aucun message extrait — session invalide ou DOM modifie"
            )

        conv = Conversation(
            platform=self.service_name,
            conversation_id=str(conv_id),
            title=title or "ChatGPT conversation",
            messages=messages,
            started_at=extra.get("created_at"),
            model=model,
        )
        self.log_parse(conv, model=model, title=title)
        return self.check(conv)

    @staticmethod
    def _role_of(turn) -> str:
        attr = (turn.get("data-message-author-role") or "").lower()
        if attr in ("user", "assistant", "system", "tool"):
            return attr
        testid = (turn.get("data-testid") or "").lower()
        if "assistant" in testid:
            return "assistant"
        classes = " ".join(turn.get("class") or []).lower()
        if "mention-sticker" in classes or "bg-surface-2xs" in classes:
            return "user"
        return "assistant"
