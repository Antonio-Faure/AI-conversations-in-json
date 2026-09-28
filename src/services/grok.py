"""Scraper Grok (xAI) via l'API REST authentifiee par cookies.

Grok bloque le Chromium automatise (Cloudflare) mais accepte l'API HTTP avec
les cookies de session. Aucun navigateur n'est donc necessaire (`uses_browser`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from ..http_client import CookieAuthError, CookieClient
from ..parsers.grok import GrokParser
from ..schema import Conversation, ConversationRef
from ..utils.html_render import conversation_to_html
from .base import BaseService, ServiceNotLoggedIn

API_BASE = "https://grok.com"
CONVERSATIONS_URL = API_BASE + "/rest/app-chat/conversations?pageSize={size}"
RESPONSES_URL = API_BASE + "/rest/app-chat/conversations/{cid}/responses"
PAGE_SIZE = 50
MAX_PAGES = 200


def _guess_mime(data: bytes) -> Optional[str]:
    """Type MIME devine par signature (les assets Grok sont derriere cookies)."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    head = data[:256].lstrip().lower()
    if head.startswith(b"<svg") or head.startswith(b"<?xml"):
        return "image/svg+xml"
    return None


class _CookieFetchSession:
    """Session factice exposant `fetch` pour le pipeline d'images.

    Grok n'a pas de navigateur : `download_service_images` cherche
    `session.fetch` pour telecharger les images authentifiees. On l'implemente
    au-dessus du `CookieClient`, en deleguant le reste a la session d'origine.
    """

    def __init__(self, http: CookieClient, inner: Optional[Any] = None):
        self._http = http
        self._inner = inner

    def fetch(self, url: str):
        try:
            status, data = self._http.get(url, accept="image/*,*/*")
        except Exception:  # noqa: BLE001 (reseau : on laisse le repli HTTP)
            return None
        if status != 200 or not data:
            return None
        return data, _guess_mime(data)

    def wait_ms(self, ms: int) -> None:
        pass

    def close(self) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        inner = object.__getattribute__(self, "_inner")
        if inner is not None:
            return getattr(inner, name)
        raise AttributeError(name)


class GrokService(BaseService):
    name = "grok"
    uses_browser = False
    home_url = API_BASE + "/"

    def __init__(self, session, config: Optional[Dict[str, Any]] = None):
        super().__init__(session, config)
        cookies_dir = Path((self.config or {}).get("cookies_dir") or "cookies")
        self.http = CookieClient("grok", cookies_dir)
        self.session = _CookieFetchSession(self.http, session)

    def build_parser(self) -> GrokParser:
        return GrokParser()

    def conversation_url(self, ref: ConversationRef) -> str:
        return ref.url or f"{API_BASE}/chat/{ref.id}"

    # -- decouverte via l'API --------------------------------------------------

    def list_conversations(self, limit: Optional[int] = None) -> List[ConversationRef]:
        refs: Dict[str, ConversationRef] = {}
        token: Optional[str] = None
        try:
            for _ in range(MAX_PAGES):
                url = CONVERSATIONS_URL.format(size=PAGE_SIZE)
                if token:
                    url += f"&pageToken={token}"
                data = self.http.get_json(url)
                conversations = data.get("conversations") or []
                for conv in conversations:
                    cid = conv.get("conversationId")
                    if not cid:
                        continue
                    refs[str(cid)] = ConversationRef(
                        service=self.name,
                        id=str(cid),
                        url=f"{API_BASE}/chat/{cid}",
                        title=conv.get("title") or None,
                        raw={
                            "last_message_at": conv.get("modifyTime"),
                            "started_at": conv.get("createTime"),
                        },
                    )
                    if limit and len(refs) >= limit:
                        break
                if limit and len(refs) >= limit:
                    break
                token = data.get("nextPageToken") or None
                if not token or not conversations:
                    break
        except CookieAuthError as exc:
            raise ServiceNotLoggedIn(str(exc)) from exc
        return list(refs.values())

    # -- scraping --------------------------------------------------------------

    def export_conversation_with_html(self, ref: ConversationRef) -> tuple:
        meta = self.http.get_json(f"{API_BASE}/rest/app-chat/conversations/{ref.id}")
        data = self.http.get_json(RESPONSES_URL.format(cid=ref.id))
        conv: Conversation = self.parser.parse(
            "",
            conversation_id=ref.id,
            extra={"conversation": meta, "responses": data.get("responses") or []},
        )
        return conv, conversation_to_html(conv)
