"""Tests du serveur web local (scripts/serve_web.py).

Serveur HTTP reel sur un port ephemere + dossier exports factice. Verifie la
validation des parametres (400 au lieu de 500) et l'absence de traversee de
chemin dans les routes API et /media.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "serve_web", ROOT / "scripts" / "serve_web.py"
)
serve_web = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules.setdefault("serve_web", serve_web)
spec.loader.exec_module(serve_web)

CONVERSATION = {
    "conversation_id": "g1",
    "platform": "grok",
    "title": "Test web",
    "url": "https://grok.com/chat/g1",
    "model": None,
    "started_at": "2026-05-01T09:00:00Z",
    "last_message_at": "2026-05-01T09:01:00Z",
    "exported_at": "2026-05-02T00:00:00Z",
    "messages": [
        {
            "message_id": "m1",
            "role": "user",
            "texte": "bonjour",
            "code_blocks": [],
        }
    ],
}


def _get(base: str, path: str):
    """GET -> (statut, corps JSON ou bytes)."""
    try:
        with urllib.request.urlopen(base + path, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _get_json(base: str, path: str):
    status, body = _get(base, path)
    return status, json.loads(body.decode("utf-8"))


@pytest.fixture()
def web(tmp_path):
    exports = tmp_path / "exports"
    platform = exports / "grok"
    platform.mkdir(parents=True)
    (platform / "conv1.json").write_text(
        json.dumps(CONVERSATION), encoding="utf-8"
    )
    (platform / "conversation_list.json").write_text(
        json.dumps(
            {
                "platform": "grok",
                "count": 1,
                "conversations": [
                    {
                        "conversation_id": "g1",
                        "title": "Test web",
                        "file": "conv1",
                        "scraped": True,
                        "message_count": 1,
                        "last_message_at": "2026-05-01T09:01:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    images = platform / "images"
    images.mkdir()
    (images / "img1.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    # fichier hors de exports/, avec un conversation_id cible (traversee)
    (tmp_path / "secret.json").write_text(
        json.dumps(
            {
                "conversation_id": "secret1",
                "platform": "inconnu",
                "title": "hors exports",
                "messages": [],
            }
        ),
        encoding="utf-8",
    )
    # dossier platforme hors liste blanche, avec un fichier "images"
    evil = exports / "evil"
    (evil / "images").mkdir(parents=True)
    (evil / "images" / "img1.png").write_bytes(b"evil")

    serve_web.Handler.state = serve_web.AppState(
        db_path=tmp_path / "rag.db",
        exports_dir=exports,
        model_name="dummy",
    )
    server = serve_web.ThreadingHTTPServer(("127.0.0.1", 0), serve_web.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class TestValidationConversation:
    def test_plateforme_traversee_400(self, web):
        """`platform=..` lisait un JSON hors de exports/ (finding d'audit)."""
        for platform in ("..", "../..", "%2Fetc", "/etc"):
            status, body = _get_json(
                web, "/api/conversation?" + urllib.parse.urlencode(
                    {"platform": platform, "id": "secret1"}
                )
            )
            assert status == 400, platform
            assert "plateforme inconnue" in body["error"]

    def test_identifiant_traversee_400(self, web):
        for cid in ("../secret1", "a/b", "a\\b", ""):
            status, _ = _get_json(
                web,
                "/api/conversation?"
                + urllib.parse.urlencode({"platform": "grok", "id": cid}),
            )
            assert status == 400, cid

    def test_parametres_manquants_400(self, web):
        status, _ = _get_json(web, "/api/conversation?platform=grok")
        assert status == 400

    def test_conversation_valide_200(self, web):
        status, body = _get_json(web, "/api/conversation?platform=grok&id=g1")
        assert status == 200
        assert body["conversation"]["conversation_id"] == "g1"
        assert body["conversation"]["title"] == "Test web"

    def test_conversations_plateforme_inconnue_400(self, web):
        status, _ = _get_json(web, "/api/conversations?platform=..")
        assert status == 400

    def test_conversations_valides_200(self, web):
        status, body = _get_json(web, "/api/conversations")
        assert status == 200
        assert [c["conversation_id"] for c in body["conversations"]] == ["g1"]
        status, body = _get_json(web, "/api/conversations?platform=grok")
        assert status == 200 and body["count"] == 1


class TestValidationRecherche:
    def test_k_invalide_400(self, web):
        status, body = _get_json(web, "/api/search?q=test&k=abc")
        assert status == 400
        assert "k invalide" in body["error"]
        for k in ("0", "-3", "501"):
            status, _ = _get_json(web, f"/api/search?q=test&k={k}")
            assert status == 400, k

    def test_alpha_invalide_400(self, web):
        status, body = _get_json(web, "/api/search?q=test&alpha=abc")
        assert status == 400
        assert "alpha invalide" in body["error"]
        for alpha in ("-0.1", "1.5"):
            status, _ = _get_json(web, f"/api/search?q=test&alpha={alpha}")
            assert status == 400, alpha

    def test_plateforme_inconnue_400(self, web):
        status, _ = _get_json(web, "/api/search?q=test&platforms=bogus")
        assert status == 400

    def test_recherche_valide_200(self, web, monkeypatch):
        import numpy as np

        monkeypatch.setattr(
            serve_web.AppState,
            "embed",
            lambda self, text: np.array([1.0] + [0.0] * 1023, dtype="float32"),
        )
        status, body = _get_json(web, "/api/search?q=test&k=5&alpha=0.5")
        assert status == 200
        assert body["query"] == "test"
        assert body["groups"] == []


class TestMedia:
    def test_plateforme_hors_liste_403(self, web):
        status, _ = _get(web, "/media/evil/img1.png")
        assert status == 403

    def test_media_valide_200(self, web):
        status, body = _get(web, "/media/grok/img1.png")
        assert status == 200
        assert body.startswith(b"\x89PNG")
