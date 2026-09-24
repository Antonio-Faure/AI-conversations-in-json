"""Tests de la collecte incrementale du fil Perplexity (scroll molette)."""

from __future__ import annotations

import pytest

from src.parsers.base import ParseError
from src.schema import ConversationRef
from src.services.base import BaseService, ConversationUnavailableError, ScrapedPage
from src.services.perplexity import PerplexityService

RESET = "window.__aicvPpl = {rows"
FIND = "scrollable-container"
COLLECT = "acc.round -= 1"
FINAL = "items.push"
SCROLL_BOTTOM = "s.scrollTop = s.scrollHeight"
SCROLL_UP = "s.scrollTop = Math.max(0"

#: 3 remontees puis 4 tours stables en haut -> arret
INFOS = [
    {"count": 5, "fresh": 5, "top": 900},
    {"count": 8, "fresh": 3, "top": 400},
    {"count": 10, "fresh": 2, "top": 0},
    {"count": 10, "fresh": 0, "top": 0},
    {"count": 10, "fresh": 0, "top": 0},
    {"count": 10, "fresh": 0, "top": 0},
    {"count": 10, "fresh": 0, "top": 0},
]
ITEMS = [
    {"key": "user:1", "role": "user", "html": "<div>bonjour</div>", "pos": 1},
    {"key": "assistant:2", "role": "assistant", "html": "<div>salut</div>", "pos": 2},
]


class BaseSession:
    """Session factice partagee (sans molette par defaut)."""

    def __init__(self, infos, items):
        self._infos = list(infos)
        self.items = items
        self.scroll_up_calls = 0
        self.scroll_bottom_calls = 0

    def eval_body(self, body):
        if RESET in body:
            return 0
        if FIND in body and "cx:" in body:
            return {"tag": "DIV", "client": 800, "height": 8000, "top": 0,
                    "cx": 111, "cy": 222}
        if "at_bottom" in body:
            return {"top": 8000, "height": 8000, "client": 800,
                    "at_bottom": True}
        if COLLECT in body:
            return self._infos.pop(0) if self._infos else {"count": 10, "fresh": 0, "top": 0}
        if FINAL in body:
            return {"items": self.items}
        if SCROLL_BOTTOM in body:
            self.scroll_bottom_calls += 1
            return 0
        if SCROLL_UP in body:
            self.scroll_up_calls += 1
            return 0
        return None

    def wait_ms(self, ms):
        pass


class WheelSession(BaseSession):
    """Session factice : la molette est un vrai evenement (pas de scrollTop)."""

    def __init__(self, infos, items):
        super().__init__(infos, items)
        self.wheels = []

    def scroll_wheel(self, x, y, delta_y):
        self.wheels.append((x, y, delta_y))


def _service(session):
    return PerplexityService(session, {"scroll": {"stable_rounds": 3}})


def test_collecte_utilise_la_molette_et_termine_en_haut():
    session = WheelSession(INFOS, ITEMS)
    html = _service(session)._collect_thread()
    assert html.count("aicv-ppl-msg") == len(ITEMS)
    assert session.wheels, "la molette doit etre utilisee"
    assert any(w[2] < 0 for w in session.wheels), "au moins une remontee"
    assert session.wheels[0][2] > 0, "d'abord descendre pour charger les recents"
    assert session.wheels[0][:2] == (111, 222), "molette ciblee au centre du conteneur"
    assert session.scroll_up_calls == 0


def test_collecte_sans_molette_replie_sur_scrolltop():
    session = BaseSession(INFOS, ITEMS)
    html = _service(session)._collect_thread()
    assert html.count("aicv-ppl-msg") == len(ITEMS)
    assert session.scroll_bottom_calls >= 1
    assert session.scroll_up_calls >= 1


def test_collecte_absente_si_pas_de_conteneur():
    session = WheelSession(INFOS, ITEMS)
    session.eval_body = lambda body: None
    assert _service(session)._collect_thread() == ""


class _UrlSession(WheelSession):
    def __init__(self, url):
        super().__init__(INFOS, ITEMS)
        self._url = url

    def url(self):
        return self._url


REF = ConversationRef(
    service="perplexity",
    id="7ed85e4b",
    url="https://www.perplexity.ai/search/7ed85e4b",
    title="T",
)


def _raise_parse(self, ref):
    raise ParseError("aucun message reconnu")


def test_redirection_accueil_marque_indisponible(monkeypatch):
    session = _UrlSession("https://www.perplexity.ai/")
    monkeypatch.setattr(BaseService, "scrape_conversation", _raise_parse)
    with pytest.raises(ConversationUnavailableError):
        _service(session).scrape_conversation(REF)


def test_parse_error_normal_repropagé(monkeypatch):
    session = _UrlSession("https://www.perplexity.ai/search/7ed85e4b")
    monkeypatch.setattr(BaseService, "scrape_conversation", _raise_parse)
    with pytest.raises(ParseError):
        _service(session).scrape_conversation(REF)


# -- comptage des messages et fusion du HTML accumule --------------------------

#: la sous-chaine `data-workflow-final-text` revient dans les classes Tailwind
#: arbitraires des ancetres : un `html.count()` gonflait la fenetre brute.
_RAW_INFLATE = (
    '<html><head><title>Nom du fil</title></head><body>'
    '<div class="[&[data-workflow-final-text]+div:not(.mt-2)]:pt-0">'
    '<div class="group/user-bubble">q2</div>'
    '<div data-workflow-final-text=""><div data-renderer="lm">a2</div></div>'
    "</div></body></html>"
)
_MERGED = (
    "<html><body><div class=\"aicv-perplexity-thread\">"
    '<div class="aicv-ppl-msg" data-role="user">'
    '<div class="group/user-bubble">q1</div></div>'
    '<div class="aicv-ppl-msg" data-role="assistant">'
    '<div data-workflow-final-text=""><div data-renderer="lm">a1</div></div></div>'
    '<div class="aicv-ppl-msg" data-role="user">'
    '<div class="group/user-bubble">q2</div></div>'
    '<div class="aicv-ppl-msg" data-role="assistant">'
    '<div data-workflow-final-text=""><div data-renderer="lm">a2</div></div></div>'
    "</div></body></html>"
)


def test_message_count_compte_les_noeuds_pas_les_sous_chaines():
    # 2 messages malgre 3 occurrences de `data-workflow-final-text`
    assert PerplexityService._message_count(_RAW_INFLATE) == 2


def test_scrape_utilise_le_html_accumule_malgre_les_classes_tailwind(monkeypatch):
    session = WheelSession(INFOS, ITEMS)
    service = _service(session)

    def fake_scrape(self, ref):
        return ScrapedPage(html=_RAW_INFLATE, conversation_id=ref.id, url=ref.url)

    monkeypatch.setattr(BaseService, "scrape_conversation", fake_scrape)
    monkeypatch.setattr(service, "_collect_thread", lambda: _MERGED)
    page = service.scrape_conversation(REF)
    assert "aicv-ppl-msg" in page.html, "le HTML accumule doit etre retenu"
    # le titre de la page est recopie pour que le parser ne prenne pas un h1
    assert "<title>Nom du fil</title>" in page.html


def test_scrape_garde_la_page_si_l_accumulation_n_apporte_rien(monkeypatch):
    session = WheelSession(INFOS, ITEMS)
    service = _service(session)

    def fake_scrape(self, ref):
        return ScrapedPage(html=_RAW_INFLATE, conversation_id=ref.id, url=ref.url)

    monkeypatch.setattr(BaseService, "scrape_conversation", fake_scrape)
    monkeypatch.setattr(service, "_collect_thread", lambda: "")
    page = service.scrape_conversation(REF)
    assert page.html == _RAW_INFLATE
