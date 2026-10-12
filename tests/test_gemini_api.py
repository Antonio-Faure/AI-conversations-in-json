"""Tests du chemin API Gemini (mapper JSON + client batchexecute + pagination).

Le mapper est valide contre l'export DOM gele de l'etalon
(`fixtures/etalons/gemini/gemini.json`, produit par le parseur DOM) : memes
roles, meme texte a la normalisation pres, memes extras, hors divergences
documentees (l'API conserve le markdown brut du modele, le DOM rend le HTML ;
l'API connait la langue des sorties de code et les pieces jointes
telechargeables, le DOM les perd).

Note d'ordre : le DOM gele a ete collecte pendant le scroll (blocs dans un
ordre tourne) ; l'API renvoie le fil dans l'ordre chronologique (verifie par
les timestamps et par le DOM complet remonte). La comparaison se fait donc par
appariement du texte utilisateur, pas par position.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

import pytest

from src.parsers.base import ParseError
from src.parsers.gemini import GeminiParser
from src.schema import ConversationRef
from src.services.base import ServiceNotLoggedIn
from src.services.gemini import (
    API_LIST_PAGE_SIZE,
    API_TURNS_PAGE_SIZE,
    GeminiService,
    _ApiAuthError,
    _decode_batchexecute,
    _GeminiHttpClient,
    _is_auth_error,
)
from src.utils.etalon import (
    DETECTABLE,
    conversation_metrics,
    detect_capabilities,
    normalize_for_match,
    regressions,
)

FIXTURES = Path(__file__).parent / "fixtures"
ETALON_DOM = FIXTURES / "etalons" / "gemini" / "gemini.json"
ETALON_DOM_HTML = FIXTURES / "etalons" / "gemini" / "gemini.html"
#: index des messages ou l'API est plus riche que le DOM (documente) : sortie
#: de code avec langue (`text`), fichier genere telechargeable, image generee.
API_ONLY_RICHNESS = {13, 23, 25, 27, 29, 31, 33, 35, 41, 51, 53, 59}


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def api_payload():
    return load_fixture("gemini_api_conversation.json")


@pytest.fixture(scope="module")
def api_conversation(api_payload):
    return GeminiParser().parse(
        "",
        conversation_id=api_payload["conversation_id"],
        extra={"api": api_payload},
    )


@pytest.fixture(scope="module")
def dom_reference():
    return load_fixture("etalons/gemini/gemini.json")


def _norm(text: str) -> str:
    """Normalise les differences de rendu (espaces, lignes vides)."""
    import re

    text = re.sub(r"[ \t]+\n", "\n", text or "")
    text = re.sub(r"\n{2,}", "\n", text)
    text = re.sub(r"[ \t]+([.,;:!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


# -- mapper API --------------------------------------------------------------


def test_parse_api_etalon(api_conversation):
    conv = api_conversation
    assert conv.platform == "gemini"
    assert conv.conversation_id == "f00d6fcb38ce17ec"
    assert conv.title == "Application de styles textuels"
    assert conv.model == "gemini-3.6-flash"
    assert len(conv.messages) == 60
    assert [m.role for m in conv.messages] == ["user", "assistant"] * 30
    assert conv.messages[0].texte.startswith("Affiche la phrase suivante")
    assert conv.messages[-1].texte.startswith("```python")


def test_parse_api_ordre_chronologique_et_timestamps(api_conversation):
    """L'API renvoie le fil du plus recent au plus ancien : mapper = inverse."""
    stamps = [m.timestamp for m in api_conversation.messages]
    assert all(stamps), "chaque message doit etre horodate"
    assert stamps == sorted(stamps)
    assert api_conversation.started_at == "2026-09-24T07:47:22Z"
    assert api_conversation.last_message_at == "2026-09-24T08:05:52Z"


def test_parse_api_identifiants_et_modele_par_message(api_conversation):
    first = api_conversation.messages[0]
    assert first.message_id.startswith("r_")
    assert first.model is None  # message utilisateur
    assistant = api_conversation.messages[1]
    assert assistant.message_id.startswith("rc_")
    assert assistant.model == "gemini-3.6-flash"


def test_parse_api_code_blocks_langues(api_conversation):
    """Les annotations de fence (`?code_reference&code_event_index`) sautent."""
    msgs = api_conversation.messages
    assert "?code_" not in "".join(m.texte for m in msgs)
    audio = msgs[35]
    languages = [b.language for b in audio.code_blocks]
    assert "python" in languages and "text" in languages
    assert audio.code_blocks[0].code.startswith("import os")
    # le corps du bloc est nettoye (pas de ligne vide avant la cloture)
    assert not audio.code_blocks[0].code.endswith("\n\n")


def test_parse_api_sources_citations(api_conversation):
    """`[cite: N]` -> sources « EXT: nom » (meme libelle que le DOM)."""
    sources = {
        tuple(m.sources)
        for m in api_conversation.messages
        if m.sources
    }
    assert ("JSON: donnees.json",) in sources
    assert ("TXT: notes.txt",) in sources
    assert ("MP3: audio.mp3",) in sources
    assert ("PDF: document.pdf",) in sources
    assert "[cite:" not in "".join(m.texte for m in api_conversation.messages)


def test_parse_api_fichier_genere_lien(api_conversation):
    """`[file-tag: ..]` -> lien markdown du fichier genere (telechargeable)."""
    generated = next(
        m
        for m in api_conversation.messages
        if m.role == "assistant" and "produits.csv" in m.texte
    )
    assert "[file-tag:" not in generated.texte
    assert "[produits.csv](https://contribution.usercontent.google.com/download" in (
        generated.texte
    )


def test_parse_api_image_generee(api_conversation):
    """URL interne d'image generee -> image markdown de l'image reelle."""
    images = [
        m
        for m in api_conversation.messages
        if m.role == "assistant" and "![watermarked_img" in m.texte
    ]
    assert len(images) == 1
    assert "lh3.googleusercontent.com/gg/" in images[0].texte
    assert "image_generation_content" not in images[0].texte
    assert images[0].tools == ["Génération d'images"]


def test_parse_api_pieces_jointes_utilisateur(api_conversation):
    """Images -> `images/`, fichiers -> `files/`, medias -> apercu + fichier."""
    audio = next(
        m
        for m in api_conversation.messages
        if m.role == "user" and m.texte.startswith("Transcris cet enregistrement")
    )
    assert "![audio.mp3](https://lh3.googleusercontent.com/gg/" in audio.texte
    assert "[audio.mp3](https://contribution.usercontent.google.com/download" in (
        audio.texte
    )
    video = next(
        m
        for m in api_conversation.messages
        if m.role == "user" and "cette vidéo" in m.texte
    )
    assert "![video.mp4](" in video.texte and "[video.mp4](" in video.texte
    image = next(
        m
        for m in api_conversation.messages
        if m.role == "user" and "cette image" in m.texte
    )
    assert "![image.png](https://lh3.googleusercontent.com/gg/" in image.texte


def test_parse_api_outils_et_artefacts(api_conversation):
    msgs = api_conversation.messages
    tools = {t for m in msgs for t in m.tools}
    assert "Exécution de code" in tools
    assert "Génération d'images" in tools
    assert "Analyse vidéo" in tools
    assert "Canvas" in tools
    assert "Recherche web" in tools
    svg = next(m for m in msgs if m.artifacts)
    assert svg.artifacts == ["SVG"]
    assert "```xml" in svg.texte and "<svg" in svg.texte


def test_parse_api_latex_et_tableaux(api_conversation):
    texts = "\n".join(m.texte for m in api_conversation.messages)
    assert "$$\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}$$" in texts
    assert "| Nom | Âge |" in texts
    assert "| --- | --- |" in texts


def test_parse_api_payload_vide():
    with pytest.raises(ParseError):
        GeminiParser().parse("", extra={"api": {"turns": []}})
    with pytest.raises(ParseError):
        GeminiParser().parse("", extra={"api": {}})


def test_parse_api_reponse_vide_conserve_l_alternance():
    """Un tour sans reponse garde un assistant vide (pas de fusion user/user)."""
    payload = {
        "conversation_id": "c1",
        "turns": [
            [["c_c1", "r_1"], ["c_c1", "r_2", "rc_1"], ["q1", None, None, None, [[]]],
             [[["rc_1", []]]], [100, 0]],
            [["c_c1", "r_2"], ["c_c1", "r_3", "rc_2"], ["q2", None, None, None, [[]]],
             [[["rc_2", ["a2"]]]], [200, 0]],
        ],
    }
    conv = GeminiParser().parse("", extra={"api": payload})
    assert [m.role for m in conv.messages] == ["user", "assistant", "user", "assistant"]
    assert conv.messages[1].texte == ""
    assert conv.messages[1].message_id == "rc_1"
    assert conv.messages[3].texte == "a2"


def test_parse_api_normalisation_annotations():
    """Cas synthetiques : fence annotee, cite, file-tag, puce `*`, item vide."""
    payload = {
        "conversation_id": "c1",
        "title": "T",
        "turns": [
            [
                ["c_c1", "r_u1"],
                ["c_c1", "r_u2", "rc_a1"],
                ["question", None, None, None, [[]]],
                [
                    [
                        [
                            "rc_a1",
                            [
                                "* puce un\n* puce deux\n\n```python?code_reference"
                                "&code_event_index=1\nprint(1)\n\n```\n\n"
                                "```text?code_stdout&code_event_index=1\n1\n\n```\n\n"
                                "`true`[cite: 5]"
                            ],
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            [2],
                            "fr",
                            None,
                            None,
                            [
                                {
                                    "44": [
                                        [
                                            ["[cite: 5]", 0],
                                            [
                                                [
                                                    None,
                                                    [
                                                        [
                                                            None,
                                                            0,
                                                            "donnees.json",
                                                            None,
                                                            None,
                                                            None,
                                                            None,
                                                            None,
                                                            None,
                                                            None,
                                                            None,
                                                            "application/json",
                                                        ]
                                                    ],
                                                ]
                                            ],
                                            ["`true`", 0],
                                        ]
                                    ]
                                }
                            ],
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                            None,
                        ],
                    ],
                    None,
                    "gemini-test",
                ],
                [1790236042, 0],
            ]
        ],
    }
    conv = GeminiParser().parse("", extra={"api": payload})
    assert [m.role for m in conv.messages] == ["user", "assistant"]
    text = conv.messages[1].texte
    assert "- puce un\n- puce deux" in text
    assert "```python\nprint(1)\n```" in text
    assert "```text\n1\n```" in text
    assert "[cite:" not in text
    assert conv.messages[1].sources == ["JSON: donnees.json"]
    assert conv.messages[1].model == "gemini-test"
    assert conv.messages[1].timestamp == "2026-09-24T07:47:22Z"


# -- comparaison a l'export DOM gele ----------------------------------------


def test_parse_api_matches_dom_reference(api_conversation, dom_reference):
    """Meme contenu que le DOM (apparie par requete utilisateur)."""
    dom_msgs = dom_reference["messages"]
    dom_by_key = {}
    for index, message in enumerate(dom_msgs):
        if message["role"] == "user":
            dom_by_key.setdefault(normalize_for_match(message["texte"], 40), index)
    matched = 0
    for index, message in enumerate(api_conversation.messages):
        if message.role != "user":
            continue
        dom_index = dom_by_key.get(normalize_for_match(message.texte, 40))
        # l'API ajoute le lien de la piece jointe : on retombe sur le nom seul
        if dom_index is None:
            stripped = message.texte.split("\n\n")[0]
            dom_index = dom_by_key.get(normalize_for_match(stripped, 40))
        assert dom_index is not None, f"requete absente du DOM: {message.texte[:60]!r}"
        matched += 1
        api_assistant = api_conversation.messages[index + 1]
        dom_assistant = dom_msgs[dom_index + 1]
        assert api_assistant.role == dom_assistant["role"] == "assistant"
        assert len(api_assistant.code_blocks) >= len(dom_assistant["code_blocks"])
        assert (api_assistant.sources or []) == (dom_assistant.get("sources") or [])
        # les sorties de code gagnent leur langue (`text`) cote API
        if (api_assistant.tools or []) != (dom_assistant.get("tools") or []):
            assert set(dom_assistant.get("tools") or []) <= set(api_assistant.tools)
        assert _norm(api_assistant.texte) != "" or _norm(dom_assistant["texte"]) == ""
    assert matched == 30


def test_parse_api_aucune_regression_metriques(api_conversation, dom_reference):
    """Les metriques de l'etalon ne baissent pas par rapport au DOM."""
    api_metrics = conversation_metrics(api_conversation.to_dict(validate=False))
    dom_metrics = conversation_metrics(dom_reference)
    assert api_metrics["messages"] == dom_metrics["messages"] == 60
    assert api_metrics["consecutive_roles"] == 0
    assert regressions(api_metrics, dom_metrics) == []


def test_parse_api_aucune_capacite_perdue(api_conversation, dom_reference):
    """Toutes les capacites detectables du DOM (HTML inclus) restent couvertes."""
    api_caps = detect_capabilities(api_conversation.to_dict(validate=False), "")
    dom_caps = detect_capabilities(
        dom_reference, ETALON_DOM_HTML.read_text(encoding="utf-8")
    )
    lost = (dom_caps & DETECTABLE) - api_caps
    assert not lost, sorted(lost)
    # l'API conserve les fichiers joints et les apercus media du DOM
    assert {"audio", "video", "upload_image", "upload_csv", "upload_json"} <= api_caps


# -- client HTTP -------------------------------------------------------------


def test_decode_batchexecute():
    text = (
        ")]}'\n\n123\n"
        '[["wrb.fr","hNvQHb","[null,[],null]",null,null,null,"generic"],'
        '["di",2],["af.httprm",1,"-1",2]]\n'
        '25\n[["e",4,null,null,123]]'
    )
    assert _decode_batchexecute(text, "hNvQHb") == [None, [], None]
    assert _decode_batchexecute(text, "MaZiqc") is None
    assert _decode_batchexecute("", "hNvQHb") is None


def test_is_auth_error():
    assert _is_auth_error(
        ')]}\'\n\n102\n[["er",null,null,null,null,401,null,null,null,16],["di",5]]'
    )
    assert not _is_auth_error(')]}\'\n\n102\n[["wrb.fr","MaZiqc","[]"]]')


def test_soft_error_code():
    from src.services.gemini import _soft_error_code

    text = (
        ')]}\'\n\n194\n[["wrb.fr","MaZiqc",null,null,null,[13,null,[["type.'
        'googleapis.com/assistant.boq.bard.application.BardErrorInfo",[1096]]]],'
        '"generic"],["di",10086]]'
    )
    assert _soft_error_code(text) == 1096
    assert _soft_error_code(')]}\'\n[["er",null,null,null,null,429,null,null,null,16]]') == 429
    assert _soft_error_code(')]}\'\n[["wrb.fr","MaZiqc","[1]"]]') is None


def test_http_client_reessaie_sur_erreur_applicative(monkeypatch):
    """Payload nul + BardErrorInfo (limite temporaire) : attente puis nouvel essai."""
    import time as time_module
    import urllib.request

    responses = [
        b'[["wrb.fr","MaZiqc",null,null,null,[13,null,[["type.googleapis.com/'
        b'assistant.boq.bard.application.BardErrorInfo",[1096]]]],"generic"]]',
        b'[["wrb.fr","MaZiqc","[1,2]",null,null,null,"generic"]]',
    ]
    sleeps: List[float] = []

    class FakeResponse:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def read(self):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout=None):
        return FakeResponse(responses.pop(0))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(time_module, "sleep", lambda s: sleeps.append(s))
    client = _GeminiHttpClient([], {"at": "tok"}, user_agent="UA")
    assert client.rpc("MaZiqc", [100, None, []], "/app") == [1, 2]
    assert sleeps == [30.0]  # backoff de base avant le second essai


def test_cookie_header_filtre_par_domaine():
    client = _GeminiHttpClient(
        [
            {"name": "SID", "value": "s", "domain": ".google.com"},
            {"name": "GEM", "value": "g", "domain": ".gemini.google.com"},
            {"name": "YT", "value": "y", "domain": ".youtube.com"},
            {"name": "HOST", "value": "h", "domain": "gemini.google.com"},
        ],
        {"at": "tok"},
        user_agent="UA",
    )
    header = client._cookie_header("https://gemini.google.com/_/BardChatUi/data")
    assert "SID=s" in header and "GEM=g" in header and "HOST=h" in header
    assert "YT=y" not in header


def test_http_client_reessaie_sur_429(monkeypatch):
    import time as time_module
    import urllib.error
    import urllib.request

    calls = {"n": 0}
    sleeps: List[float] = []

    class FakeResponse:
        status = 200

        def read(self):
            return b'[["wrb.fr","MaZiqc","[1,2]",null,null,null,"generic"]]'

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
    client = _GeminiHttpClient(
        [{"name": "SID", "value": "s", "domain": ".google.com"}],
        {"at": "tok", "sid": "1", "bl": "b"},
        user_agent="UA",
    )
    status, raw = client.post("MaZiqc", [100, None, []], "/app")
    assert status == 200
    assert calls["n"] == 2
    assert sleeps == [7.0]  # Retry-After respecte
    assert _decode_batchexecute(raw, "MaZiqc") == [1, 2]


def test_http_client_401_leve_api_auth_error(monkeypatch):
    import urllib.error
    import urllib.request

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = _GeminiHttpClient([], {"at": "tok"}, user_agent="UA")
    with pytest.raises(_ApiAuthError):
        client.rpc("MaZiqc", [100, None, []], "/app")


# -- service -----------------------------------------------------------------


class FakeSession:
    """Session navigateur factice (jetons + cookies du profil)."""

    def __init__(self, tokens=None, cookies=None):
        self._tokens = tokens if tokens is not None else {
            "at": "tok-123", "sid": "42", "bl": "boq_test",
        }
        self._cookies = cookies if cookies is not None else [
            {"name": "SID", "value": "s", "domain": ".google.com"},
        ]
        self.fingerprint = {"user_agent": "UA-test", "lang": "fr-FR"}
        self.gotos: List[str] = []
        self.evaluated: List[str] = []

    def goto(self, url):
        self.gotos.append(url)

    def wait_ms(self, ms):
        pass

    def wait_for_any(self, selectors, timeout_ms=None):
        return selectors[0] if selectors else None

    def cookies(self):
        return list(self._cookies)

    def evaluate(self, script, arg=None):
        self.evaluated.append(script)
        return dict(self._tokens)


class FakeClient:
    """Client API factice : renvoie des payloads dans l'ordre."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls: List[tuple] = []

    def rpc(self, rpcid, payload, source_path):
        self.calls.append((rpcid, payload, source_path))
        if not self.payloads:
            raise AssertionError(f"appel inattendu: {rpcid}")
        return self.payloads.pop(0)


def test_api_desactivee_par_defaut():
    service = GeminiService(FakeSession(), {"services": {"gemini": {}}})
    assert service.api_enabled is False
    assert GeminiService(FakeSession(), {}).api_enabled is False


def test_api_activee_par_config():
    service = GeminiService(FakeSession(), {"services": {"gemini": {"api": True}}})
    assert service.api_enabled is True


def test_api_client_extrait_les_jetons():
    session = FakeSession()
    service = GeminiService(session, {"services": {"gemini": {"api": True}}})
    client = service._api_client()
    assert isinstance(client, _GeminiHttpClient)
    assert client.at == "tok-123" and client.sid == "42" and client.bl == "boq_test"
    assert client.user_agent == "UA-test"
    assert client.accept_language == "fr-FR,fr;q=0.9"
    assert session.gotos == ["https://gemini.google.com/app"]
    assert service._api_client() is client  # memorise


def test_api_client_sans_jetons_leve_service_not_logged_in():
    service = GeminiService(
        FakeSession(tokens={"at": None}), {"services": {"gemini": {"api": True}}}
    )
    with pytest.raises(ServiceNotLoggedIn):
        service._api_client()


def test_api_client_sans_cookie_leve_service_not_logged_in():
    service = GeminiService(
        FakeSession(cookies=[]), {"services": {"gemini": {"api": True}}}
    )
    with pytest.raises(ServiceNotLoggedIn):
        service._api_client()


def test_list_conversations_api_pagination():
    full_page = [
        None,
        "curseur-1",
        [
            [f"c_{i:016x}", f"T{i}", None, None, None, [1791741066, 877478000]]
            for i in range(API_LIST_PAGE_SIZE)
        ],
    ]
    last_page = [None, None, [["c_final0000000001", "Dernier", None, None, None, None]]]
    service = GeminiService(FakeSession(), {"services": {"gemini": {"api": True}}})
    client = FakeClient([full_page, last_page])
    service._http = client
    refs = service.list_conversations()
    assert len(refs) == API_LIST_PAGE_SIZE + 1
    assert refs[0].id == "0000000000000000"
    assert refs[0].url == "https://gemini.google.com/app/0000000000000000"
    assert refs[0].raw["last_message_at"] == "2026-10-11T17:51:06Z"
    assert refs[-1].id == "final0000000001"
    assert client.calls[0][1][0] == API_LIST_PAGE_SIZE
    assert client.calls[1][1][1] == "curseur-1"


def test_list_conversations_api_limite():
    page = [
        None,
        None,
        [[f"c_{i:016x}", f"T{i}", None, None, None, None] for i in range(10)],
    ]
    service = GeminiService(FakeSession(), {"services": {"gemini": {"api": True}}})
    service._http = FakeClient([page])
    refs = service.list_conversations(limit=3)
    assert [r.id for r in refs] == ["0000000000000000", "0000000000000001", "0000000000000002"]


def test_fetch_conversation_api_ordre_chronologique():
    """Les pages arrivent du plus recent au plus ancien : sortie inversee."""
    turn_1 = [["c_c1", "r_1"], ["c_c1", "r_2", "rc_1"], ["q1", None, None, None, [[]]],
              [[["rc_1", ["a1"]]]], [100, 0]]
    turn_2 = [["c_c1", "r_2"], ["c_c1", "r_3", "rc_2"], ["q2", None, None, None, [[]]],
              [[["rc_2", ["a2"]]]], [200, 0]]
    turn_3 = [["c_c1", "r_3"], ["c_c1", "r_4", "rc_3"], ["q3", None, None, None, [[]]],
              [[["rc_3", ["a3"]]]], [300, 0]]
    page_newest = [turn_3, turn_2]
    page_older = [turn_1]
    service = GeminiService(FakeSession(), {"services": {"gemini": {"api": True}}})
    client = FakeClient([[page_newest, "curseur-1"], [page_older, None]])
    service._http = client
    turns = service._fetch_conversation_api("c1")
    texts = [turn[2][0] for turn in turns]
    assert texts == ["q1", "q2", "q3"]
    assert client.calls[0][1][1] == API_TURNS_PAGE_SIZE
    assert client.calls[1][1][2] == "curseur-1"


def test_export_conversation_api_sans_html_avec_payload_brut(api_payload):
    """Mode API : pas de HTML, payload brut disponible pour l'archivage."""
    service = GeminiService(FakeSession(), {"services": {"gemini": {"api": True}}})
    # l'API renvoie les tours du plus recent au plus ancien
    newest_first = list(reversed(api_payload["turns"]))
    service._http = FakeClient([[newest_first, None]])
    ref = ConversationRef(
        service="gemini",
        id=api_payload["conversation_id"],
        url="https://gemini.google.com/app/" + api_payload["conversation_id"],
        title=api_payload["title"],
    )
    conv, html = service.export_conversation_with_html(ref)
    assert html is None
    assert len(conv.messages) == 60
    assert conv.url == ref.url
    assert conv.title == api_payload["title"]
    payload = service.take_raw_payload()
    assert payload["conversation_id"] == api_payload["conversation_id"]
    assert payload["turns"] == api_payload["turns"]
    # consomme : un export ulterieur ne le reecrit pas
    assert service.take_raw_payload() is None
    # pas de screenshots en mode API
    assert service.capture_message_screenshots(conv, Path("/tmp/unused")) == 0


def test_export_conversation_api_vide_leve_parse_error():
    service = GeminiService(FakeSession(), {"services": {"gemini": {"api": True}}})
    service._http = FakeClient([[[], None]])
    ref = ConversationRef(service="gemini", id="c1", url="https://gemini.google.com/app/c1")
    with pytest.raises(ParseError):
        service.export_conversation_with_html(ref)


def test_rpc_rafraichit_les_jetons_sur_401():
    """Un 401 en cours de run relit les jetons une fois puis rejoue l'appel."""

    class FlakyClient:
        def __init__(self, ok: bool):
            self.ok = ok
            self.calls = 0

        def rpc(self, rpcid, payload, source_path):
            self.calls += 1
            if not self.ok:
                raise _ApiAuthError("expire")
            return ["ok"]

    service = GeminiService(FakeSession(), {"services": {"gemini": {"api": True}}})
    first = FlakyClient(ok=False)
    second = FlakyClient(ok=True)
    clients = [first, second]
    refreshes: List[bool] = []

    def fake_client():
        refreshes.append(True)
        return clients.pop(0)

    service._api_client = fake_client  # type: ignore[method-assign]
    assert service._rpc("MaZiqc", [1], "/app") == ["ok"]
    assert first.calls == 1 and second.calls == 1
    assert len(refreshes) == 2  # client initial + rafraichissement
    assert service._http is None  # l'ancien client est invalide
