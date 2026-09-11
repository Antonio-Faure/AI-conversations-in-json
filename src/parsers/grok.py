"""Parser Grok (xAI) : construit la conversation depuis l'API REST.

Grok n'est pas scrapable au navigateur (Cloudflare bloque le Chromium pilote) ;
les donnees viennent de l'API authentifiee par cookies, passees en `extra` :

  extra = {
    "conversation": {conversationId, title, createTime, modifyTime, ...},
    "responses": [{responseId, message, sender, createTime, model, ...}, ...]
  }
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..schema import Conversation
from .base import BaseParser, ParseError

ROLE_MAP = {"human": "user", "user": "user", "assistant": "assistant",
            "system": "system", "tool": "tool"}


class GrokParser(BaseParser):
    service_name = "grok"

    def parse(
        self,
        html: str,
        *,
        conversation_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Conversation:
        extra = extra or {}
        responses = extra.get("responses") or []
        conv_meta = extra.get("conversation") or {}
        if not responses:
            raise ParseError("grok: aucune reponse dans les donnees API")

        messages: List[Any] = []
        models: List[str] = []
        for resp in responses:
            text = (resp.get("message") or "").strip()
            if not text:
                continue
            sender = str(resp.get("sender") or "assistant").lower()
            role = ROLE_MAP.get(sender, "assistant")
            model = resp.get("model") or None
            if role == "assistant" and model:
                models.append(str(model))
            messages.append(
                self.msg(
                    role,
                    text,
                    resp.get("createTime"),
                    {"tokens": None},
                    message_id=str(resp.get("responseId") or ""),
                    model=str(model) if model else None,
                )
            )

        if not messages:
            raise ParseError("grok: aucun message exploitable")

        model = None
        if models:
            model = max(set(models), key=models.count)
        conv_id = (
            conversation_id
            or conv_meta.get("conversationId")
            or extra.get("conversation_id")
        )
        if not conv_id:
            raise ParseError("grok: conversation_id manquant")

        conv = Conversation(
            platform=self.service_name,
            conversation_id=str(conv_id),
            title=str(conv_meta.get("title") or extra.get("title_hint") or "Grok conversation"),
            messages=messages,
            model=model,
            started_at=conv_meta.get("createTime"),
            last_message_at=conv_meta.get("modifyTime"),
        )
        self.log_parse(conv, model=model, title=conv.title)
        return self.check(conv)
