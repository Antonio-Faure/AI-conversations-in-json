"""Parser Grok (xAI) : construit la conversation depuis l'API REST.

Grok n'est pas scrapable au navigateur (Cloudflare bloque le Chromium pilote) ;
les donnees viennent de l'API authentifiee par cookies, passees en `extra` :

  extra = {
    "conversation": {conversationId, title, createTime, modifyTime, ...},
    "responses": [{responseId, message, sender, createTime, model, ...}, ...]
  }
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..schema import Conversation
from ..utils.cleanup import clean_grok_text
from .base import BaseParser, ParseError

ROLE_MAP = {"human": "user", "user": "user", "assistant": "assistant",
            "system": "system", "tool": "tool"}


def _rendered_files(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Cartes `rendered_file_card` d'une reponse (fichiers generes par Grok).

    Grok renvoie ces cartes dans `cardAttachmentsJson` (liste de chaines JSON)
    quand le tour produit un fichier (TXT, PDF, DOCX...) sans message texte.
    """
    cards: List[Dict[str, Any]] = []
    for item in resp.get("cardAttachmentsJson") or []:
        card: Any = item
        if isinstance(item, str):
            try:
                card = json.loads(item)
            except json.JSONDecodeError:
                continue
        if not isinstance(card, dict):
            continue
        if card.get("type") == "render_file" or card.get("cardType") == "rendered_file_card":
            cards.append(card)
    return cards


def _rendered_files_text(cards: List[Dict[str, Any]]) -> str:
    """Texte de substitution pour un tour assistant reduit a un fichier genere."""
    lines: List[str] = []
    for card in cards:
        name = str(card.get("file_name") or "fichier").strip()
        mime = str(card.get("mime_type") or card.get("content_type") or "").strip()
        size = card.get("file_size")
        details = ", ".join(
            part for part in (mime, f"{size} o" if isinstance(size, int) else "") if part
        )
        lines.append(f"Fichier généré : {name}" + (f" ({details})" if details else ""))
    return "\n".join(lines)


def _stream_error_message(resp: Dict[str, Any]) -> str:
    """Texte du premier `streamError` d'une reponse (quota, erreur de flux).

    Grok renvoie parfois une reponse assistant au `message` vide accompagnee de
    `streamErrors` (ex. « You've reached your usage limit »). La conserver
    evite de fusionner les deux messages `user` encadrants, ce qui decalerait
    tout l'appariement des tours.
    """
    errors = resp.get("streamErrors") or []
    if not errors:
        meta = resp.get("metadata") or {}
        errors = (meta.get("request_metadata") or {}).get("stream_errors") or []
    for err in errors:
        if isinstance(err, dict):
            message = err.get("message")
            if message and str(message).strip():
                return str(message).strip()
    return ""


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
            text = clean_grok_text(resp.get("message") or "").strip()
            cards = _rendered_files(resp)
            files_text = _rendered_files_text(cards)
            if not text:
                # reponse vide mais porteuse d'une erreur de flux : on la garde
                # pour preserver l'alternance des roles (cas du quota atteint).
                text = _stream_error_message(resp)
            if not text and files_text:
                # tour assistant reduit a un fichier genere (resultat.txt, PDF...)
                text = files_text
            if not text:
                role = ROLE_MAP.get(
                    str(resp.get("sender") or "assistant").lower(), "assistant"
                )
                if role != "assistant":
                    continue
                # tour assistant totalement vide : on le conserve vide pour ne pas
                # fusionner les deux messages `user` encadrants (alternance).
            elif files_text and files_text not in text:
                text = f"{text}\n\n{files_text}"
            text = text.strip()
            sender = str(resp.get("sender") or "assistant").lower()
            role = ROLE_MAP.get(sender, "assistant")
            model = resp.get("model") or None
            if role == "assistant" and model:
                models.append(str(model))
            metadata: Dict[str, Any] = {"tokens": None}
            if cards:
                metadata["generated_files"] = cards
            messages.append(
                self.msg(
                    role,
                    text,
                    resp.get("createTime"),
                    metadata,
                    message_id=str(resp.get("responseId") or ""),
                    model=str(model) if model else None,
                )
            )

        if not messages:
            raise ParseError("grok: aucun message exploitable")

        model = self.majority(models)
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
