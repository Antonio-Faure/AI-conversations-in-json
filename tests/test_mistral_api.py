"""Tests du chemin API Mistral (mapper tRPC + client HTTP + versions).

Le mapper est valide contre les exports DOM des etalons (verite terrain) : memes
messages (ids, roles, compte), memes extras (reasoning, outils, canvas, pieces
jointes, sources), hors divergences documentees :

- le markdown de l'API est plus fidele (le DOM perdait ``**gras**``/``$tex$``) ;
- les images distantes rendues en simple lien par le DOM restent des images ;
- les URL signees des pieces jointes sont fraiches (tokens differents) ;
- les timestamps de l'API sont exacts (UTC), le DOM lisait l'heure locale.
"""

from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest

from src.parsers.base import ParseError
from src.parsers.mistral import MistralParser
from src.schema import ConversationRef
from src.services.base import (
    ConversationUnavailableError,
    EmptyConversationError,
    ServiceNotLoggedIn,
)
from src.services.mistral import (
    API_CONVERSATION_PROCS,
    MistralService,
    _MistralHttpClient,
    _TrpcNotFound,
)

FIXTURES = Path(__file__).parent / "fixtures"

WORK_ID = "f0000000-0000-4000-8000-0dbcc445d9cd"
CHAT_ID = "f0000000-0000-4000-8000-20d2c740c82b"
VARIANTS_ID = "f0000000-0000-4000-8000-fe4cd87c85fd"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def work_payload():
    return load_fixture("mistral_api_conversation.json")


@pytest.fixture(scope="module")
def work_conversation(work_payload):
    return MistralParser().parse(
        "", conversation_id=WORK_ID, extra={"api": work_payload}
    )


@pytest.fixture(scope="module")
def chat_conversation():
    payload = load_fixture("mistral_api_chat.json")
    return MistralParser().parse("", conversation_id=CHAT_ID, extra={"api": payload})


@pytest.fixture(scope="module")
def variants_conversation():
    payload = load_fixture("mistral_api_variants.json")
    return MistralParser().parse(
        "", conversation_id=VARIANTS_ID, extra={"api": payload}
    )


# -- mapper API --------------------------------------------------------------


def test_parse_api_etalon(work_conversation):
    conv = work_conversation
    assert conv.platform == "mistral"
    assert conv.conversation_id == WORK_ID
    assert conv.title == "Interaction sans texte"
    assert conv.model == "GLM 5.3"
    assert len(conv.messages) == 26
    # alternance stricte user/assistant
    assert [m.role for m in conv.messages] == ["user", "assistant"] * 13
    # timestamps exacts de l'API (UTC), derives sur la conversation
    assert conv.started_at == "2026-09-24T12:27:39Z"
    assert conv.last_message_at == "2026-09-24T15:36:16Z"
    assert conv.messages[0].timestamp == "2026-09-24T12:27:39Z"


def test_parse_api_pieces_jointes(work_conversation):
    """Les `files` de l'API alimentent le markdown des pieces jointes."""
    msgs = work_conversation.messages
    assert msgs[0].texte.startswith("Extrais le texte de cette image.")
    assert "![image.png](https://" in msgs[0].texte
    assert "[document.pdf](https://" in msgs[2].texte
    assert "[audio.mp3](https://" in msgs[8].texte
    assert "[notes.txt](https://" in msgs[10].texte


def test_parse_api_raisonnement_et_outils(work_conversation):
    msgs = work_conversation.messages
    assert msgs[1].reasoning.startswith("Try read_file.")
    assert msgs[1].tools == ["Analyse d'image", "Lecture de fichier"]
    assert msgs[9].tools == ["Lecture de fichier"]
    assert msgs[15].tools == ["Recherche d'outil", "Génération d'image"]


def test_parse_api_canvas(work_conversation):
    msgs = work_conversation.messages
    assert msgs[13].artifacts == ["Rapport (text/markdown)"]
    assert msgs[13].tools == ["Compétence", "Canvas"]
    assert msgs[21].artifacts == ["notes.txt (text/markdown)"]
    assert msgs[21].tools == ["Canvas"]
    # le contenu du canvas ne pollue pas le texte de la reponse
    assert msgs[21].texte.startswith("J'ai créé le fichier notes.txt")


def test_parse_api_tableaux_et_listes(work_conversation):
    """Le markdown de l'API conserve tableaux et listes imbriquees."""
    msgs = work_conversation.messages
    assert "| Cle | Valeur |" in msgs[11].texte
    assert "| --- | --- |" in msgs[11].texte
    assert "- Élément 1\n  - Sous-élément 1a" in msgs[19].texte


def test_parse_api_fichier_et_image_generes(chat_conversation):
    """`file_reference` et `image_url` produisent les memes liens que le DOM."""
    msgs = chat_conversation.messages
    assert msgs[11].texte.startswith("[Download the CSV file](https://")
    assert msgs[23].texte.startswith("![image](https://")
    assert msgs[23].tools == ["Génération d'image"]
    # le DOM rendait cette image en lien : l'API garde le markdown image
    assert msgs[21].texte == "![test](https://placehold.co/120x60)"


def test_parse_api_sources_web():
    """Les `contentChunks` de recherche alimentent `sources` comme au DOM."""
    payload = {
        "chat": {"id": "c1", "generatedTitle": "Recherche"},
        "messages": {
            "items": [
                {
                    "id": "u1",
                    "role": "user",
                    "content": "cherche",
                    "turn": 0,
                    "createdAt": "2026-01-01T00:00:00Z",
                    "parentId": None,
                },
                {
                    "id": "a1",
                    "role": "assistant",
                    "content": "voila",
                    "turn": 0,
                    "createdAt": "2026-01-01T00:00:01Z",
                    "parentId": "u1",
                    "parentVersion": 0,
                    "contentChunks": [
                        {
                            "type": "tool_call",
                            "name": "web_search",
                            "publicResult": {
                                "results": [
                                    {"url": "https://a.example", "title": "Exemple"}
                                ]
                            },
                        },
                        {"type": "text", "text": "voila"},
                    ],
                },
            ]
        },
    }
    conv = MistralParser().parse("", extra={"api": payload})
    assert conv.messages[1].sources == ["Exemple — https://a.example"]
    assert conv.messages[1].tools == ["Recherche web"]


def test_parse_api_latex_et_code_preserves():
    """Le markdown de l'API passe tel quel (LaTeX, fences -> code_blocks)."""
    payload = {
        "chat": {"id": "c1", "generatedTitle": "Formatage"},
        "messages": {
            "items": [
                {
                    "id": "u1",
                    "role": "user",
                    "content": "formate",
                    "turn": 0,
                    "createdAt": "2026-01-01T00:00:00Z",
                    "parentId": None,
                },
                {
                    "id": "a1",
                    "role": "assistant",
                    "content": "",
                    "turn": 0,
                    "createdAt": "2026-01-01T00:00:01Z",
                    "parentId": "u1",
                    "parentVersion": 0,
                    "contentChunks": [
                        {
                            "type": "text",
                            "text": "$E=mc^2$\n\n```python\nprint('ok')\n```",
                        }
                    ],
                },
            ]
        },
    }
    conv = MistralParser().parse("", extra={"api": payload})
    assert conv.messages[1].texte.startswith("$E=mc^2$")
    assert conv.messages[1].code_blocks[0].language == "python"
    assert conv.messages[1].code_blocks[0].code == "print('ok')"


def test_parse_api_tour_canvas_seul():
    """Un tour reduit a un canvas affiche son contenu (repli DOM)."""
    payload = {
        "chat": {"id": "c1", "generatedTitle": "Canvas"},
        "messages": {
            "items": [
                {
                    "id": "u1",
                    "role": "user",
                    "content": "cree",
                    "turn": 0,
                    "createdAt": "2026-01-01T00:00:00Z",
                    "parentId": None,
                },
                {
                    "id": "a1",
                    "role": "assistant",
                    "content": "",
                    "turn": 0,
                    "createdAt": "2026-01-01T00:00:01Z",
                    "parentId": "u1",
                    "parentVersion": 0,
                    "contentChunks": [{"type": "canva", "id": "k1", "version": 0}],
                },
            ]
        },
        "canvases": {
            "items": [
                {
                    "id": "k1",
                    "version": 0,
                    "title": "Doc",
                    "type": "text/markdown",
                    "content": "# Doc\n\n- une puce",
                    "messageId": "a1",
                }
            ]
        },
    }
    conv = MistralParser().parse("", extra={"api": payload})
    assert conv.messages[1].texte == "# Doc\n\n- une puce"
    assert conv.messages[1].artifacts == ["Doc (text/markdown)"]


def test_parse_api_modele_legacy_et_alias_inconnu():
    """Sans `modelConfig` (conversations /chat), repli sur le 1er modele."""
    payload = {
        "chat": {"id": "c1", "generatedTitle": "Legacy"},
        "messages": {
            "items": [
                {
                    "id": "u1",
                    "role": "user",
                    "content": "q",
                    "turn": 0,
                    "createdAt": "2026-01-01T00:00:00Z",
                    "parentId": None,
                },
                {
                    "id": "a1",
                    "role": "assistant",
                    "content": "r",
                    "turn": 0,
                    "createdAt": "2026-01-01T00:00:01Z",
                    "parentId": "u1",
                    "parentVersion": 0,
                },
            ]
        },
        "model_display_map": {"mistral-large-latest": "Mistral Large"},
    }
    conv = MistralParser().parse("", extra={"api": payload})
    assert conv.model == "Mistral Large"
    # alias absent de la map : alias brut
    payload["chat"]["modelConfig"] = {"model_alias": "inconnu-1"}
    conv = MistralParser().parse("", extra={"api": payload})
    assert conv.model == "inconnu-1"


def test_parse_api_versions_edition(variants_conversation):
    """La chaine active exclut les branches ; les editions vont en variantes."""
    conv = variants_conversation
    assert len(conv.messages) == 8
    assert [m.role for m in conv.messages] == ["user", "assistant"] * 4
    edited = conv.messages[2]
    assert edited.role == "user"
    assert edited.texte == "Analyse le pdf pour me donner la page de ce manuel"
    assert len(edited.variants) == 3
    assert [v.kind for v in edited.variants] == ["edit"] * 3
    assert [v.active for v in edited.variants] == [False, False, True]
    assert edited.variants[0].texte == "Tu es con ? Analyse le pdf"
    # les reponses des versions non actives ne sont pas dans le fil
    assert conv.messages[3].message_id == "f0000000-0000-4000-8000-65ab3c4a5c9a"


def test_parse_api_versions_regeneration():
    """Plusieurs reponses de meme parent -> variantes `regeneration`."""
    payload = {
        "chat": {"id": "c1", "generatedTitle": "Regen"},
        "messages": {
            "items": [
                {
                    "id": "u1",
                    "role": "user",
                    "content": "q",
                    "turn": 0,
                    "createdAt": "2026-01-01T00:00:00Z",
                    "parentId": None,
                },
                {
                    "id": "a1",
                    "role": "assistant",
                    "content": "v0",
                    "turn": 0,
                    "createdAt": "2026-01-01T00:00:01Z",
                    "parentId": "u1",
                    "parentVersion": 0,
                    "version": 0,
                },
                {
                    "id": "a2",
                    "role": "assistant",
                    "content": "v1",
                    "turn": 0,
                    "createdAt": "2026-01-01T00:00:02Z",
                    "parentId": "u1",
                    "parentVersion": 0,
                    "version": 1,
                },
            ]
        },
    }
    conv = MistralParser().parse("", extra={"api": payload})
    assert len(conv.messages) == 2
    variants = conv.messages[1].variants
    assert [v.kind for v in variants] == ["regeneration", "regeneration"]
    assert [v.texte for v in variants] == ["v0", "v1"]
    assert [v.active for v in variants] == [False, True]


def test_parse_api_latex_normalise():
    """`\\(...\\)`/`\\[...\\]` de l'API -> `$...$`/`$$...$$` (comme le DOM)."""
    payload = {
        "chat": {"id": "c1", "generatedTitle": "Maths"},
        "messages": {
            "items": [
                {
                    "id": "u1",
                    "role": "user",
                    "content": "formule",
                    "turn": 0,
                    "createdAt": "2026-01-01T00:00:00Z",
                    "parentId": None,
                },
                {
                    "id": "a1",
                    "role": "assistant",
                    "content": "",
                    "turn": 0,
                    "createdAt": "2026-01-01T00:00:01Z",
                    "parentId": "u1",
                    "parentVersion": 0,
                    "contentChunks": [
                        {
                            "type": "text",
                            "text": (
                                "Voici la formule : \\( E = mc^2 \\).\n\n"
                                "\\[\n\\begin{cases}\nx + y = 1 \\\\\n2x - y = 3\n"
                                "\\end{cases}\n\\]"
                            ),
                        }
                    ],
                },
            ]
        },
    }
    conv = MistralParser().parse("", extra={"api": payload})
    texte = conv.messages[1].texte
    assert "Voici la formule : $E = mc^2$." in texte
    assert "$$\\begin{cases}" in texte
    assert "\\(" not in texte and "\\[" not in texte


def test_parse_api_titre_tableau_riche():
    """`custom_element` table-metadata : le titre precede le tableau (DOM)."""
    payload = {
        "chat": {"id": "c1", "generatedTitle": "Tableau"},
        "messages": {
            "items": [
                {
                    "id": "u1",
                    "role": "user",
                    "content": "tableau",
                    "turn": 0,
                    "createdAt": "2026-01-01T00:00:00Z",
                    "parentId": None,
                },
                {
                    "id": "a1",
                    "role": "assistant",
                    "content": "",
                    "turn": 0,
                    "createdAt": "2026-01-01T00:00:01Z",
                    "parentId": "u1",
                    "parentVersion": 0,
                    "contentChunks": [
                        {
                            "type": "custom_element",
                            "element": {
                                "type": "table-metadata",
                                "attributes": {"title": "Exemple de tableau"},
                            },
                        },
                        {
                            "type": "text",
                            "text": "| Nom | Âge |\n| --- | --- |\n| Alice | 25 |",
                        },
                    ],
                },
            ]
        },
    }
    conv = MistralParser().parse("", extra={"api": payload})
    assert conv.messages[1].texte == (
        "Exemple de tableau\n\n| Nom | Âge |\n| --- | --- |\n| Alice | 25 |"
    )


def test_parse_api_payload_vide():
    with pytest.raises(ParseError):
        MistralParser().parse("", extra={"api": {"messages": {"items": []}}})
    with pytest.raises(ParseError):
        MistralParser().parse("", extra={"api": {"messages": {"items": [{}]}}})


def test_parse_api_ordre_par_parente():
    """L'ordre vient des liens de parente, pas de l'ordre de la reponse."""
    payload = {
        "chat": {"id": "c1", "generatedTitle": "Ordre"},
        "messages": {
            "items": [
                {  # ordre volontairement inverse (comme l'API : recent d'abord)
                    "id": "a1",
                    "role": "assistant",
                    "content": "r",
                    "createdAt": "2026-01-01T00:00:01Z",
                    "parentId": "u1",
                    "parentVersion": 0,
                },
                {
                    "id": "u1",
                    "role": "user",
                    "content": "q",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "parentId": None,
                },
            ]
        },
    }
    conv = MistralParser().parse("", extra={"api": payload})
    assert [m.role for m in conv.messages] == ["user", "assistant"]
    assert [m.message_id for m in conv.messages] == ["u1", "a1"]


# -- service -----------------------------------------------------------------


class FakeSession:
    """Session navigateur factice (le mode API ne doit pas la toucher)."""

    def __init__(self, fingerprint=None):
        self.fingerprint = fingerprint or {}
        self.calls = []

    def __getattr__(self, name):
        def _recorder(*args, **kwargs):
            self.calls.append(name)
            return None

        return _recorder


class FakeTrpcClient(_MistralHttpClient):
    """Client tRPC factice : reponses par procedure (ou NOT_FOUND)."""

    def __init__(self, responses=None, not_found=()):
        super().__init__("session=abc")
        self.responses = dict(responses or {})
        self.not_found = set(not_found)
        self.calls = []

    def call_batch(self, procedures, inputs):
        self.calls.append((tuple(procedures), list(inputs)))
        entries = []
        for procedure in procedures:
            if procedure in self.not_found:
                entries.append(
                    {
                        "error": {
                            "json": {
                                "message": "NOT_FOUND",
                                "code": -32004,
                                "data": {"code": "NOT_FOUND"},
                            }
                        }
                    }
                )
                continue
            value = self.responses.get(procedure)
            if callable(value):
                value = value()
            entries.append({"result": {"data": {"json": value}}})
        return entries


def _ref(cid: str = WORK_ID) -> ConversationRef:
    return ConversationRef(
        service="mistral", id=cid, url=f"https://chat.mistral.ai/work/{cid}"
    )


def test_api_activee_par_defaut():
    service = MistralService(FakeSession(), {"services": {"mistral": {}}})
    assert service.api_enabled is True
    assert MistralService(FakeSession(), {}).api_enabled is True


def test_api_desactivee_par_config():
    service = MistralService(
        FakeSession(), {"services": {"mistral": {"api": False}}}
    )
    assert service.api_enabled is False


def test_api_client_cookies_et_ua(tmp_path):
    (tmp_path / "mistral.json").write_text(
        json.dumps({"cookies": {"ory_session_x": "tok"}}), encoding="utf-8"
    )
    session = FakeSession(
        fingerprint={"user_agent": "UA-Profil/1", "lang": "fr-FR"}
    )
    service = MistralService(
        session,
        {"cookies_dir": str(tmp_path), "services": {"mistral": {"api": True}}},
    )
    client = service._api_client()
    assert client.cookie_header == "ory_session_x=tok"
    assert client.user_agent == "UA-Profil/1"
    assert client.accept_language == "fr-FR,fr;q=0.9"
    assert service._api_client() is client


def test_api_client_sans_cookies_leve_service_not_logged_in(tmp_path):
    service = MistralService(
        FakeSession(),
        {"cookies_dir": str(tmp_path), "services": {"mistral": {"api": True}}},
    )
    with pytest.raises(ServiceNotLoggedIn):
        service._api_client()


def test_trpc_url_encodage():
    url = _MistralHttpClient._trpc_url(
        ("chat.last",), [{"chatVisibility": "private"}]
    )
    assert url.startswith("https://chat.mistral.ai/api/trpc/chat.last?")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert query["batch"] == ["1"]
    assert json.loads(query["input"][0]) == {
        "0": {"json": {"chatVisibility": "private"}}
    }


def test_trpc_url_meta_superjson():
    """`cursor` est un Date superjson : le meta accompagne la valeur."""
    url = _MistralHttpClient._trpc_url(
        ("chat.last",),
        [({"cursor": "2026-01-01T00:00:00Z"}, {"values": {"cursor": ["Date"]}, "v": 1})],
    )
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert json.loads(query["input"][0]) == {
        "0": {
            "json": {"cursor": "2026-01-01T00:00:00Z"},
            "meta": {"values": {"cursor": ["Date"]}, "v": 1},
        }
    }


def test_entry_json_not_found():
    entry = {
        "error": {
            "json": {
                "message": "NOT_FOUND",
                "code": -32004,
                "data": {"code": "NOT_FOUND"},
            }
        }
    }
    with pytest.raises(_TrpcNotFound):
        _MistralHttpClient.entry_json(entry, "chat.byId")


def test_list_conversations_api_pagination():
    pages = [
        {
            "items": [
                {
                    "id": "c1",
                    "title": "Nouvelle discussion",
                    "generatedTitle": "Titre 1",
                    "userTitle": None,
                    "updatedAt": "2026-01-02T00:00:00Z",
                },
                {"id": "c2", "generatedTitle": "Titre 2"},
            ],
            "nextCursor": "2026-01-01T00:00:00Z",
        },
        {"items": [{"id": "c3", "generatedTitle": "Titre 3"}], "nextCursor": None},
    ]
    client = FakeTrpcClient({"chat.last": lambda: pages.pop(0)})
    service = MistralService(
        FakeSession(), {"services": {"mistral": {"api": True}}}
    )
    service._http = client
    refs = service.list_conversations()
    assert [ref.id for ref in refs] == ["c1", "c2", "c3"]
    assert refs[0].title == "Titre 1"
    assert refs[0].url.endswith("/work/c1")
    # le 2e appel porte le curseur de la 1re page (Date superjson)
    second = client.calls[1][1][0]
    assert isinstance(second, tuple)
    assert second[0]["cursor"] == "2026-01-01T00:00:00Z"
    assert second[1] == {"values": {"cursor": ["Date"]}, "v": 1}


def test_list_conversations_api_limit():
    pages = [
        {
            "items": [{"id": f"c{i}", "generatedTitle": f"T{i}"} for i in range(20)],
            "nextCursor": "cur",
        }
    ]
    client = FakeTrpcClient({"chat.last": lambda: pages.pop(0)})
    service = MistralService(
        FakeSession(), {"services": {"mistral": {"api": True}}}
    )
    service._http = client
    refs = service.list_conversations(limit=5)
    assert [ref.id for ref in refs] == ["c0", "c1", "c2", "c3", "c4"]
    assert len(client.calls) == 1


def test_export_conversation_api_payload_brut(work_payload):
    payload = dict(work_payload)
    client = FakeTrpcClient(
        {
            "message.all": payload["messages"],
            "chat.byId": payload["chat"],
            "canva.all": payload["canvases"],
            "settings.get": {"vibeWorkModelConfigs": []},
        }
    )
    service = MistralService(
        FakeSession(), {"services": {"mistral": {"api": True}}}
    )
    service._http = client
    conv, html = service.export_conversation_with_html(_ref())
    assert html is None
    assert conv.conversation_id == WORK_ID
    assert len(conv.messages) == 26
    assert conv.url == f"https://chat.mistral.ai/work/{WORK_ID}"
    raw = service.take_raw_payload()
    assert raw["chat"]["id"] == WORK_ID
    assert raw["messages"]["items"]
    # consomme : un second appel ne rejoue pas le payload
    assert service.take_raw_payload() is None
    # le batch de conversation est bien celui attendu
    assert client.calls[0][0] == API_CONVERSATION_PROCS


def test_export_conversation_api_not_found():
    client = FakeTrpcClient(not_found=("chat.byId",))
    service = MistralService(
        FakeSession(), {"services": {"mistral": {"api": True}}}
    )
    service._http = client
    with pytest.raises(ConversationUnavailableError):
        service.export_conversation_with_html(_ref())


def test_export_conversation_api_vide():
    client = FakeTrpcClient(
        {
            "message.all": {"items": []},
            "chat.byId": {"id": WORK_ID},
            "canva.all": {"items": []},
        }
    )
    service = MistralService(
        FakeSession(), {"services": {"mistral": {"api": True}}}
    )
    service._http = client
    with pytest.raises(EmptyConversationError):
        service.export_conversation_with_html(_ref())


def test_api_model_map_cache(monkeypatch):
    calls = {"n": 0}

    def settings():
        calls["n"] += 1
        return {
            "vibeWorkModelConfigs": {
                "value": [
                    {
                        "model_alias": "glm-5-latest-short",
                        "model_display_name": "GLM 5.3",
                    }
                ]
            }
        }

    client = FakeTrpcClient({"settings.get": settings})
    service = MistralService(
        FakeSession(), {"services": {"mistral": {"api": True}}}
    )
    service._http = client
    assert service._api_model_map() == {"glm-5-latest-short": "GLM 5.3"}
    assert service._api_model_map() == {"glm-5-latest-short": "GLM 5.3"}
    assert calls["n"] == 1


def test_screenshots_api_noop():
    service = MistralService(
        FakeSession(), {"services": {"mistral": {"api": True}}}
    )
    assert service.capture_message_screenshots(None, Path("/tmp/x")) == 0
