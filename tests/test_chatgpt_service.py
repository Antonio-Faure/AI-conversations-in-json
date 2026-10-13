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


class VersionsSession(FakeSession):
    """Session factice : dialogue « Versions » avec deux versions."""

    def __init__(self):
        super().__init__()
        self.clicks = []
        self.dialog_open = False
        self.opened_once = False
        self.position = 0
        self.fragments = [
            "<div data-chatgpt-selection-message-id='m1'>v1</div>",
            "<div data-chatgpt-selection-message-id='m2'>v2</div>",
        ]
        self.ids = ["m1", "m2"]

    def click_at(self, x, y):
        self.clicks.append((x, y))
        if not self.opened_once:
            self.opened_once = True
            self.dialog_open = True

    def eval_body(self, body):
        if "voir les versions" in body and "let n = 0" in body:
            return 1
        if "scrollIntoView" in body:
            return {"x": 10, "y": 20}
        if "dlg ? dlg.outerHTML : null" in body:
            if not self.dialog_open:
                return None
            return self.fragments[self.position]
        if "new RegExp(" in body:
            if "version suivante" in body and self.position < len(self.fragments) - 1:
                self.position += 1
                return {"x": 1, "y": 2}
            if "version précédente" in body and self.position > 0:
                self.position -= 1
                return {"x": 1, "y": 2}
            if "revenir" in body:
                self.dialog_open = False
                return {"x": 1, "y": 2}
            return None
        if "parseInt(m[1], 10)" in body:
            if not self.dialog_open:
                return None
            return {
                "ready": True,
                "label": self.position + 1,
                "prev": self.position > 0,
                "next": self.position < len(self.fragments) - 1,
                "ids": [self.ids[self.position]],
                "head": "v",
            }
        if "data-chatgpt-search-message-ids" in body:
            return ["m2", "u1"]
        return super().eval_body(body)


def test_capture_variants_dialogue_versions():
    """La capture parcourt le dialogue et ordonne les fragments."""
    session = VersionsSession()
    service = ChatGPTService(session, {"variants": True})
    captured = service._capture_variants()
    assert captured == {
        "m2": {
            "active": 1,
            "fragments": [session.fragments[0], session.fragments[1]],
        }
    }
    assert session.clicks, "des clics reels doivent etre envoyes"
    assert session.dialog_open is False, "le dialogue doit etre referme"


def test_capture_variants_sans_bouton():
    session = FakeSession()
    service = ChatGPTService(session, {"variants": True})
    assert service._capture_variants() == {}


class LostDialogSession(FakeSession):
    """Dialogue « Versions » perdu pendant la navigation (course de rendu).

    `_versions_wait_change` ne voit plus le dialogue alors que le fragment JS
    le trouve encore : la capture doit abandonner proprement (pas d'AttributeError).
    """

    def __init__(self):
        super().__init__()
        self.clicks = []
        self.dialog_gone = False

    def click_at(self, x, y):
        self.clicks.append((x, y))

    def eval_body(self, body):
        if "voir les versions" in body and "let n = 0" in body:
            return 1
        if "scrollIntoView" in body:
            return {"x": 10, "y": 20}
        if "parseInt(m[1], 10)" in body:  # _VERSIONS_DIALOG_JS
            if self.dialog_gone:
                return None
            return {
                "ready": True,
                "prev": True,
                "next": False,
                "ids": ["m1"],
                "head": "v1",
            }
        if "dlg ? dlg.outerHTML : null" in body:  # _VERSIONS_FRAGMENT_JS
            return "<div role='dialog'>Versions 2/2</div>"
        if "new RegExp(" in body:  # _VERSIONS_CLICK_JS
            if "previous version" in body:
                self.dialog_gone = True
            return {"x": 1, "y": 2}
        return super().eval_body(body)


def test_capture_versions_dialogue_perdu_abandonne_proprement():
    session = LostDialogSession()
    service = ChatGPTService(session, {"variants": True})
    assert service._capture_versions_at(0) is None
