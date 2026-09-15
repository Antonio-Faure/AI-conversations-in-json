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
import re
from typing import Any, Dict, List, Optional

from ..schema import Conversation
from ..utils.cleanup import clean_grok_text
from .base import BaseParser, ParseError

ROLE_MAP = {"human": "user", "user": "user", "assistant": "assistant",
            "system": "system", "tool": "tool"}

#: base publique des assets Grok (`users/.../content`, images, fichiers generes)
ASSETS_BASE = "https://assets.grok.com"


def _asset_url(key: str) -> str:
    """URL absolue d'un asset Grok a partir de sa cle (`users/.../content`)."""
    key = str(key or "").strip()
    if key.startswith(("http://", "https://")):
        return key
    return f"{ASSETS_BASE}/{key.lstrip('/')}"


def _md_label(name: str) -> str:
    """Echappe le libelle d'un lien/image markdown."""
    return str(name or "").replace("[", "\\[").replace("]", "\\]")


# balises de citation inline `<grok:render ... citation_card ...>`
_RENDER_RE = re.compile(
    r"<grok:render\b(?P<attrs>[^>]*)>(?P<body>.*?)</grok:render>",
    re.DOTALL | re.IGNORECASE,
)
_ATTR_RE = re.compile(r"(\w+)=[\"']([^\"']*)[\"']")
_CITATION_ID_RE = re.compile(
    r"<argument\b[^>]*name=[\"']citation_id[\"'][^>]*>(?P<id>.*?)</argument>",
    re.DOTALL | re.IGNORECASE,
)


def _citation_urls(resp: Dict[str, Any]) -> Dict[str, str]:
    """Carte `citation_card` -> URL, indexee par identifiant de carte."""
    urls: Dict[str, str] = {}
    for item in resp.get("cardAttachmentsJson") or []:
        card: Any = item
        if isinstance(item, str):
            try:
                card = json.loads(item)
            except json.JSONDecodeError:
                continue
        if not isinstance(card, dict):
            continue
        if card.get("cardType") == "citation_card" or card.get("type") == "render_inline_citation":
            card_id = card.get("id")
            url = card.get("url")
            if card_id and url:
                urls[str(card_id)] = str(url)
    return urls


def _render_citations(message: str, resp: Dict[str, Any]) -> str:
    """Remplace les citations inline par un lien markdown `[n](url)`.

    Grok insere des balises `<grok:render ... citation_card>` que
    `clean_grok_text` supprimerait avec la source. On les convertit avant.
    """
    urls = _citation_urls(resp)
    if not urls or "<grok:render" not in message:
        return message

    def repl(match: "re.Match[str]") -> str:
        attrs = dict(_ATTR_RE.findall(match.group("attrs")))
        url = urls.get(str(attrs.get("card_id") or ""))
        if not url:
            return match.group(0)
        ref = _CITATION_ID_RE.search(match.group("body"))
        label = ref.group("id").strip() if ref else "source"
        return f"[{_md_label(label)}]({url})"

    return _RENDER_RE.sub(repl, message)


def _attachment_items(resp: Dict[str, Any]) -> List[Dict[str, str]]:
    """Pieces jointes d'un tour (nom, type MIME, cle d'asset).

    Grok expose les fichiers joins dans `fileAttachmentAssetMetadata` (riche :
    nom + mime + cle) et `fileAttachmentsMetadata` (repli). Les cles sont
    relatives (`users/.../content`) et servies par `assets.grok.com`.
    """
    items: List[Dict[str, str]] = []
    for meta in resp.get("fileAttachmentAssetMetadata") or []:
        if not isinstance(meta, dict):
            continue
        key = str(meta.get("key") or "").strip()
        if not key:
            continue
        items.append({
            "name": str(meta.get("name") or "fichier").strip() or "fichier",
            "mime": str(meta.get("mimeType") or "").strip().lower(),
            "key": key,
        })
    if items:
        return items
    for meta in resp.get("fileAttachmentsMetadata") or []:
        if not isinstance(meta, dict):
            continue
        key = str(meta.get("fileUri") or "").strip()
        if not key:
            continue
        items.append({
            "name": str(meta.get("fileName") or "fichier").strip() or "fichier",
            "mime": str(meta.get("fileMimeType") or "").strip().lower(),
            "key": key,
        })
    return items


def _attachments_markdown(resp: Dict[str, Any]) -> str:
    """Markdown des pieces jointes : images en `![...]`, autres en `[...]`."""
    entries: List[str] = []
    for item in _attachment_items(resp):
        url = _asset_url(item["key"])
        label = _md_label(item["name"])
        if item["mime"].startswith("image/"):
            entries.append(f"![{label}]({url})")
        else:
            entries.append(f"[{label}]({url})")
    # images generees (le cas echeant) : URL deja absolues ou cles d'asset
    for url in resp.get("generatedImageUrls") or []:
        if isinstance(url, str) and url.strip():
            entries.append(f"![image]({_asset_url(url)})")
    seen: set = set()
    return "\n\n".join(e for e in entries if not (e in seen or seen.add(e)))


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
            text = clean_grok_text(_render_citations(resp.get("message") or "", resp)).strip()
            cards = _rendered_files(resp)
            files_text = _rendered_files_text(cards)
            attachments = _attachments_markdown(resp)
            if not text:
                # reponse vide mais porteuse d'une erreur de flux : on la garde
                # pour preserver l'alternance des roles (cas du quota atteint).
                text = _stream_error_message(resp)
            if not text and files_text:
                # tour assistant reduit a un fichier genere (resultat.txt, PDF...)
                text = files_text
                files_text = ""
            if not text and attachments:
                # tour sans texte reduit a une piece jointe (image, document...)
                text = attachments
                attachments = ""
            if not text:
                role = ROLE_MAP.get(
                    str(resp.get("sender") or "assistant").lower(), "assistant"
                )
                if role != "assistant":
                    continue
                # tour assistant totalement vide : on le conserve vide pour ne pas
                # fusionner les deux messages `user` encadrants (alternance).
            else:
                if files_text and files_text not in text:
                    text = f"{text}\n\n{files_text}"
                if attachments and attachments not in text:
                    text = f"{text}\n\n{attachments}"
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
