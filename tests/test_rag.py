"""Tests du RAG (stockage sqlite-vec). Aucun modele d'embeddings charge."""

from __future__ import annotations

import json

import numpy as np

from src.rag.indexer import index_exports, iter_conversation_files
from src.rag.store import VectorStore


class FakeEmbedder:
    model_name = "fake-model"

    def __init__(self):
        self.calls = 0
        self.encoded = 0

    def encode(self, texts):
        self.calls += 1
        self.encoded += len(texts)
        return np.array([[1.0, 0, 0, 0] for _ in texts], dtype="float32")


def _write_export(root, conversation, exported_at="v1", extra_message=False):
    platform = "svc"
    d = root / platform
    d.mkdir(parents=True, exist_ok=True)
    (d / "conversation_list.json").write_text("{}", encoding="utf-8")
    messages = list(conversation)
    if extra_message:
        messages = messages + [
            {"message_id": "m3", "role": "user", "texte": "encore", "code_blocks": []}
        ]
    (d / "conv1.json").write_text(
        json.dumps(
            {
                "conversation_id": "c1",
                "platform": platform,
                "title": "t",
                "exported_at": exported_at,
                "messages": messages,
            }
        ),
        encoding="utf-8",
    )


def test_vecteurs_reutilises_depuis_cache(tmp_path):
    base = [
        {"message_id": "m1", "role": "user", "texte": "bonjour", "code_blocks": []},
        {"message_id": "m2", "role": "assistant", "texte": "salut", "code_blocks": []},
    ]
    root = tmp_path / "exports"
    _write_export(root, base)
    store = VectorStore(tmp_path / "rag.db", dim=4)
    embedder = FakeEmbedder()

    first = index_exports(store, embedder, root, force=True)
    assert first["vectors_computed"] == 2 and first["vectors_reused"] == 0
    assert embedder.encoded == 2

    # force + memes messages : tout est repris du cache, aucun recalcul
    second = index_exports(store, embedder, root, force=True)
    assert second["vectors_reused"] == 2 and second["vectors_computed"] == 0
    assert embedder.encoded == 2  # inchange (pas de nouvel encodage)

    # nouveau message : seuls les nouveaux textes sont calcules
    _write_export(root, base, exported_at="v2", extra_message=True)
    third = index_exports(store, embedder, root, force=True)
    assert third["vectors_reused"] == 2 and third["vectors_computed"] == 1
    assert embedder.encoded == 3
    assert store.stats()["messages"] == 3


def _msg(mid="m1", role="user", texte="bonjour"):
    return {
        "message_id": mid,
        "role": role,
        "texte": texte,
        "timestamp": "2026-01-01T00:00:00Z",
        "model": None,
        "code_blocks": [],
    }


def test_store_insert_search_cosine(tmp_path):
    store = VectorStore(tmp_path / "rag.db", dim=4)
    store.replace_conversation(
        "fake", "c1", "Titre", "2026-01-01T00:00:00Z",
        [_msg("m1")], np.array([[1.0, 0.0, 0.0, 0.0]], dtype="float32"),
    )
    store.replace_conversation(
        "fake", "c2", "Autre", "2026-01-01T00:00:00Z",
        [_msg("m2")], np.array([[0.0, 1.0, 0.0, 0.0]], dtype="float32"),
    )
    results = store.search(np.array([1.0, 0.0, 0.0, 0.0], dtype="float32"), k=2)
    assert results[0]["conversation_id"] == "c1"
    assert results[0]["score"] > 0.99
    assert results[1]["score"] < 0.5
    store.close()


def test_filtres_platform_et_role(tmp_path):
    store = VectorStore(tmp_path / "rag.db", dim=4)
    store.replace_conversation(
        "a", "c1", "t", "x",
        [_msg("m1", "user")], np.array([[1.0, 0, 0, 0]], dtype="float32"),
    )
    store.replace_conversation(
        "b", "c2", "t", "x",
        [_msg("m2", "assistant")], np.array([[1.0, 0, 0, 0]], dtype="float32"),
    )
    only_a = store.search(np.array([1.0, 0, 0, 0], dtype="float32"), k=5, platform="a")
    assert [r["conversation_id"] for r in only_a] == ["c1"]
    only_assistant = store.search(
        np.array([1.0, 0, 0, 0], dtype="float32"), k=5, role="assistant"
    )
    assert [r["conversation_id"] for r in only_assistant] == ["c2"]
    store.close()


def test_remplacement_et_increment(tmp_path):
    store = VectorStore(tmp_path / "rag.db", dim=4)
    store.replace_conversation(
        "fake", "c1", "t", "v1",
        [_msg("m1"), _msg("m2")],
        np.array([[1, 0, 0, 0], [0, 1, 0, 0]], dtype="float32"),
    )
    assert store.stats()["messages"] == 2
    assert store.indexed_exported_at("fake", "c1") == "v1"
    # remplacement : les anciens vecteurs sont supprimes (pas de doublon)
    store.replace_conversation(
        "fake", "c1", "t", "v2",
        [_msg("m1")], np.array([[1, 0, 0, 0]], dtype="float32"),
    )
    assert store.stats()["messages"] == 1
    assert store.indexed_exported_at("fake", "c1") == "v2"
    store.close()


def test_iter_conversation_files(tmp_path):
    for platform in ("alpha", "beta"):
        d = tmp_path / platform
        d.mkdir()
        (d / "conversation_list.json").write_text("{}", encoding="utf-8")
        (d / "conv.json").write_text("{}", encoding="utf-8")
    found = sorted((p, f.name) for p, f in iter_conversation_files(tmp_path))
    assert found == [
        ("alpha", "conv.json"),
        ("beta", "conv.json"),
    ]


def test_keyword_score_et_tokenize():
    from src.rag.search import keyword_score, tokenize

    assert tokenize("Serveur Minecraft, serveur !") == ["serveur", "minecraft"]
    assert keyword_score("Un serveur minecraft", ["serveur", "minecraft"]) == 1.0
    assert keyword_score("Un serveur web", ["serveur", "minecraft"]) == 0.5
    assert keyword_score("exact phrase ici", ["phrase", "ici"], phrase="exact phrase") > 0.5


def test_combined_search_semantique_et_mots_cles(tmp_path):
    import numpy as np
    from src.rag.search import combined_search

    store = VectorStore(tmp_path / "rag.db", dim=4)

    def add(cid, msg_id, texte, vec):
        store.replace_conversation(
            "a", cid, "t", "v", [dict(_msg(msg_id, texte=texte))],
            np.array([vec], dtype="float32"),
        )

    add("c1", "m1", "serveur minecraft", [1.0, 0, 0, 0])
    add("c2", "m2", "mon serveur minecraft est lent", [0.0, 1, 0, 0])
    add("c3", "m3", "recette de crepes", [0.0, 0, 1, 0])

    results = combined_search(
        store, np.array([1.0, 0, 0, 0], dtype="float32"), "minecraft",
        k=5, alpha=0.7,
    )
    ids = [r["message_id"] for r in results]
    assert "m1" in ids and "m2" in ids          # semantique + mots-cles
    assert "m3" not in ids                       # hors sujet
    assert ids[0] == "m1"                        # meilleur sur les deux axes
    m2 = next(r for r in results if r["message_id"] == "m2")
    assert m2["keyword_score"] == 1.0 and m2["semantic_score"] < 0.5

    only = combined_search(
        store, np.array([1.0, 0, 0, 0], dtype="float32"), "crepes",
        k=5, alpha=0.0, platforms=["a"],
    )
    assert only[0]["message_id"] == "m3"
    assert combined_search(
        store, np.array([1.0, 0, 0, 0], dtype="float32"), "crepes",
        k=5, platforms=["b"],
    ) == []
