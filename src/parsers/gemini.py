"""Parser DOM Gemini (gemini.google.com/app).

Structure reelle observee (2024-2026, app Angular) :
  - sidebar : element <conversation-history> avec <a href="/app/<id>">
  - message user : <user-query> (contenu dans .user-query-content .content,
    ou <message-content> imbrique, ou rich-textarea .ql-editor)
  - reponse assistant : <model-response> <message-content class="model-response-text">
  - timestamps absents du DOM (derive de l'ordre des messages)
  - modele : label de reponse [data-test-id='response-model-label'] ou texte
    "Answered by Gemini 2.x" dans le pied de reponse.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..schema import Conversation
from .base import BaseParser, ParseError

CONV_ID_RE = re.compile(r"/app/([a-zA-Z0-9_-]{20,})")
MODEL_TEXT_RE = re.compile(
    r"(Answered by\s+|Model:?\s+|)(Gemini(?:\s+Ultra|\s+\d[\d.]*\s*\w*)?(?:\s+(?:Pro|Flash|Live|Thinking|Deep Research))?)",
    re.IGNORECASE,
)


class GeminiParser(BaseParser):
    service_name = "gemini"

    link_selectors = (
        "conversation-history a[href*='/app/']",
        "mat-nav-list a[href*='/app/']",
        "aside a[href*='/app/']",
        "a[href*='gemini.google.com/app/']",
        "a[href*='/app/']",
    )
    conversation_id_pattern = CONV_ID_RE

    message_selectors = (
        "user-query",
        "model-response",
        "message-content",
    )

    USER_SELECTORS = (
        "user-query .user-query-content .content",
        "user-query .content",
        "user-query message-content .model-response-text",
        "user-query message-content",
        "user-query rich-textarea .ql-editor",
        "user-query",
        "div.user-query",
    )
    ASSISTANT_SELECTORS = (
        "model-response message-content .model-response-text",
        "model-response message-content",
        "message-content.model-response-text",
        "model-response",
    )
    TITLE_SELECTORS = (
        "[data-test-id='conversation-title']",
        "chat-header .glimpse-title",
        "chat-title",
        "header h1",
    )
    FOOTER_TEXT_SELECTORS = (
        "[data-test-id='response-footer']",
        ".response-footer",
        "message-actions",
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

        user_nodes = self.select_all_any(soup, self.USER_SELECTORS[:6])
        assistant_nodes = self.select_all_any(soup, self.ASSISTANT_SELECTORS[:4])

        pairs = [(n, "user") for n in user_nodes] + [(n, "assistant") for n in assistant_nodes]
        # ordre du document
        positions = {}
        for el in soup.descendants:
            positions.setdefault(id(el), len(positions))
        pairs.sort(key=lambda p: positions.get(id(p[0]), 1 << 30))

        messages: List[Any] = []
        model: Optional[str] = extra.get("model")
        for node, role in pairs:
            if role == "user":
                content_el = self.select_first(node, self.CONTENT_OF_USER)
            else:
                content_el = self.select_first(node, self.CONTENT_OF_ASSISTANT)
            content = self.text_of(content_el or node)
            if not content:
                continue
            metadata: Dict[str, Any] = {}
            if role == "assistant":
                metadata["tokens"] = None
                if not model:
                    # le pied de reponse est un frere du noeud de texte :
                    # on sonde l'ancetre direct, puis la page entiere
                    model = self._model_from_footer(node.parent) or self._model_from_footer(soup)
            messages.append(self.msg(role, content, None, metadata))

        if not messages:
            raise ParseError(
                "gemini: aucun message extrait — session invalide ou DOM modifie"
            )

        title = self.extract_title(soup, *self.TITLE_SELECTORS) or extra.get("title_hint")
        if title:
            title = re.sub(r"\s+[—-]\s*Gemini\s*$", "", title)

        conv_id = conversation_id or extra.get("conversation_id")
        if not conv_id:
            m = CONV_ID_RE.search(html)
            conv_id = m.group(1) if m else "unknown"

        conv = Conversation(
            service=self.service_name,
            conversation_id=str(conv_id),
            title=title or "Gemini conversation",
            messages=messages,
            model=model or None,
        )
        self.log_parse(conv, model=model, title=title)
        return self.check(conv)

    CONTENT_OF_USER = (
        ".user-query-content .content",
        ".content",
        "rich-textarea .ql-editor",
        "message-content",
    )
    CONTENT_OF_ASSISTANT = (
        "message-content .model-response-text",
        ".model-response-text",
        "message-content",
    )

    def _model_from_footer(self, assistant_node) -> Optional[str]:
        for sel in self.FOOTER_TEXT_SELECTORS:
            try:
                footers = assistant_node.select(sel)
            except Exception:
                footers = []
            for footer in footers:
                text = footer.get_text(" ", strip=True)
                m = MODEL_TEXT_RE.search(text)
                if m:
                    name = m.group(2) or m.group(0)
                    return re.sub(r"\s+", " ", name).strip()
        return None
