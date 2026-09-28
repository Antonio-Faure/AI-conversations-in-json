"""URL des conversations par plateforme (pour liens cliquables)."""

from __future__ import annotations

import re
from typing import Optional

URL_TEMPLATES = {
    "chatgpt": "https://chatgpt.com/c/{id}",
    "claude": "https://claude.ai/chat/{id}",
    "gemini": "https://gemini.google.com/app/{id}",
    "perplexity": "https://www.perplexity.ai/search/{id}",
    "grok": "https://grok.com/chat/{id}",
    "mistral": "https://chat.mistral.ai/chat/{id}",
}

_LINK_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"(\w[\w-]*)\s*=\s*[\"']([^\"']*)[\"']")


def conversation_url(
    platform: str, conversation_id: str, mode: Optional[str] = None
) -> str:
    """URL canonique reconstruite depuis la plateforme et l'id.

    `mode` (mistral) vaut "chat" ou "work" ; par defaut "chat".
    """
    if not conversation_id:
        return ""
    if platform == "mistral" and mode in ("chat", "work"):
        return f"https://chat.mistral.ai/{mode}/{conversation_id}"
    template = URL_TEMPLATES.get(platform)
    return template.format(id=conversation_id) if template else ""


def canonical_url_from_html(html: str) -> Optional[str]:
    """Extrait `<link rel="canonical" href="...">` d'une page HTML."""
    for tag in _LINK_RE.findall(html or ""):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        if attrs.get("rel", "").lower() == "canonical" and attrs.get("href"):
            return attrs["href"].strip()
    return None
