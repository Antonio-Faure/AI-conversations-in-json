"""Tests du service Grok (API REST cookies) : export sans HTML + payload brut."""

from __future__ import annotations

import json

from src.schema import ConversationRef
from src.services.grok import GrokService

META = {
    "conversationId": "g1",
    "title": "Conversation Grok",
    "createTime": "2026-05-01T09:00:00Z",
    "modifyTime": "2026-05-01T09:01:00Z",
}
RESPONSES = [
    {
        "responseId": "r1",
        "sender": "human",
        "message": "Bonjour",
        "createTime": "2026-05-01T09:00:00Z",
    },
    {
        "responseId": "r2",
        "sender": "assistant",
        "message": "Salut !",
        "createTime": "2026-05-01T09:01:00Z",
        "model": "grok-4",
    },
]


class FakeCookieClient:
    """Client HTTP factice : renvoie les pages JSON dans l'ordre."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get_json(self, url):
        self.calls.append(url)
        return self.pages.pop(0)


def make_service(tmp_path, pages):
    cookies = tmp_path / "cookies"
    cookies.mkdir()
    (cookies / "grok.json").write_text(
        json.dumps({"cookies": {"sso": "secret"}}), encoding="utf-8"
    )
    service = GrokService(None, {"cookies_dir": str(cookies)})
    service.http = FakeCookieClient(pages)
    return service


def test_export_sans_html_et_payload_brut(tmp_path):
    service = make_service(tmp_path, [META, {"responses": RESPONSES}])
    ref = ConversationRef(
        service="grok", id="g1", url="https://grok.com/chat/g1", title=META["title"]
    )
    conv, html = service.export_conversation_with_html(ref)
    assert html is None
    assert conv.conversation_id == "g1"
    assert conv.title == META["title"]
    assert [m.role for m in conv.messages] == ["user", "assistant"]
    assert conv.messages[0].texte == "Bonjour"
    assert conv.messages[1].texte == "Salut !"
    # le payload brut couvre les deux appels REST (conversation + responses)
    payload = service.take_raw_payload()
    assert payload == {"conversation": META, "responses": RESPONSES}
    # consomme : un export ulterieur ne le reecrit pas
    assert service.take_raw_payload() is None
