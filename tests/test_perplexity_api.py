"""Tests du chemin API Perplexity (mapper JSON + client HTTP + pagination).

Le mapper est valide contre l'export DOM de l'etalon
(`perplexity_api_dom_reference.json`, produit par le parseur DOM courant) : les
messages DOM se retrouvent tous dans la sortie API (a la normalisation pres),
hors divergences documentees (le DOM fusionne/avale des tours — le mapper API
est plus fidele, il garde le tour).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.parsers.base import ParseError
from src.parsers.perplexity import PerplexityParser
from src.schema import ConversationRef
from src.services.base import (
    BaseService,
    ConversationUnavailableError,
    EmptyConversationError,
    RateLimitedError,
    ServiceNotLoggedIn,
)
from src.services.perplexity import (
    PerplexityHttpError,
    PerplexityService,
    _PerplexityHttpClient,
)
from src.utils.etalon import conversation_metrics, normalize_for_match, regressions

FIXTURES = Path(__file__).parent / "fixtures"

#: url d'une piece jointe signee (fixture sanitisee : signature retiree)
IMAGE_ATTACHMENT_PREFIX = (
    "https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/images/"
)
TEXT_ATTACHMENT_PREFIX = (
    "https://ppl-ai-file-upload.s3.amazonaws.com/web/direct-files/attachments/"
)


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def api_payload():
    return load_fixture("perplexity_api_thread.json")


@pytest.fixture(scope="module")
def long_payload():
    return load_fixture("perplexity_api_thread_long.json")


@pytest.fixture(scope="module")
def api_conversation(api_payload):
    return PerplexityParser().parse(
        "",
        conversation_id="f0000000-0000-4000-8000-7d3b48b1fa63",
        extra={"api": api_payload},
    )


@pytest.fixture(scope="module")
def long_conversation(long_payload):
    return PerplexityParser().parse(
        "",
        conversation_id="f0000000-0000-4000-8000-66c5457162a2",
        extra={"api": long_payload},
    )


# -- mapper API --------------------------------------------------------------


def test_parse_api_etalon(api_conversation):
    conv = api_conversation
    assert conv.platform == "perplexity"
    assert conv.conversation_id == "f0000000-0000-4000-8000-7d3b48b1fa63"
    assert conv.title.startswith("Crée un titre principal et deux sous-titres")
    assert conv.model == "turbo"
    assert conv.started_at == "2026-09-24T09:38:40Z"
    assert conv.last_message_at == "2026-09-24T17:50:19Z"
    # 7 tours : 7 questions, 6 reponses + 1 tour de workflow sans texte
    assert len(conv.messages) == 14
    assert [m.role for m in conv.messages] == ["user", "assistant"] * 7
    assert conv.messages[0].texte.startswith("Crée un titre principal")
    assert conv.messages[-1].texte == ""
    assert conv.messages[-1].reasoning == "Vérification des fichiers disponibles"


def test_parse_api_timestamps_et_identifiants(api_conversation):
    first = api_conversation.messages[0]
    assert first.timestamp == "2026-09-24T09:38:40Z"
    assert first.message_id == "f0000000-0000-4000-8000-5bc459910106"
    assert first.model == "turbo"
    assistant = api_conversation.messages[1]
    assert assistant.timestamp == "2026-09-24T09:38:41Z"
    assert assistant.message_id == first.message_id


def test_parse_api_markdown_etalon(api_conversation):
    msgs = api_conversation.messages
    assert msgs[1].texte == "# Titre principal\n\n## Sous-titre 1\n\n## Sous-titre 2"
    assert "**en gras**" in msgs[3].texte and "~~barré~~" in msgs[3].texte
    assert msgs[5].texte.startswith("- Premier élément")
    assert msgs[7].texte.startswith("1. Premier élément")
    assert msgs[9].texte.startswith("> Ceci est une citation")
    assert msgs[11].texte == "`print`"


def test_parse_api_latex_normalisee(long_conversation):
    msgs = long_conversation.messages
    # la formule inline brute \( ... \) devient $...$ (convention du depot)
    assert msgs[7].texte == "$E = mc^2$"
    display = msgs[9].texte
    assert display.startswith("$$\\begin{cases}")
    assert "\\(" not in display and "\\[" not in display
    assert display.endswith("$$")


def test_parse_api_pieces_jointes_utilisateur(api_conversation, long_conversation):
    # image jointe : markdown image avec l'URL signee (telechargee au scrape)
    last_user = api_conversation.messages[-2]
    assert last_user.role == "user"
    assert f"![image.jpg]({IMAGE_ATTACHMENT_PREFIX}" in last_user.texte
    # fichier joint : lien markdown `[nom](url)` (extension detectee)
    text_user = long_conversation.messages[16]
    assert text_user.role == "user"
    assert f"[notes.txt]({TEXT_ATTACHMENT_PREFIX}" in text_user.texte
    # les pieces jointes suivent le DOM (chips du tour) : les fichiers du
    # contexte (sources_payload des tours suivants) ne sont pas redupliques.
    positions = [
        index
        for index, message in enumerate(long_conversation.messages)
        if message.role == "user"
        and ("image.jpg" in message.texte or "notes.txt" in message.texte)
    ]
    assert positions == [4, 14, 16, 82]


def test_parse_api_reasoning_et_outils(api_conversation, long_conversation):
    # etapes de workflow -> reasoning ; la recherche web -> outil « Recherche »
    assert api_conversation.messages[1].tools == ["Recherche"]
    assert api_conversation.messages[-1].reasoning == (
        "Vérification des fichiers disponibles"
    )
    assert long_conversation.messages[1].tools == ["Recherche"]
    # execution de code (asset CODE_ASSET) -> outil « Code »
    code_answer = long_conversation.messages[101]
    assert "Code" in code_answer.tools


def test_parse_api_artifacts_et_sources(long_conversation):
    msgs = long_conversation.messages
    # fichier genere (CODE_FILE) -> artifacts (comme le DOM)
    yaml_message = next(m for m in msgs if m.artifacts)
    assert yaml_message.artifacts == ["notes.yaml"]
    assert yaml_message.texte == "Le fichier YAML a été généré."
    # sources web (titre — url), absentes du DOM mais exposees par l'API
    sourced = next(m for m in msgs if m.sources)
    assert sourced.sources[0] == (
        "Download Python | Python.org — https://www.python.org/downloads/"
    )


def test_parse_api_matches_dom_reference(api_conversation):
    """Tous les messages de l'export DOM se retrouvent dans la sortie API."""
    dom = load_fixture("perplexity_api_dom_reference.json")["messages"]
    api = api_conversation.messages
    assert len(dom) == 13
    # le DOM avale le tour de workflow sans texte : l'API en a un de plus
    assert len(api) == 14
    api_keys = [normalize_for_match(m.texte, 120) for m in api]
    for index, message in enumerate(dom):
        key = normalize_for_match(message["texte"], 120)
        if not key:
            continue
        assert any(key in candidate or candidate in key for candidate in api_keys if candidate), (
            f"message DOM {index} absent de la sortie API"
        )
    roles = [m.role for m in api]
    assert roles[:13] == [m["role"] for m in dom]


def test_parse_api_payload_vide():
    with pytest.raises(ParseError):
        PerplexityParser().parse("", extra={"api": {"entries": []}})


def test_metriques_api_sans_regression_sur_le_dom(api_conversation):
    """Aucune metrique de l'etalon ne chute (meme sans HTML)."""
    dom = load_fixture("perplexity_api_dom_reference.json")
    api_metrics = conversation_metrics(api_conversation.to_dict(validate=False))
    dom_metrics = conversation_metrics(dom)
    assert api_metrics["consecutive_roles"] == 0
    assert regressions(api_metrics, dom_metrics) == []
    # le DOM avale le tour de workflow : l'API en a un de plus
    assert api_metrics["messages"] == dom_metrics["messages"] + 1
    assert api_metrics["images"] == dom_metrics["images"]
    assert api_metrics["lists"] == dom_metrics["lists"]


# -- mapper API : forme « blocs markdown » (reponse diffusee) -----------------


@pytest.fixture(scope="module")
def blocks_payload():
    return load_fixture("perplexity_api_thread_blocks.json")


@pytest.fixture(scope="module")
def blocks_conversation(blocks_payload):
    return PerplexityParser().parse(
        "",
        conversation_id="f0000000-0000-4000-8000-34e0c27b401f",
        extra={"api": blocks_payload},
    )


def test_parse_api_markdown_blocks_reponses_non_vides(blocks_conversation):
    """Forme « blocs markdown » : les reponses assistant ne sont plus perdues.

    Regression : le mapper ne lisait que les items `WORKFLOW_ITEM_TEXT` du
    `workflow_block` ; les conversations ou la reponse vit dans des blocs
    `markdown_block` (`ask_text` final + segments `ask_text_N_markdown`)
    sortaient avec des messages assistant vides.
    """
    conv = blocks_conversation
    assert [m.role for m in conv.messages] == ["user", "assistant"] * 3
    assistants = [m for m in conv.messages if m.role == "assistant"]
    assert all((m.texte or "").strip() for m in assistants)
    assert all(len(m.texte) > 500 for m in assistants)
    assert conv.model == "turbo"
    assert conv.started_at == "2026-06-08T19:08:12Z"
    assert conv.last_message_at == "2026-06-08T19:12:12Z"


def test_parse_api_markdown_blocks_table_et_images(blocks_conversation):
    first = blocks_conversation.messages[1]
    # tableau markdown du segment « mesures » conserve tel quel
    assert "| Mesure | Détail |" in first.texte
    assert "|--------|" in first.texte
    # images inline du bloc `inline_entity_block` (`ask_text_4_images`)
    assert first.texte.count("![") == 2
    assert "ATE_Maison-retrait-argiles-evaporation-secheresse_800.jpg" in first.texte
    assert "ATE_Maison-gonflement-argiles-drainage_protections.jpg" in first.texte
    # raisonnement (workflow), sources et outils restent mappes
    assert first.reasoning
    assert first.sources
    assert first.tools == ["Recherche"]


def test_parse_api_markdown_blocks_sans_duplication(
    blocks_payload, blocks_conversation
):
    """Le bloc final `ask_text` prime : les segments ne sont pas re-concaténés."""
    entry = blocks_payload["entries"][0]
    final = next(
        block["markdown_block"]["answer"]
        for block in entry["blocks"]
        if block.get("intended_usage") == "ask_text"
    )
    assistant = blocks_conversation.messages[1]
    assert final.strip() in assistant.texte
    assert assistant.texte.count("### Avant la construction") == 1
    assert assistant.texte.count("### Aide financière possible") == 1


def test_parse_api_markdown_segments_sans_bloc_final():
    """Sans bloc final, les segments sont concatenes dans l'ordre du suffixe."""
    payload = {
        "entries": [
            {
                "uuid": "f0000000-0000-4000-8000-0000000000a1",
                "query_str": "Question ?",
                "blocks": [
                    {
                        "intended_usage": "ask_text_1_markdown",
                        "markdown_block": {
                            "progress": "DONE",
                            "chunks": ["seconde"],
                            "chunk_starting_offset": 0,
                            "answer": "seconde partie",
                        },
                    },
                    {
                        "intended_usage": "ask_text_0_markdown",
                        "markdown_block": {
                            "progress": "DONE",
                            "chunks": [],
                            "chunk_starting_offset": 0,
                            "answer": "premiere partie",
                        },
                    },
                ],
            }
        ]
    }
    conv = PerplexityParser().parse(
        "",
        conversation_id="f0000000-0000-4000-8000-0000000000b1",
        extra={"api": payload},
    )
    assert conv.messages[1].texte == "premiere partie\nseconde partie"


def test_parse_api_markdown_chunks_en_repli():
    """`answer` vide : les `chunks` (deltas de streaming) sont rejoues."""
    payload = {
        "entries": [
            {
                "uuid": "f0000000-0000-4000-8000-0000000000a2",
                "query_str": "Question ?",
                "blocks": [
                    {
                        "intended_usage": "ask_text",
                        "markdown_block": {
                            "progress": "DONE",
                            "chunks": ["Bonjour", " le", " monde"],
                            "chunk_starting_offset": 0,
                            "answer": "",
                        },
                    }
                ],
            }
        ]
    }
    conv = PerplexityParser().parse(
        "",
        conversation_id="f0000000-0000-4000-8000-0000000000b2",
        extra={"api": payload},
    )
    assert conv.messages[1].texte == "Bonjour le monde"


def test_parse_api_medias_images_et_videos():
    """Blocs de medias : images en `![nom](image)`, videos en lien."""
    payload = {
        "entries": [
            {
                "uuid": "f0000000-0000-4000-8000-0000000000a3",
                "query_str": "Question ?",
                "blocks": [
                    {
                        "intended_usage": "ask_text",
                        "markdown_block": {
                            "progress": "DONE",
                            "chunks": [],
                            "chunk_starting_offset": 0,
                            "answer": "Reponse.",
                        },
                    },
                    {
                        "intended_usage": "answer_media_items",
                        "inline_entity_block": {
                            "media_block": {
                                "media_items": [
                                    {
                                        "medium": "image",
                                        "image": "https://example.com/photo.jpg",
                                        "url": "https://example.com/article",
                                        "name": "Photo",
                                    },
                                    {
                                        "medium": "video",
                                        "image": "https://img.youtube.com/vi/x/hqdefault.jpg",
                                        "url": "https://www.youtube.com/watch?v=x",
                                        "name": "Video",
                                    },
                                ]
                            }
                        },
                    },
                ],
            }
        ]
    }
    conv = PerplexityParser().parse(
        "",
        conversation_id="f0000000-0000-4000-8000-0000000000b3",
        extra={"api": payload},
    )
    texte = conv.messages[1].texte
    assert "![Photo](https://example.com/photo.jpg)" in texte
    assert "[Video](https://www.youtube.com/watch?v=x)" in texte


# -- service : config et client ----------------------------------------------


class FakeSession:
    """Session factice : cookies + empreinte (sans navigateur)."""

    def __init__(self, cookies=None, fingerprint=None):
        self._cookies = cookies if cookies is not None else [
            {"name": "session-token", "value": "abc"},
            {"name": "pplx-account", "value": "acc"},
        ]
        self.fingerprint = fingerprint or {
            "user_agent": "UA-test",
            "lang": "fr-FR",
        }
        self.gotos = []

    def goto(self, url, **kwargs):
        self.gotos.append(url)

    def wait_ms(self, ms):
        pass

    def cookies(self):
        return list(self._cookies)


class FakeClient:
    """Client HTTP factice : renvoie des reponses JSON dans l'ordre."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get_json(self, url):
        self.calls.append(url)
        if not self.pages:
            raise AssertionError(f"appel inattendu: {url}")
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page

    def post_json(self, url, payload):
        self.calls.append(url)
        if not self.pages:
            raise AssertionError(f"appel inattendu: {url}")
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def _service(session=None, config=None):
    config = config or {}
    return PerplexityService(session or FakeSession(), config)


def test_api_activee_par_defaut():
    assert _service().api_enabled is True
    assert _service(config={"services": {"perplexity": {"api": True}}}).api_enabled is True


def test_dom_opt_in_force_le_repli():
    # repli DOM : `dom: true` ou `api: false`
    assert _service(config={"services": {"perplexity": {"dom": True}}}).api_enabled is False
    assert _service(config={"services": {"perplexity": {"api": False}}}).api_enabled is False


def test_dom_opt_in_utilise_le_chemin_navigateur(monkeypatch):
    """`dom: true` : decouverte et export repassent par le pipeline DOM."""
    service = _service(config={"services": {"perplexity": {"dom": True}}})
    called = {}

    def fake_list(self, limit=None):
        called["list"] = True
        return []

    def fake_export(self, ref):
        called["export"] = True
        return "conv", "<html></html>"

    monkeypatch.setattr(BaseService, "list_conversations", fake_list)
    monkeypatch.setattr(BaseService, "export_conversation_with_html", fake_export)
    assert service.api_enabled is False
    assert service.list_conversations() == []
    ref = ConversationRef(service="perplexity", id="x", url="https://www.perplexity.ai/search/x")
    assert service.export_conversation_with_html(ref) == ("conv", "<html></html>")
    assert called == {"list": True, "export": True}


def test_api_client_recupere_les_cookies():
    session = FakeSession()
    service = _service(session, {"services": {"perplexity": {}}})
    client = service._api_client()
    assert isinstance(client, _PerplexityHttpClient)
    assert "session-token=abc" in client.cookie_header
    assert client.user_agent == "UA-test"
    assert client.accept_language == "fr-FR,fr;q=0.9"
    assert session.gotos == ["https://www.perplexity.ai/"]
    # client memorise (pas de nouveau mint)
    assert service._api_client() is client


def test_api_client_sans_cookie_leve_service_not_logged_in():
    service = _service(FakeSession(cookies=[]))
    with pytest.raises(ServiceNotLoggedIn):
        service._api_client()


# -- service : decouverte et fil ---------------------------------------------


def _graphql_page(ids, has_next=False, cursor="CUR"):
    edges = [
        {
            "node": {
                "entryId": cid,
                "slug": cid,
                "name": f"Titre {cid}",
                "displayModel": {"modelID": "turbo"},
                "updatedAt": "2026-10-01T00:00:00Z",
            }
        }
        for cid in ids
    ]
    return {
        "data": {
            "viewer": {
                "recentGroup": {
                    "threads": {
                        "edges": edges,
                        "pageInfo": {
                            "hasNextPage": has_next,
                            "endCursor": cursor if has_next else None,
                        },
                    }
                }
            }
        }
    }


def test_list_conversations_api_pagination():
    service = _service()
    client = FakeClient(
        [_graphql_page(["c1", "c2"], has_next=True), _graphql_page(["c3"])]
    )
    service._http = client
    refs = service.list_conversations()
    assert [ref.id for ref in refs] == ["c1", "c2", "c3"]
    assert refs[0].url == "https://www.perplexity.ai/search/c1"
    assert refs[0].title == "Titre c1"
    assert service._thread_models["c1"] == "turbo"
    assert service._thread_updated["c1"] == "2026-10-01T00:00:00Z"
    assert len(client.calls) == 2


def test_list_conversations_api_limite():
    service = _service()
    service._http = FakeClient([_graphql_page(["c1", "c2", "c3"])])
    refs = service.list_conversations(limit=2)
    assert [ref.id for ref in refs] == ["c1", "c2"]


def test_list_conversations_api_structure_inattendue():
    service = _service()
    service._http = FakeClient([{"data": {}}])
    with pytest.raises(RuntimeError):
        service.list_conversations()


def test_thread_url_chronologique_et_curseur():
    url = PerplexityService._thread_url("cid", limit=20)
    assert "/rest/thread/cid?" in url
    assert "from_first=true" in url
    assert "limit=20" in url
    assert "cursor=" not in url
    assert "supported_block_use_cases=workflow_steps" in url
    with_cursor = PerplexityService._thread_url("cid", limit=20, cursor="C1")
    assert "cursor=C1" in with_cursor


def test_fetch_thread_api_pagine_par_curseur():
    page1 = {
        "entries": [{"uuid": "e1"}],
        "has_next_page": "True",
        "next_cursor": "CUR1",
        "thread_metadata": {"title": "T"},
    }
    page2 = {
        "entries": [{"uuid": "e2"}, {"uuid": "e1"}],
        "has_next_page": "False",
        "next_cursor": "None",
        "thread_metadata": {"title": "T"},
    }
    service = _service()
    client = FakeClient([page1, page2])
    data = service._fetch_thread_api(client, "cid")
    assert [e["uuid"] for e in data["entries"]] == ["e1", "e2"]
    assert "cursor=CUR1" in client.calls[1]
    assert data["thread_metadata"]["title"] == "T"


def test_fetch_thread_api_404_conversation_indisponible():
    service = _service()
    service._http = FakeClient([PerplexityHttpError(404, "u")])
    with pytest.raises(ConversationUnavailableError):
        service._fetch_thread_api(service._http, "cid")


def test_fetch_thread_api_401_session_perdue():
    service = _service()
    service._http = FakeClient([PerplexityHttpError(401, "u")])
    with pytest.raises(ServiceNotLoggedIn):
        service._fetch_thread_api(service._http, "cid")


def test_export_api_fil_vide_ignore():
    """Fil vide : `EmptyConversationError` (conversation ignoree), pas `failed`."""
    service = _service()
    service._http = FakeClient(
        [{"entries": [], "has_next_page": "False", "thread_metadata": {"title": "T"}}]
    )
    ref = ConversationRef(
        service="perplexity",
        id="cid-vide",
        url="https://www.perplexity.ai/search/cid-vide",
    )
    with pytest.raises(EmptyConversationError):
        service.export_conversation_with_html(ref)


def test_export_api_sans_html_avec_payload_brut(api_payload):
    service = _service()
    service._http = FakeClient([api_payload])
    ref = ConversationRef(
        service="perplexity",
        id="f0000000-0000-4000-8000-7d3b48b1fa63",
        url="https://www.perplexity.ai/search/f0000000-0000-4000-8000-7d3b48b1fa63",
    )
    conv, html = service.export_conversation_with_html(ref)
    assert len(conv.messages) == 14
    assert conv.url == ref.url
    assert html is None
    payload = service.take_raw_payload()
    assert payload["entries"] == api_payload["entries"]
    # consomme : un export ulterieur ne le reecrit pas
    assert service.take_raw_payload() is None
    assert service.capture_message_screenshots(conv, Path("/tmp/unused")) == 0


# -- client HTTP --------------------------------------------------------------


def test_http_client_reessaie_sur_429(monkeypatch):
    """429 (limite du forfait gratuit) : attente puis nouvel essai."""
    import time as time_module
    import urllib.error
    import urllib.request

    calls = {"n": 0}
    sleeps = []

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                request.full_url, 429, "Too Many Requests",
                {"Retry-After": "7"}, None,
            )
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time_module, "sleep", lambda s: sleeps.append(s))
    client = _PerplexityHttpClient("a=b")
    data = client.get_json("https://www.perplexity.ai/rest/thread/x")
    assert data == {"ok": True}
    assert calls["n"] == 2
    assert sleeps == [7.0]  # Retry-After respecte


def test_http_client_erreur_non_429_leve(monkeypatch):
    import urllib.error
    import urllib.request

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = _PerplexityHttpClient("a=b")
    with pytest.raises(PerplexityHttpError) as excinfo:
        client.get_json("https://www.perplexity.ai/rest/thread/x")
    assert excinfo.value.status == 403


def test_http_client_429_persistant_leve_rate_limited(monkeypatch):
    """429 epuise : `RateLimitedError` (arret propre), pas RuntimeError."""
    import time as time_module
    import urllib.error
    import urllib.request

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 429, "Too Many Requests", {}, None
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time_module, "sleep", lambda s: None)
    client = _PerplexityHttpClient("a=b")
    with pytest.raises(RateLimitedError):
        client.get_json("https://www.perplexity.ai/rest/thread/x")


def test_http_client_post_json(monkeypatch):
    import urllib.request

    seen = {}

    class FakeResponse:
        status = 200
        headers = {}

        def read(self):
            return b'{"data": 1}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        seen["method"] = request.get_method()
        seen["content_type"] = request.headers.get("Content-type")
        seen["body"] = request.data
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = _PerplexityHttpClient("a=b")
    data = client.post_json(
        "https://www.perplexity.ai/rest/perplexity_ask/graphql", {"operationName": "X"}
    )
    assert data == {"data": 1}
    assert seen["method"] == "POST"
    assert seen["content_type"] == "application/json"
    assert b"operationName" in seen["body"]
