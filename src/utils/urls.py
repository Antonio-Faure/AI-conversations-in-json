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
    "mistral": "https://chat.mistral.ai/work/{id}",
}

#: ancien prefixe Mistral (/chat a fusionne dans /work)
_MISTRAL_CHAT_RE = re.compile(r"(chat\.mistral\.ai)/chat(/|\?|#|$)")

_LINK_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"(\w[\w-]*)\s*=\s*[\"']([^\"']*)[\"']")


def conversation_url(
    platform: str, conversation_id: str, mode: Optional[str] = None
) -> str:
    """URL canonique reconstruite depuis la plateforme et l'id.

    `mode` (mistral) est obsolete : l'application /chat a fusionne dans /work,
    toutes les conversations vivent sous `/work/<id>`. Le parametre est
    conserve pour compatibilite et ignore.
    """
    if not conversation_id:
        return ""
    template = URL_TEMPLATES.get(platform)
    return template.format(id=conversation_id) if template else ""


def normalize_conversation_url(url: str) -> str:
    """Reecrit les anciennes URLs Mistral `/chat/<id>` en `/work/<id>`.

    Les autres URLs sont retournees inchangees.
    """
    if not url:
        return url
    return _MISTRAL_CHAT_RE.sub(r"\1/work\2", url)


def canonical_url_from_html(html: str) -> Optional[str]:
    """Extrait `<link rel="canonical" href="...">` d'une page HTML."""
    for tag in _LINK_RE.findall(html or ""):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        if attrs.get("rel", "").lower() == "canonical" and attrs.get("href"):
            return attrs["href"].strip()
    return None
