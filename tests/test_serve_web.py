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
from datetime import datetime, timedelta, timezone
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
