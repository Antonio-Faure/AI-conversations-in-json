"""Tests du serveur web local (scripts/serve_web.py).

Serveur HTTP reel sur un port ephemere + dossier exports factice. Verifie la
validation des parametres (400 au lieu de 500) et l'absence de traversee de
chemin dans les routes API et /media.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
import sqlite_vec

from src.utils import webauth

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


def _post_json(base: str, path: str, payload):
    """POST JSON -> (statut, corps JSON)."""
    request = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _seed_messages(store, rows):
    """Insere des messages bruts (sans vecteurs) dans la base de test."""
    for row in rows:
        store.db.execute(
            """
            INSERT INTO messages
                (message_id, conversation_id, platform, role, model,
                 timestamp, texte, has_code, conversation_file, fingerprint)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL)
            """,
            (
                row.get("message_id", ""),
                row["conversation_id"],
                row["platform"],
                row.get("role", "user"),
                row.get("model", ""),
                row.get("timestamp", ""),
                row.get("texte", "x"),
            ),
        )
    store.db.commit()


def _seed_conversations(store, rows):
    """Insere des conversations indexees (tirage aleatoire, compteurs)."""
    for row in rows:
        store.db.execute(
            """
            INSERT INTO conversations
                (platform, conversation_id, title, url, exported_at,
                 message_count, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["platform"],
                row["conversation_id"],
                row.get("title", ""),
                row.get("url", ""),
                row.get("exported_at", ""),
                row.get("message_count", 1),
                row.get("indexed_at", "2026-01-01T00:00:00Z"),
            ),
        )
    store.db.commit()


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
    serve_web.Handler.auth = None  # pas d'authentification (tests historiques)
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


class TestRechercheFiltresSeuls:
    def _seed(self):
        store = serve_web.Handler.state.store()
        _seed_conversations(store, [
            {"platform": "grok", "conversation_id": "s1", "title": "Filtre grok",
             "url": "https://grok.com/chat/s1", "message_count": 2},
            {"platform": "gemini", "conversation_id": "s2", "title": "Filtre gemini",
             "message_count": 1},
        ])
        _seed_messages(store, [
            {"platform": "grok", "conversation_id": "s1", "message_id": "f1",
             "model": "grok-3", "timestamp": "2026-03-01T08:00:00Z", "texte": "un"},
            {"platform": "grok", "conversation_id": "s1", "message_id": "f2",
             "model": "grok-3", "timestamp": "2026-03-02T09:00:00Z", "texte": "deux"},
            {"platform": "gemini", "conversation_id": "s2", "message_id": "f3",
             "model": "gemini-x", "timestamp": "2026-03-02T10:00:00Z", "texte": "trois"},
        ])

    def test_q_vide_avec_plateforme(self, web):
        """q vide + filtre : 200 groupe, tri date desc, aucun score."""
        self._seed()
        status, body = _get_json(web, "/api/search?q=&platforms=grok")
        assert status == 200
        assert body["filtered"] is True
        assert body["count"] == 2
        assert body["group_count"] == 1
        group = body["groups"][0]
        assert (group["platform"], group["title"]) == ("grok", "Filtre grok")
        assert group["hit_count"] == 2
        assert group["best_score"] is None
        assert [m["timestamp"] for m in group["messages"]] == [
            "2026-03-02T09:00:00Z", "2026-03-01T08:00:00Z"]
        assert all(
            m["score"] is None and m["semantic_score"] is None
            and m["keyword_score"] is None
            for m in group["messages"]
        )

    def test_q_vide_avec_modele(self, web):
        self._seed()
        status, body = _get_json(web, "/api/search?q=&model=gemini-x")
        assert status == 200
        assert body["count"] == 1
        assert body["groups"][0]["conversation_id"] == "s2"

    def test_q_vide_avec_dates_inclusives(self, web):
        """La borne de fin de plage inclut toute la journee."""
        self._seed()
        status, body = _get_json(
            web, "/api/search?q=&date_from=2026-03-02&date_to=2026-03-02"
        )
        assert status == 200
        assert body["count"] == 2
        assert body["group_count"] == 2

    def test_q_vide_sans_filtre_reponse_vide(self, web):
        """Sans texte ni critere : reponse vide (comportement historique)."""
        self._seed()
        status, body = _get_json(web, "/api/search?q=")
        assert status == 200
        assert body["count"] == 0
        assert body["groups"] == []
        assert "filtered" not in body

    def test_filtre_sans_resultat(self, web):
        self._seed()
        status, body = _get_json(web, "/api/search?q=&platforms=claude")
        assert status == 200
        assert body["count"] == 0 and body["groups"] == []


class TestRechercheMedias:
    def _seed(self):
        """3 medias indexes (2 images chatgpt, 1 audio gemini) + titres."""
        state = serve_web.Handler.state
        _seed_conversations(state.store(), [
            {"platform": "chatgpt", "conversation_id": "c1", "title": "Conv image",
             "url": "https://chatgpt.com/c/c1", "message_count": 1},
        ])
        rows = [
            {"media_id": "chatgpt/images/aaa.jpg", "platform": "chatgpt",
             "conversation_id": "c1", "message_id": "m1", "kind": "image",
             "path": "chatgpt/images/aaa.jpg", "texte": "un chat"},
            {"media_id": "chatgpt/images/bbb.png", "platform": "chatgpt",
             "conversation_id": "", "message_id": "", "kind": "image",
             "path": "chatgpt/images/bbb.png", "texte": "orpheline"},
            {"media_id": "gemini/files/ccc.mp3", "platform": "gemini",
             "conversation_id": "", "message_id": "", "kind": "audio",
             "path": "gemini/files/ccc.mp3", "texte": "audio.mp3"},
        ]
        media_store = state.media_store()
        media_store.replace(rows, [np.ones(state.dim, dtype="float32")] * len(rows))

    def test_medias_renvoyes_avec_recherche(self, web, monkeypatch):
        self._seed()
        monkeypatch.setattr(
            serve_web.AppState,
            "embed",
            lambda self, text: np.ones(self.dim, dtype="float32"),
        )
        status, body = _get_json(web, "/api/search?q=chat")
        assert status == 200
        # media rattaches a leur conversation : un media = un hit du groupe
        groups = {g["conversation_id"]: g for g in body["groups"]}
        assert "c1" in groups
        attached = groups["c1"]["media"]
        assert len(attached) == 1
        item = attached[0]
        assert item["kind"] == "image"
        assert item["path"] == "chatgpt/images/aaa.jpg"
        assert item["message_id"] == "m1"
        assert item["title"] == "Conv image"  # titre joint depuis l'index
        assert item["score"] is not None
        assert item["url"] == "/media/chatgpt/aaa.jpg"
        # orphelins (sans conversation) : cartes autonomes
        media = body["media"]
        assert len(media) == 2
        assert {m["kind"] for m in media} == {"image", "audio"}
        for item in media:
            assert item["score"] is not None
            assert item["url"] == f"/media/{item['platform']}/{item['path'].rsplit('/', 1)[-1]}"
        orphan = next(m for m in media if m["path"] == "chatgpt/images/bbb.png")
        assert orphan["conversation_id"] is None
        assert orphan["title"] == ""

    def test_media_rattache_au_groupe_existant(self, web, monkeypatch):
        """Un media rejoint le groupe de sa conversation (hit texte deja present).

        Regression : le groupe existant n'avait pas encore de cle `media`.
        """
        state = serve_web.Handler.state
        _seed_conversations(state.store(), [
            {"platform": "chatgpt", "conversation_id": "c9", "title": "Conv mixte",
             "url": "https://chatgpt.com/c/c9", "message_count": 2},
        ])
        _seed_messages(state.store(), [
            {"message_id": "m9", "conversation_id": "c9", "platform": "chatgpt",
             "timestamp": "2026-01-01T00:00:00Z", "texte": "chat chat"},
        ])
        rows = [
            {"media_id": "chatgpt/images/mix.png", "platform": "chatgpt",
             "conversation_id": "c9", "message_id": "m9", "kind": "image",
             "path": "chatgpt/images/mix.png", "texte": "chat"},
        ]
        state.media_store().replace(rows, [np.ones(state.dim, dtype="float32")])
        monkeypatch.setattr(
            serve_web.AppState,
            "embed",
            lambda self, text: np.ones(self.dim, dtype="float32"),
        )
        status, body = _get_json(web, "/api/search?q=chat")
        assert status == 200
        groups = {g["conversation_id"]: g for g in body["groups"]}
        assert "c9" in groups
        group = groups["c9"]
        assert len(group["messages"]) >= 1  # hit texte (mots-cles)
        assert len(group["media"]) == 1  # media rattache au groupe existant
        assert group["hit_count"] == len(group["messages"]) + 1
        assert body["media"] == []  # aucun orphelin

    def test_filtre_plateforme(self, web, monkeypatch):
        self._seed()
        monkeypatch.setattr(
            serve_web.AppState,
            "embed",
            lambda self, text: np.ones(self.dim, dtype="float32"),
        )
        status, body = _get_json(web, "/api/search?q=chat&platforms=gemini")
        assert status == 200
        assert [m["platform"] for m in body["media"]] == ["gemini"]

    def test_media_k(self, web, monkeypatch):
        self._seed()
        monkeypatch.setattr(
            serve_web.AppState,
            "embed",
            lambda self, text: np.ones(self.dim, dtype="float32"),
        )
        status, body = _get_json(web, "/api/search?q=chat&media_k=1")
        assert status == 200
        # le media peut etre rattache a un groupe ou rester orphelin
        attached = sum(len(g.get("media") or []) for g in body["groups"])
        assert attached + len(body["media"]) == 1
        for value in ("0", "25", "abc"):
            status, _ = _get_json(web, f"/api/search?q=chat&media_k={value}")
            assert status == 400, value

    def test_q_vide_aucun_media(self, web):
        """Critères seuls ou requête vide : pas de vecteur -> media vide."""
        self._seed()
        status, body = _get_json(web, "/api/search?q=&platforms=grok")
        assert status == 200
        assert body["media"] == []
        status, body = _get_json(web, "/api/search?q=")
        assert status == 200
        assert body["media"] == []


class TestMedia:
    def test_plateforme_hors_liste_403(self, web):
        status, _ = _get(web, "/media/evil/img1.png")
        assert status == 403

    def test_media_valide_200(self, web):
        status, body = _get(web, "/media/grok/img1.png")
        assert status == 200
        assert body.startswith(b"\x89PNG")


class TestActivity:
    def test_fenetre_de_365_jours(self, web):
        """365 jours zero-completes, hors fenetre exclu, max = pic."""
        store = serve_web.Handler.state.store()
        today = datetime.now(timezone.utc).date()
        yesterday = today - timedelta(days=1)
        old = today - timedelta(days=400)
        _seed_messages(store, [
            {"platform": "grok", "conversation_id": "g1", "timestamp": f"{today}T08:00:00Z"},
            {"platform": "grok", "conversation_id": "g1", "timestamp": f"{today}T09:00:00Z"},
            {"platform": "grok", "conversation_id": "g1", "timestamp": f"{yesterday}T10:00:00Z"},
            {"platform": "grok", "conversation_id": "g1", "timestamp": f"{old}T10:00:00Z"},
            {"platform": "grok", "conversation_id": "g1", "timestamp": ""},
        ])
        status, body = _get_json(web, "/api/activity")
        assert status == 200
        days = body["days"]
        assert len(days) == 365
        assert days[today.isoformat()] == 2
        assert days[yesterday.isoformat()] == 1
        assert days[(today - timedelta(days=364)).isoformat()] == 0
        assert old.isoformat() not in days
        assert body["max"] == 2

    def test_sans_messages(self, web):
        status, body = _get_json(web, "/api/activity")
        assert status == 200
        assert len(body["days"]) == 365
        assert body["max"] == 0
        assert set(body["days"].values()) == {0}


class TestRandom:
    def _seed(self):
        store = serve_web.Handler.state.store()
        _seed_conversations(store, [
            {"platform": "grok", "conversation_id": "r1", "title": "Un",
             "url": "https://grok.com/chat/r1", "message_count": 2},
            {"platform": "claude", "conversation_id": "r2", "title": "Deux",
             "message_count": 1},
            {"platform": "gemini", "conversation_id": "r3", "title": "Trois",
             "message_count": 3},
            {"platform": "mistral", "conversation_id": "vide", "title": "Vide",
             "message_count": 0},
        ])
        _seed_messages(store, [
            {"platform": "grok", "conversation_id": "r1", "message_id": "m1",
             "timestamp": "2026-05-01T09:00:00Z"},
            {"platform": "grok", "conversation_id": "r1", "message_id": "m2",
             "timestamp": "2026-05-01T09:01:00Z"},
        ])

    def test_n_absent_format_historique(self, web):
        """Sans n : objet simple (compat bouton « Aléatoire »), vides exclus."""
        self._seed()
        status, body = _get_json(web, "/api/random")
        assert status == 200
        assert "conversations" not in body
        assert body["conversation_id"] in ("r1", "r2", "r3")
        assert body["url"]

    def test_n_1_format_historique(self, web):
        self._seed()
        status, body = _get_json(web, "/api/random?n=1")
        assert status == 200
        assert "conversations" not in body

    def test_quatre_conversations_dedupliquees(self, web):
        self._seed()
        status, body = _get_json(web, "/api/random?n=4")
        assert status == 200
        items = body["conversations"]
        assert len(items) == 3  # 3 conversations non vides, 1 exclue
        keys = {(c["platform"], c["conversation_id"]) for c in items}
        assert len(keys) == 3
        assert "vide" not in {c["conversation_id"] for c in items}
        # enrichissement : compteur + date du dernier message
        assert all(c.get("message_count") is not None for c in items)
        assert all(c.get("url") for c in items)

    def test_n_invalide_400(self, web):
        for n in ("0", "-1", "9", "abc"):
            status, _ = _get_json(web, f"/api/random?n={n}")
            assert status == 400, n


class TestDay:
    def _seed(self):
        store = serve_web.Handler.state.store()
        _seed_conversations(store, [
            {"platform": "grok", "conversation_id": "d1", "title": "Jour grok",
             "url": "https://grok.com/chat/d1", "message_count": 2},
            {"platform": "gemini", "conversation_id": "d2", "title": "Jour gemini",
             "message_count": 1},
        ])
        _seed_messages(store, [
            {"platform": "grok", "conversation_id": "d1", "message_id": "a1",
             "role": "user", "model": "grok-3", "timestamp": "2026-05-01T08:00:00Z",
             "texte": "bonjour"},
            {"platform": "grok", "conversation_id": "d1", "message_id": "",
             "role": "assistant", "timestamp": "2026-05-01T08:01:00Z",
             "texte": "mot " * 200},
            {"platform": "gemini", "conversation_id": "d2", "message_id": "c1",
             "timestamp": "2026-05-01T07:00:00Z", "texte": "autre conv"},
            {"platform": "grok", "conversation_id": "d1", "message_id": "a2",
             "timestamp": "2026-05-02T08:00:00Z", "texte": "hors jour"},
        ])

    def test_jour_groupe_et_compteurs(self, web):
        """Groupement par conversation, tri horaire, titres de l'index."""
        self._seed()
        status, body = _get_json(web, "/api/day?date=2026-05-01")
        assert status == 200
        assert body["date"] == "2026-05-01"
        assert body["message_count"] == 3
        assert body["conversation_count"] == 2
        # ordre chronologique : gemini (07:00) avant grok (08:00)
        first, second = body["conversations"]
        assert (first["platform"], first["title"]) == ("gemini", "Jour gemini")
        assert first["message_count"] == 1
        assert first["url"].startswith("https://")  # URL calculee si absente
        assert (second["platform"], second["title"]) == ("grok", "Jour grok")
        assert second["url"] == "https://grok.com/chat/d1"
        assert second["message_count"] == 2
        # message_id vide -> null ; modele conserve
        assert [m["message_id"] for m in second["messages"]] == ["a1", None]
        assert [m["role"] for m in second["messages"]] == ["user", "assistant"]
        assert second["messages"][0]["model"] == "grok-3"

    def test_extrait_tronque_et_une_ligne(self, web):
        self._seed()
        _, body = _get_json(web, "/api/day?date=2026-05-01")
        grok = [c for c in body["conversations"] if c["platform"] == "grok"][0]
        extract = grok["messages"][1]["texte"]
        assert len(extract) <= 301  # ~300 + ellipse
        assert extract.endswith("…")
        assert "\n" not in extract

    def test_jour_sans_message(self, web):
        status, body = _get_json(web, "/api/day?date=2020-01-01")
        assert status == 200
        assert body["message_count"] == 0
        assert body["conversation_count"] == 0
        assert body["conversations"] == []

    def test_date_invalide_400(self, web):
        for date in ("2026-13-45", "2026-02-30", "2026-5-1", "20260501", ""):
            status, _ = _get_json(web, f"/api/day?date={date}")
            assert status == 400, date

    def test_date_absente_400(self, web):
        status, _ = _get_json(web, "/api/day")
        assert status == 400


class TestModelCounts:
    def test_top_8_modeles(self, web):
        """Top 8 par nombre de messages ; modeles vides/null exclus."""
        store = serve_web.Handler.state.store()
        rows = []
        for i in range(9):  # 9 modeles : le dernier est hors du top 8
            for _ in range(10 - i):
                rows.append({
                    "platform": "grok",
                    "conversation_id": f"m{i}",
                    "model": f"modele-{i}",
                    "timestamp": "2026-01-01T00:00:00Z",
                })
        rows.append({"platform": "grok", "conversation_id": "m0",
                     "model": "", "timestamp": "2026-01-01T00:00:00Z"})
        rows.append({"platform": "grok", "conversation_id": "m0",
                     "model": None, "timestamp": "2026-01-01T00:00:00Z"})
        _seed_messages(store, rows)
        status, body = _get_json(web, "/api/stats")
        assert status == 200
        counts = body["model_counts"]
        assert len(counts) == 8
        assert counts[0] == {"model": "modele-0", "count": 10}
        assert [c["model"] for c in counts] == [f"modele-{i}" for i in range(8)]
        assert "models" in body  # champ historique conserve


class TestTimeline:
    def test_month_window_consecutif(self):
        """Fenetre glissante : mois consecutifs, mois courant en dernier."""
        assert serve_web.month_window(datetime(2026, 1, 15), 3) == [
            "2025-11", "2025-12", "2026-01",
        ]
        assert serve_web.month_window(datetime(2026, 10, 1), 1) == ["2026-10"]
        assert len(serve_web.month_window(datetime(2026, 10, 1), 24)) == 24

    def test_series_par_plateforme_et_modele(self, web):
        """Conversations datees au mois de leur 1er message (1 fois par conv.)."""
        store = serve_web.Handler.state.store()
        now = datetime.now(timezone.utc)
        this_month = now.strftime("%Y-%m")
        prev_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        _seed_messages(store, [
            # conversation « a » : 2 messages le meme mois -> comptee 1 fois
            {"platform": "grok", "conversation_id": "a", "model": "grok-3",
             "timestamp": f"{prev_month}-02T10:00:00Z"},
            {"platform": "grok", "conversation_id": "a", "model": "grok-3",
             "timestamp": f"{prev_month}-09T10:00:00Z"},
            {"platform": "grok", "conversation_id": "b", "model": "grok-4",
             "timestamp": f"{prev_month}-20T10:00:00Z"},
            {"platform": "grok", "conversation_id": "c", "model": "grok-3",
             "timestamp": f"{this_month}-01T10:00:00Z"},
            {"platform": "claude", "conversation_id": "d", "model": "",
             "timestamp": f"{this_month}-02T10:00:00Z"},
        ])
        status, body = _get_json(web, "/api/timeline")
        assert status == 200
        months = body["months"]
        assert len(months) == serve_web.TIMELINE_MONTHS
        assert months == sorted(months)
        assert months[-1] == this_month
        # plateformes : triees par total decroissant, series alignees sur months
        assert body["platforms"][0]["platform"] == "grok"
        assert body["platforms"][0]["series"] == [0] * (len(months) - 2) + [2, 1]
        assert body["platforms"][0]["total"] == 3
        assert body["platforms"][0]["trend"] is None  # mois precedent a zero
        assert body["platforms"][1]["platform"] == "claude"
        assert body["platforms"][1]["series"] == [0] * (len(months) - 1) + [1]
        assert body["platforms"][1]["total"] == 1
        # total toutes plateformes (somme des series)
        assert body["total"]["total"] == 4
        assert body["total"]["series"][-2:] == [2, 2]
        # modeles : modele vide exclu, serie datee au 1er message du modele
        assert [m["model"] for m in body["models"]] == ["grok-3", "grok-4"]
        assert body["models"][0]["series"][-2:] == [1, 1]
        assert body["models"][0]["total"] == 2
        assert body["models"][1]["series"][-2:] == [1, 0]

    def test_plafond_modeles(self, web):
        """Les series de modeles sont bornees a TIMELINE_MODELS (top par total)."""
        store = serve_web.Handler.state.store()
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        rows = []
        for i in range(serve_web.TIMELINE_MODELS + 3):
            for c in range(serve_web.TIMELINE_MODELS + 3 - i):
                rows.append({
                    "platform": "grok", "conversation_id": f"m{i}-{c}",
                    "model": f"modele-{i}", "timestamp": f"{month}-01T00:00:00Z",
                })
        _seed_messages(store, rows)
        status, body = _get_json(web, "/api/timeline")
        assert status == 200
        assert len(body["models"]) == serve_web.TIMELINE_MODELS
        assert [m["model"] for m in body["models"]] == [
            f"modele-{i}" for i in range(serve_web.TIMELINE_MODELS)
        ]

    def test_tendance_dernier_mois_complet(self, web):
        """La tendance compare le dernier mois complet au precedent."""
        store = serve_web.Handler.state.store()
        now = datetime.now(timezone.utc)
        this_month = now.strftime("%Y-%m")
        prev = now.replace(day=1) - timedelta(days=1)
        prev_month = prev.strftime("%Y-%m")
        before = (prev.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        rows = [{"platform": "grok", "conversation_id": f"b{i}",
                 "timestamp": f"{before}-05T10:00:00Z"} for i in range(4)]
        rows += [{"platform": "grok", "conversation_id": f"p{i}",
                  "timestamp": f"{prev_month}-05T10:00:00Z"} for i in range(6)]
        rows.append({"platform": "grok", "conversation_id": "t0",
                     "timestamp": f"{this_month}-05T10:00:00Z"})
        _seed_messages(store, rows)
        status, body = _get_json(web, "/api/timeline")
        assert status == 200
        trend = body["platforms"][0]["trend"]
        assert trend == {"from": before, "to": prev_month, "pct": 50.0}  # 4 -> 6
        assert body["total"]["total"] == 11
        assert body["total"]["trend"] == trend
        # le mois courant (incomplet) ne sert pas a la tendance
        assert body["total"]["series"][-1] == 1


def test_month_trend_incalculable():
    """Tendance : None si fenetre trop courte ou mois precedent nul."""
    assert serve_web.month_trend([0, 0, 0], ["a", "b", "c"]) is None
    assert serve_web.month_trend([1, 2], ["a", "b"]) is None
    assert serve_web.month_trend([4, 6, 1], ["a", "b", "c"]) == {
        "from": "a", "to": "b", "pct": 50.0,
    }


class TestSimilar:
    def _seed(self):
        """Messages + vecteurs : m1 (reference), m2 proche, m3 loin."""
        store = serve_web.Handler.state.store()
        _seed_messages(store, [
            {"platform": "grok", "conversation_id": "s1", "message_id": "m1",
             "timestamp": "2026-01-01T10:00:00Z", "texte": "un chat noir"},
            {"platform": "grok", "conversation_id": "s1", "message_id": "m2",
             "timestamp": "2026-01-01T10:01:00Z", "texte": "un chien blanc"},
            {"platform": "claude", "conversation_id": "s2", "message_id": "m3",
             "timestamp": "2026-01-02T10:00:00Z", "texte": "un chat gris"},
        ])
        dim = store.dim
        near = np.zeros(dim, dtype="float32")
        near[0], near[1] = 0.98, 0.2
        base = np.zeros(dim, dtype="float32")
        base[0] = 1.0
        far = np.zeros(dim, dtype="float32")
        far[5] = 1.0
        rows = store.db.execute("SELECT rowid FROM messages ORDER BY rowid").fetchall()
        for row, vector in zip(rows, (base, near, far)):
            store.db.execute(
                "INSERT INTO vec_messages(rowid, embedding) VALUES (?, ?)",
                (row["rowid"], sqlite_vec.serialize_float32(vector)),
            )
        store.db.commit()

    def test_voisins_excluent_le_message(self, web):
        self._seed()
        status, body = _get_json(web, "/api/similar?platform=grok&id=s1&mid=m1&k=3")
        assert status == 200
        assert body["similar_to"] == {
            "platform": "grok", "conversation_id": "s1", "message_id": "m1",
        }
        found = [g["messages"][0]["message_id"] for g in body["groups"]]
        assert "m1" not in found          # message d'origine exclu
        assert found == ["m2", "m3"]      # le plus proche d'abord

    def test_position_quand_message_id_absent(self, web):
        self._seed()
        status, body = _get_json(web, "/api/similar?platform=grok&id=s1&i=1&k=1")
        assert status == 200
        assert body["similar_to"]["message_id"] is None
        assert body["count"] == 1
        assert body["groups"][0]["messages"][0]["message_id"] == "m1"

    def test_message_absent_400(self, web):
        self._seed()
        status, _ = _get_json(web, "/api/similar?platform=grok&id=inconnu&i=0")
        assert status == 400

    def test_k_invalide_400(self, web):
        for k in ("0", "abc", str(serve_web.SIMILAR_K_MAX + 1)):
            status, _ = _get_json(web, f"/api/similar?platform=grok&id=s1&k={k}")
            assert status == 400, k


class TestGalerieMedia:
    def _seed(self):
        store = serve_web.Handler.state.media_store()
        rows = [
            ("grok/images/a.png", "grok", "image", "c1", "m1", "un chat",
             "2026-01-01T00:00:00Z"),
            ("grok/images/b.jpg", "grok", "image", "c1", "m2", "un chien",
             "2026-01-02T00:00:00Z"),
            ("claude/files/son.mp3", "claude", "audio", "c2", "m3", "un son",
             "2026-01-03T00:00:00Z"),
        ]
        for path, platform, kind, conversation, message, texte, indexed in rows:
            store.db.execute(
                """
                INSERT INTO media
                    (media_id, platform, conversation_id, message_id, kind, path,
                     texte, fingerprint, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (path, platform, conversation, message, kind, path, texte, indexed),
            )
        store.db.commit()

    def test_pagination_et_filtres(self, web):
        self._seed()
        status, body = _get_json(web, "/api/media?limit=2")
        assert status == 200
        assert body["total"] == 3
        assert len(body["items"]) == 2
        # les plus recents d'abord (indexed_at decroissant)
        assert [i["filename"] for i in body["items"]] == ["son.mp3", "b.jpg"]
        assert body["items"][0]["url"] == "/media/claude/son.mp3"
        status, body = _get_json(web, "/api/media?kind=audio")
        assert body["total"] == 1
        assert body["items"][0]["kind"] == "audio"
        status, body = _get_json(web, "/api/media?platform=grok&kind=image")
        assert body["total"] == 2
        status, body = _get_json(web, "/api/media?offset=2&limit=10")
        assert len(body["items"]) == 1

    def test_parametres_invalides_400(self, web):
        for query in ("kind=bogus", "limit=0", "limit=9999", "offset=-1", "limit=abc"):
            status, _ = _get_json(web, f"/api/media?{query}")
            assert status == 400, query
        status, _ = _get_json(web, "/api/media?platform=inconnu")
        assert status == 400


class TestStatus:
    def test_index_scrape_et_sauvegardes(self, web):
        store = serve_web.Handler.state.store()
        _seed_messages(store, [
            {"platform": "grok", "conversation_id": "g1",
             "timestamp": "2026-01-01T10:00:00Z"},
        ])
        _seed_conversations(store, [
            {"platform": "grok", "conversation_id": "g1",
             "indexed_at": "2026-01-02T00:00:00Z"},
        ])
        status, body = _get_json(web, "/api/status")
        assert status == 200
        assert body["index"]["messages"] == 1
        assert body["index"]["conversations"] == 1
        assert body["index"]["indexed_at"] == "2026-01-02T00:00:00Z"
        assert body["index"]["db_bytes"] > 0
        assert body["backups"] == []
        grok = next(s for s in body["scrape"] if s["platform"] == "grok")
        assert grok["conversations"] == 1
        assert grok["last_message_at"] == "2026-05-01T09:01:00Z"  # fixture
        assert grok["scraped_at"]
        assert grok["health"] is None  # pas de api_health.json

    def test_sante_api_lue(self, web):
        exports = serve_web.Handler.state.exports_dir
        (exports / "grok" / "api_health.json").write_text(
            json.dumps({
                "platform": "grok", "n": 20, "matches": 19, "breaks": 1,
                "drifts": 1, "mean_score": 98.2, "threshold": 95.0,
                "updated_at": "2026-01-01T00:00:00Z",
            }),
            encoding="utf-8",
        )
        status, body = _get_json(web, "/api/status")
        assert status == 200
        health = next(s for s in body["scrape"] if s["platform"] == "grok")["health"]
        assert health["rate"] == 95.0
        assert health["breaks"] == 1
        assert health["drifts"] == 1
        assert health["updated_at"] == "2026-01-01T00:00:00Z"


def _fake_recheck_runner(seen):
    """Runner factice : aucun scrape pendant les tests."""
    def runner(config, platform, per_platform, min_messages, seed,
               orchestrator=None, log=print):
        seen.append(platform)
        return {
            "platform": platform, "verdict": "ok", "retention": 1.0,
            "checked": 1, "compared": 1, "ignored": 0, "ok": 1,
            "degraded": 0, "failed": 0, "checked_at": "2026-01-01T00:00:00Z",
            "conversations": [],
        }
    return runner


def _install_fake_job(runner, suffix: str):
    serve_web.Handler.state.recheck_job = serve_web.RecheckJob(
        runner=runner,
        lock_path=f"/tmp/aicv-test-{os.getpid()}-{suffix}.lock",
        orchestrator_factory=lambda config: object(),
    )
    return serve_web.Handler.state.recheck_job


class TestRecheckJobWeb:
    def test_get_etat_initial(self, web):
        status, body = _get_json(web, "/api/recheck")
        assert status == 200
        assert body["running"] is False
        assert body["reports"] == []
        assert body["log"] == []

    def test_post_lance_le_test(self, web):
        seen = []
        job = _install_fake_job(_fake_recheck_runner(seen), "start")
        status, body = _post_json(
            web, "/api/recheck", {"platforms": ["grok"], "per_platform": 2}
        )
        assert status == 200
        assert body["started"] is True
        assert body["platforms"] == ["grok"]
        job._thread.join(timeout=10)
        status, body = _get_json(web, "/api/recheck")
        assert body["running"] is False
        assert [item["platform"] for item in body["done"]] == ["grok"]
        assert seen == ["grok"]

    def test_post_plateforme_inconnue_400(self, web):
        status, body = _post_json(web, "/api/recheck", {"platforms": ["bogus"]})
        assert status == 400
        assert "plateforme" in body["error"]

    def test_post_platforms_invalide_400(self, web):
        status, _ = _post_json(web, "/api/recheck", {"platforms": "grok"})
        assert status == 400
        status, _ = _post_json(web, "/api/recheck", {"platforms": []})
        assert status == 400

    def test_post_parametres_invalides_400(self, web):
        for payload in ({"per_platform": 0}, {"per_platform": 99},
                        {"min_messages": 0}, {"min_messages": "beaucoup"}):
            status, _ = _post_json(web, "/api/recheck", payload)
            assert status == 400, payload

    def test_post_deja_en_cours_409(self, web):
        release = threading.Event()

        def blocking(*args, **kwargs):
            release.wait(timeout=5)
            return None

        job = _install_fake_job(blocking, "busy")
        status, _ = _post_json(web, "/api/recheck", {"platforms": ["grok"]})
        assert status == 200
        status, body = _post_json(web, "/api/recheck", {"platforms": ["grok"]})
        assert status == 409
        assert "deja en cours" in body["error"]
        release.set()
        job._thread.join(timeout=5)

    def test_corps_json_invalide_400(self, web):
        request = urllib.request.Request(
            web + "/api/recheck", data=b"{oups",
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        assert status == 400

    def test_post_endpoint_inconnu_404(self, web):
        status, _ = _post_json(web, "/api/inexistant", {})
        assert status == 404


def _fake_scrape_runner(seen):
    """Runner de scrape factice : aucun scrape pendant les tests."""
    def runner(config, platform, mode="daily", limit=None,
               orchestrator=None, log=print):
        seen.append((platform, mode, limit))
        return {
            "platform": platform, "mode": mode, "discovered": 5, "targets": 2,
            "exported": 1, "patched": 0, "failed": 0, "skipped": None, "ok": True,
        }
    return runner


def _install_fake_scrape_job(runner, suffix: str):
    serve_web.Handler.state.scrape_job = serve_web.ScrapeJob(
        runner=runner,
        lock_path=f"/tmp/aicv-test-{os.getpid()}-{suffix}.lock",
        orchestrator_factory=lambda config: object(),
    )
    return serve_web.Handler.state.scrape_job


class TestScrapeJobWeb:
    def test_get_etat_initial(self, web):
        status, body = _get_json(web, "/api/scrape")
        assert status == 200
        assert body["running"] is False
        assert body["mode"] == "daily"
        assert body["done"] == []
        assert body["log"] == []

    def test_post_lance_le_daily(self, web):
        seen = []
        job = _install_fake_scrape_job(_fake_scrape_runner(seen), "daily")
        status, body = _post_json(
            web, "/api/scrape", {"platforms": ["grok", "claude"], "mode": "daily"}
        )
        assert status == 200
        assert body["started"] is True
        assert body["platforms"] == ["grok", "claude"]
        job._thread.join(timeout=10)
        status, body = _get_json(web, "/api/scrape")
        assert body["running"] is False
        assert [item["platform"] for item in body["done"]] == ["grok", "claude"]
        assert body["done"][0]["exported"] == 1
        assert seen == [("grok", "daily", None), ("claude", "daily", None)]

    def test_post_plateforme_inconnue_400(self, web):
        status, body = _post_json(web, "/api/scrape", {"platforms": ["bogus"]})
        assert status == 400
        assert "plateforme" in body["error"]

    def test_post_parametres_invalides_400(self, web):
        for payload in (
            {"platforms": "grok"},
            {"platforms": []},
            {"mode": "hebdo"},
            {"limit": 0},
            {"limit": "beaucoup"},
        ):
            status, _ = _post_json(web, "/api/scrape", payload)
            assert status == 400, payload

    def test_post_deja_en_cours_409(self, web):
        release = threading.Event()

        def blocking(*args, **kwargs):
            release.wait(timeout=5)
            return None

        job = _install_fake_scrape_job(blocking, "busy")
        status, _ = _post_json(web, "/api/scrape", {"platforms": ["grok"]})
        assert status == 200
        status, body = _post_json(web, "/api/scrape", {"platforms": ["grok"]})
        assert status == 409
        assert "deja en cours" in body["error"]
        release.set()
        job._thread.join(timeout=5)


# -- authentification (page de connexion + session signee) --------------------

PASSWORD = "mot-de-passe-de-test-42"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Ne suit pas les redirections (on veut observer le 302)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT = urllib.request.build_opener(_NoRedirect)


def _request(base, path, *, method="GET", payload=None, cookie=None, origin=None,
             headers=None):
    """Requete brute -> (statut, en-tetes, corps)."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    sent = dict(headers or {})
    if payload is not None:
        sent["Content-Type"] = "application/json"
    if cookie is not None:
        sent["Cookie"] = f"aicv_session={cookie}"
    if origin is not None:
        sent["Origin"] = origin
    request = urllib.request.Request(
        base + path, data=data, headers=sent, method=method
    )
    try:
        with _NO_REDIRECT.open(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _login(base, password=PASSWORD):
    """Connexion -> (statut, jeton de session ou None)."""
    status, headers, _ = _request(
        base, "/api/login", method="POST", payload={"password": password}
    )
    cookie = headers.get("Set-Cookie") or ""
    token = (
        cookie.split(";")[0].split("=", 1)[1]
        if cookie.startswith(f"{webauth.SESSION_COOKIE}=")
        else None
    )
    return status, token


@pytest.fixture()
def web_auth(tmp_path):
    """Serveur avec authentification active (mot de passe connu)."""
    secret = b"s" * 32
    serve_web.Handler.state = serve_web.AppState(
        db_path=tmp_path / "rag.db",
        exports_dir=tmp_path / "exports",
        model_name="dummy",
    )
    serve_web.Handler.auth = serve_web.AuthConfig(
        password_hash=webauth.hash_password(PASSWORD),
        secret=secret,
        ttl_seconds=3600,
        cookie_secure="always",
        throttle=webauth.LoginThrottle(max_failures=3, window=60, lockout=60),
    )
    server = serve_web.ThreadingHTTPServer(("127.0.0.1", 0), serve_web.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "base": f"http://127.0.0.1:{server.server_address[1]}",
            "secret": secret,
            "key": serve_web.Handler.auth.key,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        serve_web.Handler.auth = None


class TestAuthentification:
    def test_accueil_redirige_vers_la_connexion(self, web_auth):
        status, headers, _ = _request(web_auth["base"], "/")
        assert status == 302
        assert headers["Location"] == "/login?next=/"

    def test_page_login_servie_sans_session(self, web_auth):
        status, _, body = _request(web_auth["base"], "/login")
        assert status == 200
        assert b'id="password"' in body
        assert b"login.js" in body

    def test_scripts_de_connexion_servis_sans_session(self, web_auth):
        for path in ("/login.js", "/theme-boot.js"):
            status, _, body = _request(web_auth["base"], path)
            assert status == 200, path
            assert body

    def test_api_refusee_sans_session(self, web_auth):
        status, _, body = _request(web_auth["base"], "/api/stats")
        assert status == 401
        assert json.loads(body)["login"] == "/login"

    def test_media_refuse_sans_session(self, web_auth):
        status, _, _ = _request(web_auth["base"], "/media/grok/img1.png")
        assert status == 401

    def test_mauvais_mot_de_passe(self, web_auth):
        status, token = _login(web_auth["base"], "mauvais")
        assert status == 401
        assert token is None

    def test_connexion_pose_un_cookie_strict(self, web_auth):
        status, headers, _ = _request(
            web_auth["base"], "/api/login", method="POST",
            payload={"password": PASSWORD},
        )
        assert status == 200
        cookie = headers["Set-Cookie"]
        assert cookie.startswith(f"{webauth.SESSION_COOKIE}=")
        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie
        assert "Secure" in cookie
        assert "Max-Age=3600" in cookie

    def test_acces_avec_session(self, web_auth):
        status, token = _login(web_auth["base"])
        assert status == 200 and token
        status, _, body = _request(web_auth["base"], "/api/stats", cookie=token)
        assert status == 200
        assert "total_messages" in json.loads(body)
        status, _, _ = _request(web_auth["base"], "/", cookie=token)
        assert status == 200

    def test_login_redirige_si_deja_connecte(self, web_auth):
        _, token = _login(web_auth["base"])
        status, headers, _ = _request(web_auth["base"], "/login", cookie=token)
        assert status == 302
        assert headers["Location"] == "/"

    def test_deconnexion_efface_le_cookie(self, web_auth):
        _, token = _login(web_auth["base"])
        status, headers, _ = _request(
            web_auth["base"], "/api/logout", method="POST", payload={}, cookie=token
        )
        assert status == 200
        assert "Max-Age=0" in headers["Set-Cookie"]

    def test_post_refuse_sans_session(self, web_auth):
        status, _, body = _request(
            web_auth["base"], "/api/scrape", method="POST",
            payload={"platforms": ["grok"]},
        )
        assert status == 401
        assert json.loads(body)["error"] == "authentification requise"

    def test_csrf_origine_etrangere(self, web_auth):
        _, token = _login(web_auth["base"])
        status, _, body = _request(
            web_auth["base"], "/api/scrape", method="POST",
            payload={"platforms": ["grok"]}, cookie=token,
            origin="https://attaque.example",
        )
        assert status == 403
        assert "origine" in json.loads(body)["error"]

    def test_origine_identique_acceptee(self, web_auth):
        """Meme hote : la requete passe (404 = endpoint inconnu, pas 403)."""
        _, token = _login(web_auth["base"])
        status, _, _ = _request(
            web_auth["base"], "/api/inconnu", method="POST", payload={},
            cookie=token, origin=web_auth["base"],
        )
        assert status == 404

    def test_limitation_apres_trois_echecs(self, web_auth):
        for _ in range(3):
            status, _ = _login(web_auth["base"], "mauvais")
            assert status == 401
        status, token = _login(web_auth["base"])
        assert status == 429
        assert token is None

    def test_jeton_expire_refuse(self, web_auth):
        expired = webauth.make_token(
            web_auth["key"], 60, now=time.time() - 120
        )
        status, _, _ = _request(web_auth["base"], "/api/stats", cookie=expired)
        assert status == 401

    def test_jeton_d_un_autre_secret_refuse(self, web_auth):
        forged = webauth.make_token(b"z" * 32, 3600)
        status, _, _ = _request(web_auth["base"], "/api/stats", cookie=forged)
        assert status == 401

    def test_entetes_de_durcissement(self, web_auth):
        _, headers, _ = _request(web_auth["base"], "/login")
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "no-referrer"

    def test_head_sans_corps(self, web_auth):
        status, headers, body = _request(web_auth["base"], "/login", method="HEAD")
        assert status == 200
        assert body == b""
        assert int(headers["Content-Length"]) > 0

    def test_csp_stricte(self, web_auth):
        _, headers, _ = _request(web_auth["base"], "/login")
        csp = headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp
        # bibliotheques vendorisees : aucun script tiers autorise
        assert "script-src 'self';" in csp
        assert "cdn.jsdelivr" not in csp
        assert "frame-ancestors 'none'" in csp
        assert headers["Permissions-Policy"]
        # HTTP local : pas de HSTS
        assert "Strict-Transport-Security" not in headers

    def test_hsts_derriere_le_proxy_https(self, web_auth):
        _, headers, _ = _request(
            web_auth["base"], "/login", headers={"X-Forwarded-Proto": "https"}
        )
        assert "Strict-Transport-Security" in headers

    def test_pages_sans_script_inline(self):
        """CSP sans 'unsafe-inline' : aucun script inline dans les pages."""
        for name in ("index.html", "login.html"):
            text = (ROOT / "web" / name).read_text(encoding="utf-8")
            assert "<script>" not in text, name
            assert "theme-boot.js" in text, name

    def test_revocation_apres_deconnexion(self, web_auth):
        _, token = _login(web_auth["base"])
        status, _, _ = _request(
            web_auth["base"], "/api/logout", method="POST", payload={}, cookie=token
        )
        assert status == 200
        # le jeton est revoque, pas seulement efface du navigateur
        status, _, _ = _request(web_auth["base"], "/api/stats", cookie=token)
        assert status == 401

    def test_meme_client_bloque_meme_avec_xff(self, web_auth):
        """La cle est le saut du proxy : 3 echecs du meme client => 429."""
        headers = {"X-Forwarded-For": "203.0.113.7"}
        for _ in range(3):
            status, _, _ = _request(
                web_auth["base"], "/api/login", method="POST",
                payload={"password": "mauvais"}, headers=headers,
            )
            assert status == 401
        status, _, _ = _request(
            web_auth["base"], "/api/login", method="POST",
            payload={"password": "mauvais"}, headers=headers,
        )
        assert status == 429

    def test_media_sandbox(self, web_auth):
        """Un SVG ouvert directement ne peut rien executer (CSP sandbox)."""
        _, token = _login(web_auth["base"])
        exports = serve_web.Handler.state.exports_dir
        images = exports / "grok" / "images"
        images.mkdir(parents=True, exist_ok=True)
        (images / "x.svg").write_text(
            "<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8"
        )
        status, headers, _ = _request(
            web_auth["base"], "/media/grok/x.svg", cookie=token
        )
        assert status == 200
        assert headers["Content-Type"] == "image/svg+xml"
        assert "sandbox" in headers["Content-Security-Policy"]


def test_safe_next():
    assert serve_web.safe_next("/") == "/"
    assert serve_web.safe_next("/?q=python") == "/?q=python"
    assert serve_web.safe_next("//evil.example") == "/"
    assert serve_web.safe_next("/\t/evil.example") == "/"
    assert serve_web.safe_next("https://evil.example") == "/"
    assert serve_web.safe_next("/\\evil") == "/"
    assert serve_web.safe_next("") == "/"


def test_conversation_file_hors_exports_refuse(web):
    """Une entree de base pointant hors de exports/ n'est jamais lue."""
    state = serve_web.Handler.state
    store = state.store()
    store.db.execute(
        """
        INSERT INTO messages
            (message_id, conversation_id, platform, role, model, timestamp,
             texte, has_code, conversation_file)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("m-poison", "g1", "grok", "user", "", "", "x", 0,
         "/etc/passwd"),
    )
    store.db.commit()
    path = state.conversation_file("grok", "g1")
    assert path is not None
    assert state.exports_dir.resolve() in path.parents
    assert path.name != "passwd"


# -- robustesse de la couche HTTP (anti-desynchronisation, methodes) ----------

def _raw(base, request: bytes, timeout: float = 1.5) -> bytes:
    """Envoie une requete brute et rend tout ce qui est recu avant la fermeture."""
    host, port = base.replace("http://", "").split(":")
    chunks = []
    with socket.create_connection((host, int(port)), timeout=timeout) as sock:
        sock.sendall(request)
        sock.settimeout(timeout)
        while True:
            try:
                data = sock.recv(65536)
            except (socket.timeout, ConnectionResetError):
                break
            if not data:
                break
            chunks.append(data)
    return b"".join(chunks)


def _post_with_body(base, path, body: bytes, extra: bytes = b"") -> bytes:
    return _raw(
        base,
        b"POST " + path.encode() + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        + b"Content-Type: application/json\r\n"
        + b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        + extra + b"\r\n" + body,
    )


def test_corps_trop_gros_refuse_et_connexion_fermee(web_auth):
    """Un corps > 64 Kio => 413 + fermeture, jamais de relecture."""
    raw = _post_with_body(web_auth["base"], "/api/login", b"A" * (65 * 1024))
    assert b"413" in raw.split(b"\r\n", 1)[0]
    assert raw.count(b"HTTP/1.1") == 1


def test_corps_non_reinterprete_apres_refus(web_auth):
    """Le corps d'un POST refuse (403) est consomme, pas rejoue en requete."""
    smuggled = b"GET /api/stats HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    raw = _post_with_body(
        web_auth["base"], "/api/login", smuggled,
        extra=b"Origin: https://evil.example\r\n",
    )
    assert b"403" in raw.split(b"\r\n", 1)[0]
    assert raw.count(b"HTTP/1.1") == 1


def test_get_avec_corps_ne_desynchronise_pas(web_auth):
    """Un corps sur un GET est consomme : une seule reponse, pas de 400 fantome."""
    raw = _raw(
        web_auth["base"],
        b"GET /login HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Content-Length: 10\r\n\r\n0123456789",
    )
    assert b"200" in raw.split(b"\r\n", 1)[0]
    assert raw.count(b"HTTP/1.1") == 1


def test_head_sur_api_refuse(web_auth):
    status, _, _ = _request(web_auth["base"], "/api/stats", method="HEAD")
    assert status == 405


def test_methodes_non_servies_405_avec_entetes(web_auth):
    for method in ("OPTIONS", "PUT", "DELETE", "PATCH", "TRACE"):
        status, headers, _ = _request(
            web_auth["base"], "/login", method=method, payload={}
        )
        assert status == 405, method
        assert headers["X-Frame-Options"] == "DENY", method


def test_requete_malformee_repond(web_auth):
    """Version HTTP invalide : une reponse, pas d'exception dans le journal."""
    raw = _raw(web_auth["base"], b"GET /login HTTP/9.9\r\nHost: x\r\n\r\n")
    assert raw.startswith(b"HTTP/")


def test_corps_relu_a_chaque_requete_keep_alive(web_auth):
    """Deux POST sur la MEME connexion : chaque corps doit etre lu.

    Regression : le corps etait memorise sur l'instance de handler, reutilisee
    par toutes les requetes d'une connexion persistante (login casse + corps
    laisses dans le socket).
    """
    good = json.dumps({"password": PASSWORD}).encode()
    bad = json.dumps({"password": "mauvais"}).encode()
    req = (
        b"POST /api/login HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(good)).encode() + b"\r\n\r\n" + good
        + b"POST /api/login HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(bad)).encode() + b"\r\n\r\n" + bad
    )
    raw = _raw(web_auth["base"], req)
    assert raw.count(b"HTTP/1.1") == 2, raw[:200]
    assert b"200 OK" in raw          # bon mot de passe
    assert b"401 Unauthorized" in raw  # mauvais mot de passe (corps bien relu)


def test_corps_consomme_avant_refus_puis_requete_suivante(web_auth):
    """Un POST refuse (403) laisse la connexion utilisable pour la suite."""
    smuggled = b"GET /login HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    req = (
        b"POST /api/login HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Origin: https://evil.example\r\n"
        b"Content-Length: " + str(len(smuggled)).encode() + b"\r\n\r\n" + smuggled
    )
    raw = _raw(web_auth["base"], req)
    assert b"403" in raw.split(b"\r\n", 1)[0]
    # le corps n'a pas ete rejoue : une seule reponse
    assert raw.count(b"HTTP/1.1") == 1


def test_quota_par_client_ne_fuit_pas(web_auth):
    """Le quota de connexions est relache : 30 requetes sequentielles passent."""
    for _ in range(30):
        status, _, _ = _request(web_auth["base"], "/api/stats")
        assert status == 401  # non authentifie, mais servi (pas 503)


# -- robustesse des donnees (round 4) -----------------------------------------

def test_json_profond_refuse_sans_500(web_auth):
    """JSON imbrique a l'exces : 400, jamais 500 (RecursionError)."""
    body = b"[" * 20000 + b"]" * 20000
    raw = _post_with_body(web_auth["base"], "/api/login", body)
    status = raw.split(b"\r\n", 1)[0]
    assert b"400" in status or b"413" in status, status
    assert b"500" not in status


def test_head_erreur_sans_corps(web_auth):
    """Une reponse HEAD ne porte jamais de corps, meme en erreur."""
    raw = _raw(
        web_auth["base"],
        b"HEAD /login HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: -5\r\n\r\n",
    )
    head, _, body = raw.partition(b"\r\n\r\n")
    assert b"400" in head.split(b"\r\n", 1)[0]
    assert body == b""


def test_chemin_absurde_sans_500(web_auth):
    """NUL et composant trop long : 404, pas 500 (test AUTHENTIFIE).

    Sans session, la garde d'auth repond avant : le test ne couvrait rien.
    """
    status, token = _login(web_auth["base"])
    assert status == 200 and token
    exports = serve_web.Handler.state.exports_dir
    (exports / "grok" / "images").mkdir(parents=True, exist_ok=True)
    for path in ("/%00", "/" + "a" * 300, "/media/grok/" + "b" * 300):
        raw = _raw(
            web_auth["base"],
            b"GET " + path.encode() + b" HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Cookie: aicv_session=" + token.encode() + b"\r\n\r\n",
        )
        status = raw.split(b"\r\n", 1)[0]
        assert b"500" not in status, (path, status)


def test_fichier_archive_non_utf8_ne_casse_pas(web):
    """Un JSON d'archive non UTF-8 ne doit pas produire de 500."""
    exports = serve_web.Handler.state.exports_dir
    (exports / "grok" / "corrompu.json").write_bytes(b"\xff\xfe{")
    for path in ("/api/conversation?platform=grok&id=inconnu", "/api/status",
                 "/api/conversations?platform=grok"):
        status, _ = _get(web, path)
        assert status == 200, path


def test_lien_symbolique_hors_exports_ignore(web, tmp_path):
    """Un symlink dans exports/ n'est jamais suivi (repli conversation_file)."""
    exports = serve_web.Handler.state.exports_dir
    secret = tmp_path / "hors-exports.json"
    secret.write_text(json.dumps({"conversation_id": "conv-piege",
                                  "title": "SECRET"}), encoding="utf-8")
    link = exports / "grok" / "piege.json"
    link.symlink_to(secret)
    status, body = _get_json(web, "/api/conversation?platform=grok&id=conv-piege")
    assert status == 200
    assert "SECRET" not in json.dumps(body)


def test_platforms_dedoublonnees(web):
    """Une liste de plateformes repetee n'amplifie pas le job."""
    seen = []
    _install_fake_scrape_job(_fake_scrape_runner(seen), "daily")
    status, body = _post_json(
        web, "/api/scrape", {"platforms": ["grok"] * 500, "mode": "daily"}
    )
    assert status == 200
    assert body["platforms"] == ["grok"]


def test_entier_json_geant_sans_500(web_auth):
    """Un entier de 5000 chiffres : 400, pas 500 (limite CPython)."""
    body = b'{"password":' + b"9" * 5000 + b"}"
    raw = _post_with_body(web_auth["base"], "/api/login", body)
    status = raw.split(b"\r\n", 1)[0]
    assert b"400" in status, status
    assert b"500" not in status


def test_origin_ipv6_malformee_sans_500(web_auth):
    """`Origin: http://[` : 403, pas 500."""
    status, _, body = _request(
        web_auth["base"], "/api/login", method="POST",
        payload={"password": "x"}, origin="http://[",
    )
    assert status == 403
    assert b"erreur interne" not in body


def test_head_version_invalide_sans_corps(web_auth):
    """HEAD avec version invalide : reponse sans corps."""
    raw = _raw(
        web_auth["base"],
        b"HEAD /login HTTP/9.9\r\nHost: 127.0.0.1\r\n\r\n",
    )
    head, _, body = raw.partition(b"\r\n\r\n")
    assert b"505" in head.split(b"\r\n", 1)[0]
    assert body == b""


def test_content_length_duplique_refuse(web_auth):
    """Deux Content-Length : 400 + fermeture (anti-desynchronisation)."""
    raw = _raw(
        web_auth["base"],
        b"POST /api/login HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        b"Content-Length: 0\r\nContent-Length: 30\r\n\r\n",
    )
    status = raw.split(b"\r\n", 1)[0]
    assert b"400" in status, status


def test_rapports_pieges_sans_500(web):
    """conversation_list.json / recheck.json / api_health.json pieges : 200."""
    exports = serve_web.Handler.state.exports_dir
    (exports / "grok" / "conversation_list.json").write_text(
        json.dumps({"conversations": ["pas-un-dict", 42, {"conversation_id": "x"}]}),
        encoding="utf-8",
    )
    (exports / "grok" / "recheck.json").write_text(
        json.dumps({"verdict": {"a": 1}, "checked": "abc",
                    "conversations": [{"status": "degrade", "lost": 5}]}),
        encoding="utf-8",
    )
    (exports / "grok" / "api_health.json").write_text(
        json.dumps({"n": "abc", "matches": {"a": 1}}), encoding="utf-8"
    )
    for path in ("/api/status", "/api/conversations?platform=grok", "/api/recheck"):
        status, _ = _get(web, path)
        assert status == 200, path


def test_lien_dur_hors_archive_refuse(web, tmp_path):
    """Un lien dur vers un fichier hors archive n'est jamais lu."""
    exports = serve_web.Handler.state.exports_dir
    secret = tmp_path / "hors.json"
    secret.write_text(json.dumps({"conversation_id": "conv-hard",
                                  "title": "SECRET-HARD"}), encoding="utf-8")
    link = exports / "grok" / "dur.json"
    link.hardlink_to(secret)
    status, body = _get_json(web, "/api/conversation?platform=grok&id=conv-hard")
    assert "SECRET-HARD" not in json.dumps(body)


def test_dossier_plateforme_symbolique_ignore(web, tmp_path):
    """Un dossier de plateforme symlinke n'est pas servi."""
    exports = serve_web.Handler.state.exports_dir
    outside = tmp_path / "dehors"
    outside.mkdir()
    (outside / "conversation_list.json").write_text(
        json.dumps({"conversations": [{"conversation_id": "x", "title": "SECRET-LINK"}]}),
        encoding="utf-8",
    )
    (exports / "claude").symlink_to(outside)
    status, body = _get_json(web, "/api/conversations?platform=claude")
    assert "SECRET-LINK" not in json.dumps(body)


def test_random_url_canonique(web):
    """Une URL `javascript:` de la base est remplacee par le lien canonique."""
    state = serve_web.Handler.state
    store = state.store()
    store.db.execute(
        "UPDATE conversations SET url=? WHERE platform='grok'",
        ("javascript:alert(1)",),
    )
    store.db.commit()
    status, body = _get_json(web, "/api/random?n=3")
    assert status == 200
    for conv in body.get("conversations", []):
        assert not str(conv.get("url", "")).lower().startswith("javascript:")


def test_sous_dossier_media_symbolique_refuse(web_auth):
    """`images/` symlinké ne doit pas donner accès hors de l'archive."""
    exports = serve_web.Handler.state.exports_dir
    base = exports / "grok"
    base.mkdir(parents=True, exist_ok=True)
    outside = exports / "dehors"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET-MEDIA", encoding="utf-8")
    (base / "images").symlink_to(outside)
    status, token = _login(web_auth["base"])
    assert status == 200
    status, _, body = _request(
        web_auth["base"], "/media/grok/secret.txt", cookie=token
    )
    assert status == 404
    assert b"SECRET-MEDIA" not in body


def test_conversation_list_symbolique_ignore(web, tmp_path):
    """Un conversation_list.json symlinké n'est jamais lu."""
    exports = serve_web.Handler.state.exports_dir
    outside = tmp_path / "liste-externe.json"
    outside.write_text(
        json.dumps({"conversations": [{"conversation_id": "x",
                                       "title": "SECRET-LISTE"}]}),
        encoding="utf-8",
    )
    path = exports / "grok" / "conversation_list.json"
    path.unlink()
    path.symlink_to(outside)
    for url in ("/api/conversations?platform=grok", "/api/status"):
        status, body = _get_json(web, url)
        assert status == 200, url
        assert "SECRET-LISTE" not in json.dumps(body), url


def test_surrogate_isole_ne_casse_pas_la_reponse(web):
    """Un surrogate isole dans l'archive est echappe, pas une erreur 400."""
    exports = serve_web.Handler.state.exports_dir
    (exports / "grok" / "conversation_list.json").write_text(
        json.dumps({"conversations": [{"conversation_id": "s1",
                                       "title": "titre \ud800"}]}),
        encoding="utf-8",
    )
    for url in ("/api/conversations?platform=grok", "/api/status"):
        status, body = _get(web, url)
        assert status == 200, url
        assert b"\xed\xa0\x80" not in body  # jamais d'UTF-8 invalide


def test_message_champs_mal_types_normalises(web):
    """`code_blocks`/`variants` non-listes : normalises, la vue ne casse pas."""
    exports = serve_web.Handler.state.exports_dir
    (exports / "grok" / "conv1.json").write_text(
        json.dumps({
            "conversation_id": "g1", "platform": "grok", "title": "T",
            "url": "https://grok.com/chat/g1",
            "messages": [{"message_id": "m1", "role": "user", "texte": "bonjour",
                          "code_blocks": "pas une liste", "variants": {"a": 1}}],
        }),
        encoding="utf-8",
    )
    status, body = _get_json(web, "/api/conversation?platform=grok&id=g1")
    assert status == 200
    message = body["conversation"]["messages"][0]
    assert message["code_blocks"] == []
    assert message["variants"] == []
