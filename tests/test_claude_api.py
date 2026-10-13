"""Tests du chemin API Claude (mapper JSON + client HTTP + pagination + backoff).

Le mapper est valide contre l'export DOM de l'etalon
(`claude_api_dom_reference.json`, produit par le parseur DOM courant) : memes
roles, meme texte (a la normalisation pres) et metriques au moins egales, hors
divergences documentees (l'API est plus complete : elle conserve le tour
assistant reduit a une image markdown que le DOM fusionne dans le tour user, et
les resumes de raisonnement non deplies).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.parsers.claude import ClaudeParser
from src.schema import ConversationRef
from src.services.base import (
    ConversationUnavailableError,
    RateLimitedError,
    ServiceNotLoggedIn,
)
from src.services.claude import (
    API_PAGE_SIZE,
    ClaudeService,
    _ApiFetchSession,
    _ClaudeHttpClient,
)
from src.utils.etalon import conversation_metrics, detect_capabilities

FIXTURES = Path(__file__).parent / "fixtures"

#: l'API est plus complete que le DOM : le parseur DOM n'a pas vu le tour
#: assistant reduit a une image markdown (API[41]) et a fusionne les deux tours
#: user encadrants (DOM[40] = API[40] + API[42]) ; le decalage vaut 2 au-dela.
DOM_TO_API = list(range(40)) + [40] + [43, 44, 45, 46, 47, 48, 49]
#: tour DOM fusionne (deux messages user de l'API)
DOM_MERGED_INDEX = 40


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def api_payload():
    return load_fixture("claude_api_conversation.json")


@pytest.fixture(scope="module")
def api_conversation(api_payload):
    return ClaudeParser().parse(
        "", conversation_id=api_payload["uuid"], extra={"api": api_payload}
    )


def _norm(text: str) -> str:
    """Normalise les differences de rendu DOM (guillemets, liens, tableaux)."""
    text = (text or "").replace("’", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\|(?:\s*-{2,}\s*\|)+", "|---|", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# -- mapper API --------------------------------------------------------------


def test_parse_api_etalon(api_conversation):
    conv = api_conversation
    assert conv.platform == "claude"
    assert conv.conversation_id == "f0000000-0000-4000-8000-12a21176e31e"
    assert conv.title == "Formatting text with different styles"
    assert conv.model == "claude-sonnet-5"
    assert conv.url == f"https://claude.ai/chat/{conv.conversation_id}"
    assert conv.started_at == "2026-09-24T08:58:21Z"
    assert conv.last_message_at == "2026-09-24T09:37:00Z"
    assert len(conv.messages) == 50
    # alternance stricte user/assistant
    assert [m.role for m in conv.messages] == ["user", "assistant"] * 25
    assert conv.messages[0].texte.startswith("Écris le mot \"test\" trois fois")
    assert conv.messages[-1].texte.startswith("Vitesse moyenne = distance")


def test_parse_api_timestamps_et_ids(api_conversation):
    first = api_conversation.messages[0]
    assert first.timestamp == "2026-09-24T08:58:22Z"
    assert first.message_id.startswith("f0000000-")
    assert first.model is None
    assistant = api_conversation.messages[1]
    assert assistant.timestamp is not None


def test_parse_api_matches_dom_reference(api_conversation):
    """Le mapper API concorde avec l'export DOM (hors divergences connues)."""
    dom = load_fixture("claude_api_dom_reference.json")["messages"]
    api = api_conversation.messages
    assert len(api) == 50 and len(dom) == 48
    for dom_index, api_index in enumerate(DOM_TO_API):
        a = api[api_index]
        b = dom[dom_index]
        assert a.role == b["role"], dom_index
        if dom_index == DOM_MERGED_INDEX:
            merged = "\n\n".join((api[40].texte, api[42].texte))
            assert _norm(merged) == _norm(b["texte"]), dom_index
        else:
            assert _norm(a.texte) == _norm(b["texte"]), dom_index


def test_parse_api_metriques_au_moins_egales_au_dom(api_conversation):
    """L'API ne perd aucune metrique de l'export DOM (et gagne 2 messages)."""
    dom = load_fixture("claude_api_dom_reference.json")
    api_metrics = conversation_metrics(api_conversation.to_dict(validate=False))
    dom_metrics = conversation_metrics(dom)
    for feature in ("images", "tables", "lists", "code_langs", "latex"):
        assert api_metrics[feature] >= dom_metrics[feature], feature
    assert api_metrics["consecutive_roles"] == 0
    assert api_metrics["messages"] >= dom_metrics["messages"]


def test_parse_api_capacites_sans_regression(api_conversation):
    """Aucune capacite detectee du DOM ne disparait (connectors en plus)."""
    dom = load_fixture("claude_api_dom_reference.json")
    api_caps = detect_capabilities(api_conversation.to_dict(validate=False), "")
    dom_caps = detect_capabilities(dom, "")
    assert not (dom_caps - api_caps)
    assert "connectors" in api_caps  # outil MCP `visualize:*` (mcp_server_url)


def test_parse_api_latex_et_code(api_conversation):
    msgs = api_conversation.messages
    assert msgs[13].texte == "L'équation $E=mc^2$ relie l'énergie et la masse."
    assert msgs[15].texte.startswith("$$\\begin{pmatrix}")
    css = msgs[19]
    assert css.code_blocks and css.code_blocks[0].language == "markdown"


def test_parse_api_pieces_jointes(api_conversation):
    """Images et documents en markdown (URL absolue) ; blobs et extraits par nom."""
    msgs = api_conversation.messages
    image = msgs[20].texte
    assert "![image.png](https://claude.ai/api/" in image
    assert "/files/" in image and image.endswith("/preview)")
    pdf = msgs[22].texte
    assert "[document.pdf](https://claude.ai/api/" in pdf
    assert pdf.endswith("/document_pdf)")
    # pieces sans URL de telechargement : nom seul, comme le DOM
    assert msgs[26].texte.endswith("donnees.json")
    assert msgs[28].texte.endswith("video.mp4")
    assert "](http" not in msgs[26].texte.rsplit("donnees.json", 1)[0]


def test_parse_api_texte_colle_sans_nom():
    """Un texte colle (file_name vide) garde son nom UI : le message n'est pas vide."""
    payload = {
        "uuid": "c1",
        "chat_messages": [
            {
                "uuid": "u1",
                "parent_message_uuid": "00000000-0000-4000-8000-000000000000",
                "sender": "human",
                "index": 0,
                "content": [{"type": "text", "text": ""}],
                "attachments": [
                    {
                        "id": "a1",
                        "file_name": "",
                        "file_type": "txt",
                        "extracted_content": "des logs colles",
                    }
                ],
            },
            {
                "uuid": "a2",
                "parent_message_uuid": "u1",
                "sender": "assistant",
                "index": 1,
                "content": [{"type": "text", "text": "reponse"}],
            },
        ],
        "current_leaf_message_uuid": "a2",
    }
    conv = ClaudeParser().parse("", extra={"api": payload})
    assert [m.role for m in conv.messages] == ["user", "assistant"]
    assert conv.messages[0].texte == "Texte collé.txt"


def test_parse_api_raisonnement_outils_artefacts(api_conversation):
    msgs = api_conversation.messages
    # thinking -> summaries (raisonnement) et tool_use -> libelles
    assert msgs[43].reasoning == (
        "Checking available tools for generating the requested image."
    )
    assert msgs[43].tools == ["MCP visualize:read_me", "MCP visualize:show_widget"]
    assert msgs[43].metadata.get("had_thinking") is True
    # execution de code + cartes de fichiers -> outils et artefacts (comme le DOM)
    assert "Exécution de code" in msgs[31].tools
    assert msgs[31].artifacts == ["Scores (csv)"]
    assert msgs[33].artifacts == ["Bonjour (txt)"]
    assert msgs[45].artifacts == ["Artefact test (html)"]
    assert "Artefact publié" in msgs[45].tools


def test_parse_api_sources(api_payload):
    """Les citations des blocs texte alimentent `sources` (URL dedupliquees)."""
    conv = ClaudeParser().parse(
        "", conversation_id=api_payload["uuid"], extra={"api": api_payload}
    )
    assert all(not m.sources for m in conv.messages)

    payload = {
        "uuid": "c1",
        "name": "Sources",
        "chat_messages": [
            {
                "uuid": "u1",
                "parent_message_uuid": "00000000-0000-4000-8000-000000000000",
                "sender": "human",
                "index": 0,
                "content": [{"type": "text", "text": "cherche"}],
            },
            {
                "uuid": "a1",
                "parent_message_uuid": "u1",
                "sender": "assistant",
                "index": 1,
                "content": [
                    {
                        "type": "text",
                        "text": "voila",
                        "citations": [
                            {"title": "A", "url": "https://a.example"},
                            {"title": "A bis", "url": "https://a.example"},
                            {"title": "B", "url": "https://b.example"},
                        ],
                    }
                ],
            },
        ],
    }
    conv = ClaudeParser().parse("", extra={"api": payload})
    assert conv.messages[1].sources == ["https://a.example", "https://b.example"]


def test_parse_api_autolink_hors_code(api_payload):
    """Les URL nues deviennent des liens markdown, le code est preserve."""
    payload = {
        "uuid": "c1",
        "chat_messages": [
            {
                "uuid": "u1",
                "parent_message_uuid": "00000000-0000-4000-8000-000000000000",
                "sender": "human",
                "index": 0,
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Voir https://example.com/a?b=1.\n\n"
                            "```\nhttps://code.example\n```\n\n"
                            "![img](https://img.example/i.png)"
                        ),
                    }
                ],
            },
            {
                "uuid": "a1",
                "parent_message_uuid": "u1",
                "sender": "assistant",
                "index": 1,
                "content": [{"type": "text", "text": "ok"}],
            },
        ],
    }
    conv = ClaudeParser().parse("", extra={"api": payload})
    text = conv.messages[0].texte
    assert "[https://example.com/a?b=1](https://example.com/a?b=1)." in text
    assert "```\nhttps://code.example\n```" in text
    assert "![img](https://img.example/i.png)" in text


# -- variantes (arbre API) ---------------------------------------------------


def test_parse_api_variantes_regeneration():
    """Un frere assistant hors branche est une regeneration (version active)."""
    payload = load_fixture("claude_api_variants.json")
    conv = ClaudeParser().parse(
        "", conversation_id=payload["uuid"], extra={"api": payload}
    )
    assert len(conv.messages) == 14
    regenerated = [m for m in conv.messages if m.variants]
    assert len(regenerated) == 1
    message = regenerated[0]
    assert message.role == "assistant"
    assert [v.kind for v in message.variants] == ["regeneration", "regeneration"]
    assert [v.active for v in message.variants] == [False, True]
    assert message.variants[0].texte == ""  # version abandonnee (vide)
    assert message.variants[1].texte.startswith("Petit souci technique")
    assert message.variants[1].message_id == message.message_id


def test_parse_api_variantes_edition():
    """Des freres user hors branche sont une edition (version active marquee)."""
    payload = {
        "uuid": "c1",
        "name": "Edition",
        "chat_messages": [
            {
                "uuid": "u1",
                "parent_message_uuid": "00000000-0000-4000-8000-000000000000",
                "sender": "human",
                "index": 0,
                "content": [{"type": "text", "text": "version 1"}],
            },
            {
                "uuid": "u2",
                "parent_message_uuid": "00000000-0000-4000-8000-000000000000",
                "sender": "human",
                "index": 1,
                "content": [{"type": "text", "text": "version 2"}],
            },
            {
                "uuid": "a2",
                "parent_message_uuid": "u2",
                "sender": "assistant",
                "index": 2,
                "content": [{"type": "text", "text": "reponse"}],
            },
        ],
        "current_leaf_message_uuid": "a2",
    }
    conv = ClaudeParser().parse("", extra={"api": payload})
    assert [m.role for m in conv.messages] == ["user", "assistant"]
    assert conv.messages[0].texte == "version 2"
    assert [v.kind for v in conv.messages[0].variants] == ["edit", "edit"]
    assert [v.texte for v in conv.messages[0].variants] == ["version 1", "version 2"]
    assert [v.active for v in conv.messages[0].variants] == [False, True]
    # le tour assistant n'a pas de frere : pas de variante
    assert conv.messages[1].variants == []


def test_parse_api_feuille_absente_repli_derniere_version():
    """Sans `current_leaf_message_uuid`, la derniere version (index max) est active."""
    payload = {
        "uuid": "c1",
        "chat_messages": [
            {
                "uuid": "u1",
                "parent_message_uuid": "00000000-0000-4000-8000-000000000000",
                "sender": "human",
                "index": 0,
                "content": [{"type": "text", "text": "v1"}],
            },
            {
                "uuid": "u2",
                "parent_message_uuid": "00000000-0000-4000-8000-000000000000",
                "sender": "human",
                "index": 1,
                "content": [{"type": "text", "text": "v2"}],
            },
        ],
        "current_leaf_message_uuid": None,
    }
    conv = ClaudeParser().parse("", extra={"api": payload})
    assert len(conv.messages) == 1
    assert conv.messages[0].texte == "v2"
    assert len(conv.messages[0].variants) == 2


def test_parse_api_payload_vide():
    from src.parsers.base import ParseError

    with pytest.raises(ParseError):
        ClaudeParser().parse("", extra={"api": {"chat_messages": []}})


# -- service -----------------------------------------------------------------


class FakeSession:
    """Session navigateur factice (harvest des cookies + fetch de repli)."""

    def __init__(self, cookies=None):
        self._cookies = cookies if cookies is not None else [
            {"name": "sessionKey", "value": "sk-abc"},
            {"name": "cf_clearance", "value": "cf-1"},
        ]
        self.gotos = []
        self.fetched = []
        self.fingerprint = {
            "user_agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/151.0.0.0",
            "lang": "en-GB",
        }

    def goto(self, url):
        self.gotos.append(url)

    def wait_ms(self, ms):
        pass

    def cookies(self):
        return list(self._cookies)

    def fetch(self, url):
        self.fetched.append(url)
        return b"external", "image/png"


class FakeClient:
    """Client HTTP factice : pages JSON / reponses brutes dans l'ordre."""

    def __init__(self, pages=None, responses=None):
        self.pages = list(pages or [])
        self.responses = list(responses or [])
        self.calls = []
        self.org_id = "org-1"

    def get_json(self, url):
        self.calls.append(url)
        if not self.pages:
            raise AssertionError(f"appel inattendu: {url}")
        return self.pages.pop(0)

    def get(self, url, accept="application/json"):
        self.calls.append(url)
        if not self.responses:
            raise AssertionError(f"appel inattendu: {url}")
        return self.responses.pop(0)

    def fetch(self, url, accept="*/*"):
        self.calls.append(url)
        return b"bytes", "image/webp"


def _service(session=None, config=None):
    return ClaudeService(session or FakeSession(), config or {})


def test_api_activee_par_defaut():
    assert _service().api_enabled is True
    assert _service(config={"services": {"claude": {}}}).api_enabled is True


def test_api_desactivable_par_config():
    service = _service(config={"services": {"claude": {"api": False}}})
    assert service.api_enabled is False


def test_api_client_harvest_cookies_et_org(monkeypatch):
    session = FakeSession()
    service = _service(session)
    monkeypatch.setattr(
        _ClaudeHttpClient, "get_json", lambda self, url: [{"uuid": "org-9"}]
    )
    client = service._api_client()
    assert isinstance(client, _ClaudeHttpClient)
    assert client.org_id == "org-9"
    assert "sessionKey=sk-abc" in client.cookie_header
    assert client.user_agent.startswith("Mozilla/5.0 (X11")
    assert session.gotos == ["https://claude.ai/chats"]
    # le client est memorise (pas de nouveau harvest)
    assert service._api_client() is client


def test_api_client_cookies_captures_sans_navigateur(monkeypatch):
    """cookies/claude.json present : aucun harvest navigateur (HTTP pur)."""
    session = FakeSession()
    service = _service(session)
    monkeypatch.setattr(
        "src.services.claude.load_cookie_header",
        lambda cookies_dir, name: "sessionKey=from-file",
    )
    monkeypatch.setattr(
        _ClaudeHttpClient, "get_json", lambda self, url: [{"uuid": "org-1"}]
    )
    client = service._api_client()
    assert client.cookie_header == "sessionKey=from-file"
    assert session.gotos == []


def test_api_client_sans_cookie_leve_service_not_logged_in():
    service = _service(FakeSession(cookies=[]))
    with pytest.raises(ServiceNotLoggedIn):
        service._api_client()


def test_api_client_401_leve_service_not_logged_in(monkeypatch):
    def boom(self, url):
        raise ServiceNotLoggedIn("401")

    monkeypatch.setattr(_ClaudeHttpClient, "get_json", boom)
    with pytest.raises(ServiceNotLoggedIn):
        _service()._api_client()


def test_resolve_org_id_repli_account():
    client = FakeClient(pages=[
        RuntimeError("indisponible"),  # /api/organizations
        {"memberships": [{"organization": {"uuid": "org-2"}}]},  # /api/account
    ])

    def get_json(url):
        result = client.pages.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    client.get_json = get_json
    service = _service()
    assert service._resolve_org_id(client) == "org-2"


def test_list_conversations_api_pagination(monkeypatch):
    full_page = {
        "data": [
            {
                "uuid": f"c{i}",
                "name": f"T{i}",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-02T00:00:00Z",
            }
            for i in range(API_PAGE_SIZE)
        ],
        "has_more": True,
    }
    last_page = {
        "data": [{"uuid": "clast", "name": "Dernier", "created_at": None, "updated_at": None}],
        "has_more": False,
    }
    client = FakeClient(pages=[full_page, last_page])
    service = _service()
    monkeypatch.setattr(service, "_api_client", lambda: client)
    refs = service.list_conversations()
    assert len(refs) == API_PAGE_SIZE + 1
    assert refs[-1].id == "clast"
    assert refs[-1].url == "https://claude.ai/chat/clast"
    assert refs[0].raw["started_at"] == "2026-01-01T00:00:00Z"
    assert "offset=0" in client.calls[0]
    assert f"offset={API_PAGE_SIZE}" in client.calls[1]
    assert "archived=false" in client.calls[0]


def test_list_conversations_api_limite(monkeypatch):
    client = FakeClient(pages=[{
        "data": [{"uuid": f"c{i}", "name": f"T{i}"} for i in range(API_PAGE_SIZE)],
        "has_more": True,
    }])
    service = _service()
    monkeypatch.setattr(service, "_api_client", lambda: client)
    refs = service.list_conversations(limit=5)
    assert len(refs) == 5
    assert len(client.calls) == 1


def test_fetch_conversation_api_404_indisponible(monkeypatch):
    client = FakeClient(responses=[(404, b"{}")])
    service = _service()
    with pytest.raises(ConversationUnavailableError):
        service._fetch_conversation_api(client, "c404")


def test_fetch_conversation_api_401_session_perdue():
    client = FakeClient(responses=[(401, b"{}")])
    with pytest.raises(ServiceNotLoggedIn):
        _service()._fetch_conversation_api(client, "c401")


def test_export_conversation_api_sans_html(monkeypatch):
    payload = load_fixture("claude_api_conversation.json")
    client = FakeClient()
    service = _service()
    monkeypatch.setattr(service, "_api_client", lambda: client)
    monkeypatch.setattr(
        service, "_fetch_conversation_api", lambda client, cid: payload
    )
    ref = ConversationRef(
        service="claude", id=payload["uuid"], url=f"https://claude.ai/chat/{payload['uuid']}"
    )
    conv, html = service.export_conversation_with_html(ref)
    assert html is None
    assert conv.conversation_id == payload["uuid"]
    assert len(conv.messages) == 50
    assert service.take_raw_payload() is payload
    assert service.take_raw_payload() is None  # consomme


def test_export_conversation_vide_ignoree(monkeypatch):
    from src.services.base import EmptyConversationError

    service = _service()
    monkeypatch.setattr(service, "_api_client", lambda: FakeClient())
    monkeypatch.setattr(
        service, "_fetch_conversation_api", lambda client, cid: {"chat_messages": []}
    )
    with pytest.raises(EmptyConversationError):
        service.export_conversation_with_html(
            ConversationRef(service="claude", id="c1", url="https://claude.ai/chat/c1")
        )


def test_screenshots_ignores_en_mode_api():
    service = _service()
    assert service.capture_message_screenshots(None, Path("/tmp/x")) == 0


# -- client HTTP : 429 ---------------------------------------------------------


def test_backoff_429_respecte_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr("src.services.api_client.time.sleep", sleeps.append)
    client = _ClaudeHttpClient("sessionKey=sk")
    assert client._wait_429(0, {"Retry-After": "12"}, "https://claude.ai/x") is True
    assert sleeps == [12.0]


def test_backoff_429_exponentiel_plafonne(monkeypatch):
    sleeps = []
    monkeypatch.setattr("src.services.api_client.time.sleep", sleeps.append)
    client = _ClaudeHttpClient("sessionKey=sk")
    assert client._wait_429(2, {}, "https://claude.ai/x") is True
    assert sleeps == [180.0]
    assert client._wait_429(3, {}, "https://claude.ai/x") is True
    assert sleeps[-1] == 300  # plafond


def test_backoff_429_epuise_abandonne(monkeypatch):
    monkeypatch.setattr("src.services.api_client.time.sleep", lambda s: None)
    client = _ClaudeHttpClient("sessionKey=sk")
    assert client._wait_429(7, {}, "https://claude.ai/x") is False


def test_get_reessaie_sur_429(monkeypatch):
    """Le client rejoue la requete apres un 429 (Retry-After) puis rend la reponse."""
    import urllib.error
    import urllib.request

    monkeypatch.setattr("src.services.api_client.time.sleep", lambda s: None)
    calls = {"n": 0}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                request.full_url, 429, "Too Many Requests", {"Retry-After": "1"}, None
            )
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = _ClaudeHttpClient("sessionKey=sk")
    status, raw = client.get("https://claude.ai/api/x")
    assert status == 200 and raw == b'{"ok": true}'
    assert calls["n"] == 2


def test_429_persistant_leve_rate_limited(monkeypatch):
    """429 epuise : `RateLimitedError` (arret propre), pas RuntimeError."""
    import urllib.error
    import urllib.request

    monkeypatch.setattr("src.services.api_client.time.sleep", lambda s: None)

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 429, "Too Many Requests", {}, None
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = _ClaudeHttpClient("sessionKey=sk")
    with pytest.raises(RateLimitedError):
        client.get_json("https://claude.ai/api/x")


# -- telechargement des assets -------------------------------------------------


def test_api_fetch_session_route_les_assets(monkeypatch):
    session = FakeSession()
    service = _service(session)
    wrapper = service.session
    assert isinstance(wrapper, _ApiFetchSession)
    monkeypatch.setattr(
        service,
        "download_api_asset",
        lambda url: (b"asset", "image/webp") if url.startswith("https://claude.ai/") else None,
    )
    assert wrapper.fetch("https://claude.ai/api/org/files/x/preview") == (
        b"asset",
        "image/webp",
    )
    # les URL externes passent par la session d'origine
    assert wrapper.fetch("https://placehold.co/120x60") == (b"external", "image/png")
    assert session.fetched == ["https://placehold.co/120x60"]


def test_download_api_asset_utilise_le_client(monkeypatch):
    client = FakeClient()
    service = _service()
    monkeypatch.setattr(service, "_api_client", lambda: client)
    assert service.download_api_asset("https://claude.ai/api/org/files/x/preview") == (
        b"bytes",
        "image/webp",
    )
    assert client.calls == ["https://claude.ai/api/org/files/x/preview"]
