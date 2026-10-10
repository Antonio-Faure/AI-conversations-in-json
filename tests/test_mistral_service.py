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


class VariantSession:
    """Session factice : switcher de versions d'un message (editions)."""

    def __init__(self, htmls, active):
        self.htmls = dict(htmls)
        self.v = active

    def eval_body(self, body):
        if "if (prev || next)" in body:
            return [{"id": "m1", "v": self.v}]
        if "n.outerHTML" in body:
            return {"v": self.v, "html": self.htmls[self.v]}
        if "new RegExp(label" in body:
            import re as _re

            match = _re.search(r'const id = "([^"]+)", label = "([^"]+)"', body)
            label = match.group(2) if match else ""
            if label.startswith("suiv"):
                if self.v < max(self.htmls):
                    self.v += 1
                    return True
                return False
            if self.v > min(self.htmls):
                self.v -= 1
                return True
            return False
        return None

    def wait_ms(self, ms):
        pass


def test_capture_variants_parcourt_et_restaure():
    session = VariantSession({0: "<div>v0</div>", 1: "<div>v1</div>", 2: "<div>v2</div>"}, active=2)
    captured = MistralService(session, {})._capture_variants()
    assert captured == {
        "m1": {"active": 2, "fragments": ["<div>v0</div>", "<div>v1</div>", "<div>v2</div>"]}
    }
    assert session.v == 2  # version affichee restauree


def test_capture_variants_active_au_milieu():
    session = VariantSession({0: "h0", 1: "h1", 2: "h2"}, active=1)
    captured = MistralService(session, {})._capture_variants()
    assert captured["m1"]["active"] == 1
    assert captured["m1"]["fragments"] == ["h0", "h1", "h2"]
    assert session.v == 1


def test_capture_variants_sans_switcher():
    class NoVariantSession:
        def eval_body(self, body):
            return []

        def wait_ms(self, ms):
            pass

    assert MistralService(NoVariantSession(), {})._capture_variants() == {}
