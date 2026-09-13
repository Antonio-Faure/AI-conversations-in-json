"""Parser DOM Mistral (chat.mistral.ai).

Structure observee (2026) :
  - tour de message : <div data-message-author-role="user|assistant"
                           data-message-id="..." data-message-version="0">
  - user      : <div class="select-text"><span class="whitespace-pre-wrap">...</span></div>
  - assistant : parties <div data-message-part-type="answer"> (a garder) et
                "reasoning" (reflexion interne, exclue) ; le texte est dans
                .markdown-container-style
  - titre : <title> de la page (ex: "Estimation tokens ...")
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..schema import Conversation
from .base import BaseParser, ParseError

#{ deux modes : /chat (chatbot) et /work (agentique). Les projets
# (/chat/projects/<id>) ne sont pas des conversations et sont exclus par le
# motif d'id (uuid juste apres /chat/ ou /work/).
ID_RE = re.compile(r"/(?:chat|work)/([0-9a-fA-F-]{8,})")


class MistralParser(BaseParser):
    service_name = "mistral"

    link_selectors = (
        "a[href^='/chat/']",
        "a[href^='/work/']",
        "a[href*='/chat/']",
        "a[href*='/work/']",
    )
    conversation_id_pattern = ID_RE

    message_selectors = ("div[data-message-author-role]",)

    TITLE_SELECTORS = (
        "header h1",
        "[data-testid='chat-title']",
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

        messages: List[Any] = []
        for container in soup.select("[data-message-author-role]"):
            role = (container.get("data-message-author-role") or "").lower()
            if role not in ("user", "assistant", "system", "tool"):
                role = "assistant"
            text = self._text_of_turn(container, role)
            if not text:
                continue
            messages.append(
                self.msg(
                    role,
                    text,
                    None,
                    {"tokens": None} if role == "assistant" else {},
                    message_id=str(container.get("data-message-id") or ""),
                )
            )

        if not messages:
            raise ParseError("mistral: aucun message extrait — session ou DOM modifie")

        title = self.extract_title(soup, *self.TITLE_SELECTORS)
        conv_id = conversation_id or extra.get("conversation_id")
        if not conv_id:
            m = ID_RE.search(html)
            conv_id = m.group(1) if m else "unknown"

        conv = Conversation(
            platform=self.service_name,
            conversation_id=str(conv_id),
            title=title or "Mistral conversation",
            messages=messages,
        )
        self.log_parse(conv, title=title)
        return self.check(conv)

    @classmethod
    def _text_of_turn(cls, container, role: str) -> str:
        if role == "assistant":
            parts = container.select("[data-message-part-type='answer']")
            texts = []
            for part in parts:
                text = cls.text_of(part)
                # Un tour reduit a un <hr> (reponse « affiche un separateur »)
                # n'a aucun texte : `get_text` le perd, ce qui ferait fusionner
                # deux tours user consecutifs (alternance du schema). On le rend
                # en regle Markdown.
                if part.find("hr") is not None:
                    text = f"{text}\n\n---".strip()
                texts.append(text)
            text = "\n\n".join(t for t in texts if t).strip()
            if text:
                return text
            # reponses rendues en "canvas"/document (sans partie answer)
            canvas = container.select_one("div[class*='pt-3'][class*='pb-4']")
            return cls.text_of(canvas).strip() if canvas is not None else ""
        # user : le corps du message (hors boutons/actions)
        body = container.select_one(".select-text") or container
        return cls.text_of(body)
