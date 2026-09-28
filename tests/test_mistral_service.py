"""Tests de la collecte incrementale du fil Mistral (virtualisation)."""

from __future__ import annotations

from src.services.mistral import MistralService

RESET = "window.__aicvMistral = {rows"
FIND = "window.__aicvMistralScroller = el"
COLLECT = "acc.seq.push"
FINAL = "edges: acc.edges"
SCROLL_BOTTOM = "s.scrollTop = s.scrollHeight"
SCROLL_UP = "s.scrollTop = Math.max(0"

#: totals stables en haut -> arret apres `stable_rounds`
INFOS = [
    {"dom": 2, "total": 2, "top": 900, "height": 4000},
    {"dom": 2, "total": 3, "top": 400, "height": 4000},
    {"dom": 2, "total": 3, "top": 0, "height": 4000},
    {"dom": 2, "total": 3, "top": 0, "height": 4000},
    {"dom": 2, "total": 3, "top": 0, "height": 4000},
    {"dom": 2, "total": 3, "top": 0, "height": 4000},
]

ROWS = {
    "m1": '<div data-message-author-role="user" data-message-id="m1">un</div>',
    "m2": '<div data-message-author-role="assistant" data-message-id="m2">deux</div>',
    "m3": '<div data-message-author-role="user" data-message-id="m3">trois</div>',
}
EDGES = [["m2", "m3"], ["m1", "m2"]]
SEQ = ["m2", "m3", "m1"]


class FakeSession:
    def __init__(self, infos=None):
        self._infos = list(infos or INFOS)
        self.scroll_bottom_calls = 0
        self.scroll_up_calls = 0

    def eval_body(self, body):
        if RESET in body:
            return 0
        if FIND in body:
            return {"client": 800, "height": 4000, "top": 0}
        if COLLECT in body:
            return self._infos.pop(0) if self._infos else {
                "dom": 2, "total": 3, "top": 0, "height": 4000,
            }
        if FINAL in body:
            return {"rows": ROWS, "edges": EDGES, "seq": SEQ}
        if SCROLL_BOTTOM in body:
            self.scroll_bottom_calls += 1
            return 0
        if SCROLL_UP in body:
            self.scroll_up_calls += 1
            return 0
        return None

    def wait_ms(self, ms):
        pass


def _service(session):
    return MistralService(session, {"scroll": {"stable_rounds": 3}})


def test_collecte_reconstitue_l_ordre_et_termine_en_haut():
    session = FakeSession()
    html = _service(session)._collect_conversation()
    assert html.count("data-message-author-role") == len(ROWS)
    assert html.index('data-message-id="m1"') < html.index('data-message-id="m2"')
    assert html.index('data-message-id="m2"') < html.index('data-message-id="m3"')
    assert session.scroll_bottom_calls >= 1
    assert session.scroll_up_calls >= 1


def test_collecte_absente_si_pas_de_conteneur():
    session = FakeSession()
    session.eval_body = lambda body: None
    assert _service(session)._collect_conversation() == ""
