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


class GrokService(BaseService):
    name = "grok"
    uses_browser = False
    home_url = API_BASE + "/"

    def __init__(self, session, config: Optional[Dict[str, Any]] = None):
        super().__init__(session, config)
        cookies_dir = Path((self.config or {}).get("cookies_dir") or "cookies")
        self.http = CookieClient("grok", cookies_dir)

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
