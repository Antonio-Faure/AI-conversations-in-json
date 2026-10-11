"""Tests de la collecte des tours ChatGPT (deploiement du raisonnement, molette)."""

from __future__ import annotations

from src.schema import ConversationRef
from src.services.chatgpt import (
    TURN_SELECTOR,
    _COLLECT_TURNS_JS,
    ChatGPTService,
)

EXPANDED_TURN = (
    "<div data-testid='conversation-turn-1'>"
    "<div data-message-author-role='assistant' data-message-id='a1'>"
    "<button aria-expanded='true'>Thought for 12s</button>"
    "<div class='panel'><p>Raisonnement detaille.</p></div>"
    "<div class='markdown'><p>Reponse</p></div>"
    "</div></div>"
)


class FakeSession:
    """Session factice : compte les appels de deploiement des panneaux COT."""

    def __init__(self):
        self.expand_calls = 0
        self.collect_calls = 0

    def eval_body(self, body):
        if "window.__aicvChatgpt = {map" in body:
            return 0
        if "acc.scroller = el" in body:
            return {"tag": "DIV", "client": 800, "height": 4000}
        if 'aria-expanded="false"' in body:
            self.expand_calls += 1
            return 1 if self.expand_calls == 1 else 0
        if "acc.snaps.push(keys)" in body:
            self.collect_calls += 1
            return {
                "dom": 1,
                "fresh": 1,
                "total": 1,
                "top": 0,
                "client": 800,
                "height": 4000,
            }
        if "acc.snaps, map: acc.map" in body:
            return {"snaps": [["m:a1"]], "map": {"m:a1": EXPANDED_TURN}}
        if "s.scrollTop = s.scrollHeight" in body:
            return 0
        if "s.scrollTop = Math.max(0" in body:
            return 0
        return None

    def evaluate(self, script):
        return {"messages": {}}

    def wait_ms(self, ms):
        pass


class WheelSession(FakeSession):
    """Session factice disposant de la molette reelle (CDP)."""

    def __init__(self):
        super().__init__()
        self.wheel_calls = []

    def eval_body(self, body):
        # `_FIND_SCROLLER_JS` renseigne `acc.scroller` ; `_SCROLLER_RECT_JS`
        # lit `getBoundingClientRect` sur ce scroller.
        if "getBoundingClientRect" in body:
            return {"x": 500, "y": 400, "client": 800}
        if "acc.snaps.push(keys)" in body:
            self.collect_calls += 1
            top = 100 if self.collect_calls <= 5 else 0
            return {
                "dom": 1,
                "fresh": 1,
                "total": 1,
                "top": top,
                "client": 800,
                "height": 4000,
            }
        return super().eval_body(body)

    def scroll_wheel(self, x, y, delta_y):
        self.wheel_calls.append((x, y, delta_y))


def test_collecte_deploie_le_raisonnement_avant_capture():
    session = FakeSession()
    service = ChatGPTService(
        session, {"scroll": {"stable_rounds": 1, "top_stable_rounds": 1}}
    )
    fragments, _meta = service._collect_conversation_turns()
    assert session.expand_calls >= 1
    assert fragments and "Raisonnement detaille." in fragments[0]


def test_turn_selector_nouvelle_ui():
    assert TURN_SELECTOR == "[data-turn-key]"
    assert "data-turn-key" in _COLLECT_TURNS_JS


def test_collecte_utilise_la_molette_reelle():
    """La nouvelle UI annule `scrollTop` : l'historique se charge a la molette."""
    session = WheelSession()
    service = ChatGPTService(
        session, {"scroll": {"stable_rounds": 1, "top_stable_rounds": 1}}
    )
    fragments, _meta = service._collect_conversation_turns()
    assert session.wheel_calls, "la molette reelle doit etre utilisee"
    assert all(delta < 0 for _, _, delta in session.wheel_calls)
    assert fragments


class TimesSession(FakeSession):
    """Session factice exposant les timestamps du script bootstrap."""

    def evaluate(self, script):
        if "last_message_at" in script:
            return {
                "created_at": 1790169940.075207,
                "last_message_at": 1790170216.609211,
            }
        return {"messages": {}}


def test_extract_extras_timestamps_conversation():
    service = ChatGPTService(TimesSession(), {})
    ref = ConversationRef(
        service="chatgpt", id="x", url="https://chatgpt.com/c/x"
    )
    extra = service.extract_extras(ref)
    assert extra["created_at"] == 1790169940.075207
    assert extra["last_message_at"] == 1790170216.609211


class FetchTimesSession(FakeSession):
    """Session dont le script bootstrap a disparu : repli SSR via fetch."""

    def __init__(self):
        self.fetched = []

    def evaluate(self, script):
        if "last_message_at" in script:
            return {}
        return {"messages": {}}

    def fetch(self, url):
        self.fetched.append(url)
        html = (
            b'...\\"create_time\\",1779209172.134841,\\"update_time\\",'
            b'1779219159.105229,...'
        )
        return html, "text/html"


def test_extract_extras_timestamps_fallback_ssr():
    session = FetchTimesSession()
    service = ChatGPTService(session, {})
    ref = ConversationRef(
        service="chatgpt", id="x", url="https://chatgpt.com/c/x"
    )
    extra = service.extract_extras(ref)
    assert extra["created_at"] == 1779209172.134841
    assert extra["last_message_at"] == 1779219159.105229
    assert session.fetched == ["https://chatgpt.com/c/x"]

