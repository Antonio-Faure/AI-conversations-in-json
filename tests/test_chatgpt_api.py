"""Tests du chemin API ChatGPT (mapper JSON + client HTTP + pagination).

Le mapper est valide contre l'export DOM de l'etalon (`chatgpt_api_dom_reference.json`,
produit par le parseur DOM courant) : memes roles, meme texte (a la normalisation
pres) et memes extras, hors divergences documentees (images distantes perdues par
le DOM, libelle d'erreur d'outil propre au DOM).
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from src.parsers.chatgpt import API_FILE_DOWNLOAD_URL, ChatGPTParser
from src.schema import ConversationRef
from src.services.base import RateLimitedError, ServiceNotLoggedIn
from src.services.chatgpt import (
    API_PAGE_SIZE,
    ChatGPTService,
    _ChatGPTHttpClient,
)
from src.utils.images import download_service_images

FIXTURES = Path(__file__).parent / "fixtures"
#: l'API recupere les images markdown distantes que le DOM rend en placeholder
IMAGE_LOSS_INDICES = {11, 13}
#: libelle d'erreur d'outil uniquement present dans le DOM
DOM_ONLY_TOOLS_INDICES = {37}


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def api_payload():
    return load_fixture("chatgpt_api_conversation.json")


@pytest.fixture(scope="module")
def api_conversation(api_payload):
    return ChatGPTParser().parse(
        "", conversation_id=api_payload["conversation_id"], extra={"api": api_payload}
    )


def _norm(text: str) -> str:
    """Normalise les differences de rendu DOM (sauts durs, espaces)."""
    text = re.sub(r"[ \t]+\n", "\n", text or "")
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]+([.,;:!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


# -- mapper API --------------------------------------------------------------


def test_parse_api_etalon(api_conversation):
    conv = api_conversation
    assert conv.platform == "chatgpt"
    assert conv.conversation_id == "f0000000-0000-4000-8000-f2f43feb599e"
    assert conv.title == "Formatage de texte"
    assert conv.model == "gpt-5-6-mini"
    assert conv.started_at == "2026-09-23T13:25:40Z"
    assert conv.last_message_at == "2026-09-23T13:30:16Z"
    assert len(conv.messages) == 52
    # alternance stricte user/assistant
    assert [m.role for m in conv.messages] == ["user", "assistant"] * 26
    assert conv.messages[0].texte.startswith("Affiche uniquement trois mots")
    assert conv.messages[-1].texte.startswith("Je ne peux pas fournir")


def test_parse_api_timestamps_et_modeles(api_conversation):
    first = api_conversation.messages[0]
    assert first.timestamp == "2026-09-23T13:25:39Z"
    assert first.message_id == "f0000000-0000-4000-8000-74c4fb735c5d"
    assert first.model is None  # message utilisateur
    assistant = api_conversation.messages[1]
    assert assistant.model == "gpt-5-6"
    assert assistant.timestamp is not None


def test_parse_api_regroupe_raisonnement_et_outils(api_conversation):
    msgs = api_conversation.messages
    assert msgs[25].reasoning == "Worked for 5s"
    assert msgs[25].tools == ["Searched files and read the requested file"]
    assert msgs[27].reasoning == "Worked for 23s"
    assert msgs[27].tools == [
        "Searched files and PDFs, fixed JavaScript quotes, and extracted answer code",
        "Scanning PDF Pages for Hidden Codes",
    ]
    assert msgs[45].reasoning == "Worked for a couple of seconds"
    assert msgs[45].tools == ["Interacted with Files"]


def test_parse_api_filtre_les_tours_internes(api_conversation):
    # les 114 messages bruts se reduisent a 52 messages visibles ; aucun role
    # system/tool ni contenu interne ne doit apparaitre.
    roles = {m.role for m in api_conversation.messages}
    assert roles == {"user", "assistant"}
    assert not any(m.texte == "" and not m.reasoning for m in api_conversation.messages)


def test_parse_api_latex_et_code(api_conversation):
    msgs = api_conversation.messages
    assert msgs[15].texte.startswith("$E = mc^2$ signifie")
    assert msgs[17].texte.startswith("$$\\begin{cases}")
    assert "\\(" not in msgs[17].texte and "\\[" not in msgs[17].texte
    css = msgs[19]
    assert css.code_blocks and css.code_blocks[0].language == "css"
    assert "h1 {" in css.code_blocks[0].code


def test_parse_api_conserve_les_images_distantes(api_conversation):
    msgs = api_conversation.messages
    assert msgs[11].texte == "![test](https://placehold.co/120x60)"
    assert msgs[13].texte == "[![image](https://placehold.co/120x60)](https://openai.com)"


def test_parse_api_matches_dom_reference(api_conversation):
    """Le mapper API concorde avec l'export DOM (hors divergences connues)."""
    dom = load_fixture("chatgpt_api_dom_reference.json")["messages"]
    api = api_conversation.messages
    assert len(api) == len(dom) == 52
    for index, (a, b) in enumerate(zip(api, dom)):
        assert a.role == b["role"], index
        assert a.message_id == b["message_id"], index
        assert len(a.code_blocks) == len(b["code_blocks"]), index
        assert _norm(a.reasoning) == _norm(b["reasoning"]), index
        if index not in DOM_ONLY_TOOLS_INDICES:
            assert a.tools == b["tools"], index
        if index in IMAGE_LOSS_INDICES:
            assert b["texte"] == "" or "![" not in b["texte"]
            assert "placehold.co" in a.texte
        else:
            assert _norm(a.texte) == _norm(b["texte"]), index


def test_parse_api_sources():
    payload = {
        "conversation_id": "c1",
        "title": "Recherche",
        "messages": [
            {
                "id": "u1",
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": ["cherche"]},
            },
            {
                "id": "a1",
                "author": {"role": "assistant"},
                "recipient": "all",
                "metadata": {
                    "model_slug": "gpt-5-6",
                    "content_references": [
                        {"type": "url", "title": "Exemple", "url": "https://a.example"},
                        {
                            "type": "sources_footnote",
                            "sources": [{"title": "Autre", "url": "https://b.example"}],
                        },
                    ],
                    "citations": [
                        {"metadata": {"title": "Cite", "url": "https://c.example"}}
                    ],
                },
                "content": {"content_type": "text", "parts": ["voila"]},
            },
        ],
    }
    conv = ChatGPTParser().parse("", extra={"api": payload})
    assert conv.messages[1].sources == [
        "Exemple — https://a.example",
        "Autre — https://b.example",
        "Cite — https://c.example",
    ]


def test_parse_api_image_group_reference():
    """Les carrousels d'images web (`image_group`) sont remappes dans le texte."""
    marker = '\ue200image_group\ue202{"layout":"carousel"}'
    payload = {
        "conversation_id": "c1",
        "messages": [
            {
                "id": "u1",
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": ["montre des images"]},
            },
            {
                "id": "a1",
                "author": {"role": "assistant"},
                "recipient": "all",
                "metadata": {
                    "model_slug": "gpt-5-6",
                    "content_references": [
                        {
                            "type": "image_group",
                            "matched_text": marker,
                            "alt": (
                                "![Image](https://images.openai.com/a)  "
                                "![Image](https://images.openai.com/b)"
                            ),
                        }
                    ],
                },
                "content": {
                    "content_type": "text",
                    "parts": [f"Voici :\n\n{marker}\n\nfin"],
                },
            },
        ],
    }
    conv = ChatGPTParser().parse("", extra={"api": payload})
    texte = conv.messages[1].texte
    assert marker not in texte
    assert texte.count("![") == 2
    assert "https://images.openai.com/a" in texte
    assert texte.endswith("fin")


def test_parse_api_raisonnement_thoughts():
    """Un `thoughts` porteur de texte alimente `reasoning`, son resume `tools`."""
    payload = {
        "conversation_id": "c1",
        "messages": [
            {
                "id": "u1",
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": ["q"]},
            },
            {
                "id": "t1",
                "author": {"role": "assistant"},
                "content": {
                    "content_type": "thoughts",
                    "thoughts": [{"summary": "Cherche", "content": "Je reflechis."}],
                },
            },
            {
                "id": "a1",
                "author": {"role": "assistant"},
                "recipient": "all",
                "content": {"content_type": "text", "parts": ["r"]},
            },
        ],
    }
    conv = ChatGPTParser().parse("", extra={"api": payload})
    assert conv.messages[1].reasoning == "Je reflechis."
    assert conv.messages[1].tools == ["Cherche"]


def test_parse_api_recap_hide_all_ignore():
    payload = {
        "conversation_id": "c1",
        "messages": [
            {
                "id": "u1",
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": ["q"]},
            },
            {
                "id": "r1",
                "author": {"role": "assistant"},
                "content": {"content_type": "reasoning_recap", "content": "cache"},
                "metadata": {"reasoning_recap_type": "hide_all"},
            },
            {
                "id": "a1",
                "author": {"role": "assistant"},
                "recipient": "all",
                "content": {"content_type": "text", "parts": ["r"]},
            },
        ],
    }
    conv = ChatGPTParser().parse("", extra={"api": payload})
    assert conv.messages[1].reasoning == ""


def test_parse_api_tour_assistant_sans_texte():
    """Extras orphelins : tour assistant vide ajoute pour garder l'alternance."""
    payload = {
        "conversation_id": "c1",
        "messages": [
            {
                "id": "u1",
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": ["q"]},
            },
            {
                "id": "t1",
                "author": {"role": "assistant"},
                "content": {"content_type": "thoughts", "thoughts": [{"summary": "Etape"}]},
            },
        ],
    }
    conv = ChatGPTParser().parse("", extra={"api": payload})
    assert [m.role for m in conv.messages] == ["user", "assistant"]
    assert conv.messages[1].texte == ""
    assert conv.messages[1].tools == ["Etape"]


def test_parse_api_payload_vide():
    from src.parsers.base import ParseError

    with pytest.raises(ParseError):
        ChatGPTParser().parse("", extra={"api": {"messages": []}})


# -- multimodal (`multimodal_text` : voix + images) ---------------------------


@pytest.fixture(scope="module")
def multimodal_payload():
    return load_fixture("chatgpt_api_multimodal.json")


@pytest.fixture(scope="module")
def multimodal_conversation(multimodal_payload):
    return ChatGPTParser().parse(
        "",
        conversation_id=multimodal_payload["conversation_id"],
        extra={"api": multimodal_payload},
    )


def test_parse_api_multimodal_images_et_transcriptions(multimodal_conversation):
    """`multimodal_text` : texte + transcription, images vers l'endpoint API."""
    msgs = multimodal_conversation.messages
    assert [m.role for m in msgs] == [
        "user", "assistant", "user", "assistant", "user", "assistant",
    ]
    photo = API_FILE_DOWNLOAD_URL.format(fid="file_00000000aaaabbbb")
    assert msgs[0].texte == (
        f"Regarde cette photo de mon projet.\n\n![photo.png]({photo})"
    )
    assert msgs[1].texte == "Elle est très bien cadrée."
    # transcription nettoyee ; l'asset audio (non telechargeable) est ignore
    assert msgs[2].texte == "Et maintenant génère une image."
    # image generee rattachee au tour assistant, apres son texte (convention DOM)
    chat = API_FILE_DOWNLOAD_URL.format(fid="file_00000000ccccdddd")
    assert msgs[3].texte == (
        f"Voilà le chat astronaute.\n\n![Generated image: Un chat astronaute]({chat})"
    )
    assert msgs[4].texte == "Parfait, merci !"
    # image generee seule en fin de conversation : tour assistant conserve
    fusee = API_FILE_DOWNLOAD_URL.format(fid="file_00000000eeeeffff")
    assert msgs[5].texte == f"![Generated image: Une fusée]({fusee})"


def test_parse_api_multimodal_aucune_url_distante(multimodal_conversation):
    """Jamais d'URL distante ni de pointeur brut : endpoint API uniquement."""
    for message in multimodal_conversation.messages:
        assert "sediment://" not in message.texte
        assert "estuary" not in message.texte
        assert "file-service://" not in message.texte


def test_parse_api_image_generee_avant_message_utilisateur():
    """Image d'outil entre deux tours : tour assistant insere avant le user."""
    payload = {
        "conversation_id": "c1",
        "messages": [
            {
                "id": "u1",
                "author": {"role": "user"},
                "recipient": "all",
                "content": {"content_type": "text", "parts": ["q"]},
            },
            {
                "id": "a1",
                "author": {"role": "assistant"},
                "recipient": "all",
                "content": {"content_type": "text", "parts": ["r"]},
            },
            {
                "id": "u2",
                "author": {"role": "user"},
                "recipient": "all",
                "content": {"content_type": "text", "parts": ["q2"]},
            },
            {
                "id": "t1",
                "author": {"role": "tool"},
                "recipient": "all",
                "content": {
                    "content_type": "multimodal_text",
                    "parts": [
                        {
                            "content_type": "image_asset_pointer",
                            "asset_pointer": "sediment://file-gen1",
                        }
                    ],
                },
                "metadata": {"image_gen_title": "Titre"},
            },
            {
                "id": "u3",
                "author": {"role": "user"},
                "recipient": "all",
                "content": {"content_type": "text", "parts": ["q3"]},
            },
            {
                "id": "a3",
                "author": {"role": "assistant"},
                "recipient": "all",
                "content": {"content_type": "text", "parts": ["r3"]},
            },
        ],
    }
    conv = ChatGPTParser().parse("", extra={"api": payload})
    assert [m.role for m in conv.messages] == [
        "user", "assistant", "user", "assistant", "user", "assistant",
    ]
    gen = API_FILE_DOWNLOAD_URL.format(fid="file-gen1")
    assert conv.messages[3].texte == f"![Generated image: Titre]({gen})"
    assert conv.messages[4].texte == "q3"


def test_parse_api_multimodal_sans_image_generee_degradee():
    """Un asset inconnu ne casse pas le mapping (type signale, texte garde)."""
    payload = {
        "conversation_id": "c1",
        "messages": [
            {
                "id": "u1",
                "author": {"role": "user"},
                "recipient": "all",
                "content": {
                    "content_type": "multimodal_text",
                    "parts": [
                        {
                            "content_type": "audio_asset_pointer",
                            "asset_pointer": "sediment://file-audio1",
                            "format": "wav",
                        },
                        {
                            "content_type": "audio_transcription",
                            "text": "bonjour",
                            "direction": "in",
                        },
                    ],
                },
            },
            {
                "id": "a1",
                "author": {"role": "assistant"},
                "recipient": "all",
                "content": {"content_type": "text", "parts": ["salut"]},
            },
        ],
    }
    conv = ChatGPTParser().parse("", extra={"api": payload})
    assert conv.messages[0].texte == "bonjour"
    assert "sediment://" not in conv.messages[0].texte


# -- service -----------------------------------------------------------------


class FakeSession:
    """Session navigateur factice (mint du token + cookies)."""

    def __init__(self, cookies=None, session_body=None):
        self._cookies = cookies if cookies is not None else [
            {"name": "oai-did", "value": "abc"},
            {"name": "__Secure-next-auth.session-token", "value": "sess"},
        ]
        self._session_body = session_body
        self.gotos = []
        self.fetched = []

    def goto(self, url):
        self.gotos.append(url)

    def wait_ms(self, ms):
        pass

    def cookies(self):
        return list(self._cookies)

    def fetch(self, url):
        self.fetched.append(url)
        if self._session_body is None:
            return None
        return self._session_body, "application/json"


class FakeClient:
    """Client HTTP factice : renvoie des pages JSON dans l'ordre."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get_json(self, url):
        self.calls.append(url)
        if not self.pages:
            raise AssertionError(f"appel inattendu: {url}")
        return self.pages.pop(0)


def test_api_desactivee_par_defaut():
    service = ChatGPTService(FakeSession(), {"services": {"chatgpt": {}}})
    assert service.api_enabled is False
    assert ChatGPTService(FakeSession(), {}).api_enabled is False


def test_api_activee_par_config():
    service = ChatGPTService(FakeSession(), {"services": {"chatgpt": {"api": True}}})
    assert service.api_enabled is True


def test_api_client_mint_token():
    body = json.dumps(
        {"accessToken": "tok-123", "account": {"id": "acc-9"}}
    ).encode("utf-8")
    session = FakeSession(session_body=body)
    service = ChatGPTService(session, {"services": {"chatgpt": {"api": True}}})
    client = service._api_client()
    assert isinstance(client, _ChatGPTHttpClient)
    assert client.access_token == "tok-123"
    assert client.account_id == "acc-9"
    assert "oai-did=abc" in client.cookie_header
    assert session.gotos == ["https://chatgpt.com/"]
    assert session.fetched == ["https://chatgpt.com/api/auth/session"]
    # le client est memorise (pas de nouveau mint)
    assert service._api_client() is client


def test_api_client_sans_token_leve_service_not_logged_in():
    service = ChatGPTService(
        FakeSession(session_body=b"{}"), {"services": {"chatgpt": {"api": True}}}
    )
    with pytest.raises(ServiceNotLoggedIn):
        service._api_client()


def test_api_client_sans_cookie_leve_service_not_logged_in():
    service = ChatGPTService(
        FakeSession(cookies=[]), {"services": {"chatgpt": {"api": True}}}
    )
    with pytest.raises(ServiceNotLoggedIn):
        service._api_client()


def test_list_conversations_api_pagination():
    full_page = {
        "items": [
            {"id": f"c{i}", "title": f"T{i}", "create_time": "2026-01-01T00:00:00Z"}
            for i in range(API_PAGE_SIZE)
        ],
        "total": API_PAGE_SIZE + 1,
        "limit": API_PAGE_SIZE,
        "offset": 0,
    }
    last_page = {
        "items": [{"id": "clast", "title": "Dernier"}],
        "total": API_PAGE_SIZE + 1,
        "limit": API_PAGE_SIZE,
        "offset": API_PAGE_SIZE,
    }
    service = ChatGPTService(FakeSession(), {"services": {"chatgpt": {"api": True}}})
    client = FakeClient([full_page, last_page])
    service._http = client
    refs = service.list_conversations()
    assert len(refs) == API_PAGE_SIZE + 1
    assert refs[-1].id == "clast"
    assert refs[-1].url == "https://chatgpt.com/c/clast"
    assert "offset=0" in client.calls[0]
    assert f"offset={API_PAGE_SIZE}" in client.calls[1]


def test_list_conversations_api_limite():
    page = {
        "items": [{"id": f"c{i}", "title": None} for i in range(API_PAGE_SIZE)],
        "total": API_PAGE_SIZE * 2,
        "limit": API_PAGE_SIZE,
        "offset": 0,
    }
    service = ChatGPTService(FakeSession(), {"services": {"chatgpt": {"api": True}}})
    service._http = FakeClient([page])
    refs = service.list_conversations(limit=3)
    assert [r.id for r in refs] == ["c0", "c1", "c2"]


def test_list_conversations_api_timestamps_normalises():
    """Epoch du payload -> ISO : le tri de `conversation_list` reste homogene."""
    page = {
        "items": [
            {
                "id": "c1",
                "title": "T",
                "create_time": 1790169940.075207,
                "update_time": 1790170216.609211,
            }
        ],
        "total": 1,
        "limit": API_PAGE_SIZE,
        "offset": 0,
    }
    service = ChatGPTService(FakeSession(), {"services": {"chatgpt": {"api": True}}})
    service._http = FakeClient([page])
    refs = service.list_conversations()
    assert refs[0].raw["started_at"] == "2026-09-23T13:25:40Z"
    assert refs[0].raw["last_message_at"] == "2026-09-23T13:30:16Z"


def test_fetch_conversation_api_remonte_les_pages_anciennes():
    recent = {
        "conversation_id": "c1",
        "title": "T",
        "messages": [{"id": "m2"}],
        "page_info": {"has_previous_page": True, "start_cursor": "m2"},
    }
    older = {
        "conversation_id": "c1",
        "title": "T",
        "messages": [{"id": "m1"}],
        "page_info": {"has_previous_page": False, "start_cursor": "m1"},
    }
    client = FakeClient([recent, older])
    service = ChatGPTService(FakeSession(), {"services": {"chatgpt": {"api": True}}})
    data = service._fetch_conversation_api(client, "c1")
    assert [m["id"] for m in data["messages"]] == ["m1", "m2"]
    assert "num_turns=100" in client.calls[0]
    assert "before=m2" in client.calls[1]


def test_export_conversation_api_sans_html_avec_payload_brut(api_payload):
    """Mode API : pas de HTML, payload brut disponible pour l'archivage."""
    service = ChatGPTService(FakeSession(), {"services": {"chatgpt": {"api": True}}})
    service._http = FakeClient([api_payload])
    ref = ConversationRef(
        service="chatgpt",
        id=api_payload["conversation_id"],
        url="https://chatgpt.com/c/" + api_payload["conversation_id"],
    )
    conv, html = service.export_conversation_with_html(ref)
    assert len(conv.messages) == 52
    assert conv.url == ref.url
    assert html is None
    # le payload brut est le payload de l'API, non transforme
    payload = service.take_raw_payload()
    assert payload["conversation_id"] == api_payload["conversation_id"]
    assert payload["messages"] == api_payload["messages"]
    # consomme : un export ulterieur ne le reecrit pas
    assert service.take_raw_payload() is None
    # pas de screenshots en mode API
    assert service.capture_message_screenshots(conv, Path("/tmp/unused")) == 0


# -- telechargement des assets (images) ---------------------------------------

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
ASSET_SIGNED_URL = "https://chatgpt.com/backend-api/estuary/content?id=x"


class FakeAssetClient:
    """Client HTTP factice : resolution (`get`) + octets (`fetch`)."""

    def __init__(self, status=200, download_url=ASSET_SIGNED_URL):
        self.status = status
        self.download_url = download_url
        self.gets = []
        self.fetches = []

    def get(self, url, accept="application/json, text/plain, */*"):
        self.gets.append(url)
        if self.status != 200:
            return self.status, b'{"detail":"File not found"}'
        return 200, json.dumps(
            {"status": "success", "download_url": self.download_url}
        ).encode("utf-8")

    def fetch(self, url, accept="*/*"):
        self.fetches.append(url)
        return PNG_BYTES, "image/png"


def test_download_api_asset_resout_et_telecharge():
    service = ChatGPTService(FakeSession(), {"services": {"chatgpt": {"api": True}}})
    client = FakeAssetClient()
    service._http = client
    url = API_FILE_DOWNLOAD_URL.format(fid="file-abc")
    assert service.download_api_asset(url) == (PNG_BYTES, "image/png")
    assert client.gets == [url]
    assert client.fetches == [ASSET_SIGNED_URL]


def test_download_api_asset_absent_retourne_none():
    service = ChatGPTService(FakeSession(), {"services": {"chatgpt": {"api": True}}})
    service._http = FakeAssetClient(status=404)
    url = API_FILE_DOWNLOAD_URL.format(fid="file-audio")
    assert service.download_api_asset(url) is None


def test_http_client_reessaie_sur_429(monkeypatch):
    """429 (limite de rafale) : attente puis nouvel essai, sans erreur."""
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
    client = _ChatGPTHttpClient("a=b", access_token="tok")
    status, raw = client.get("https://chatgpt.com/backend-api/x")
    assert status == 200
    assert raw == b'{"ok": true}'
    assert calls["n"] == 2
    assert sleeps == [7.0]  # Retry-After respecte


def test_http_client_429_persistant_leve_rate_limited(monkeypatch):
    """429 epuise : `RateLimitedError` (arret propre du service), pas RuntimeError."""
    import time as time_module
    import urllib.error
    import urllib.request

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 429, "Too Many Requests", {}, None
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time_module, "sleep", lambda s: None)
    client = _ChatGPTHttpClient("a=b", access_token="tok")
    with pytest.raises(RateLimitedError):
        client.get_json("https://chatgpt.com/backend-api/x")


def test_session_api_intercepte_les_assets():
    """En mode API, `session.fetch` des assets passe par le client HTTP."""
    inner = FakeSession(session_body=b"{}")
    service = ChatGPTService(inner, {"services": {"chatgpt": {"api": True}}})
    client = FakeAssetClient()
    service._http = client
    url = API_FILE_DOWNLOAD_URL.format(fid="file-abc")
    assert service.session.fetch(url) == (PNG_BYTES, "image/png")
    assert inner.fetched == []  # le navigateur n'est pas sollicite
    # une URL non-asset reste deleguee a la session navigateur
    assert service.session.fetch("https://chatgpt.com/backend-api/estuary/content?id=z")
    assert inner.fetched == ["https://chatgpt.com/backend-api/estuary/content?id=z"]


def test_download_service_images_reecrit_le_markdown(tmp_path, multimodal_payload):
    """Bout en bout : parser -> service -> markdown `images/<hash>.png` local."""
    conv = ChatGPTParser().parse(
        "",
        conversation_id=multimodal_payload["conversation_id"],
        extra={"api": multimodal_payload},
    )
    service = ChatGPTService(FakeSession(), {"services": {"chatgpt": {"api": True}}})
    service._http = FakeAssetClient()
    stats = download_service_images(conv, service, tmp_path)
    assert stats == {"downloaded": 3, "cached": 0, "failed": 0}
    url = API_FILE_DOWNLOAD_URL.format(fid="file_00000000aaaabbbb")
    filename = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20] + ".png"
    assert f"![photo.png](images/{filename})" in conv.messages[0].texte
    assert (tmp_path / filename).read_bytes() == PNG_BYTES
    # plus aucune URL distante dans le JSON
    assert all(
        "chatgpt.com" not in message.texte for message in conv.messages
    )
