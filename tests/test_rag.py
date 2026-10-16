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


def test_filtres_platform_et_role_avant_le_knn(tmp_path):
    """Filtre plateforme/role : k resultats pleins, pas tronques par le KNN.

    Regression : sqlite-vec applique `k` avant les filtres de jointure ; avec
    20 messages d'une autre plateforme plus proches, le filtre B ne renvoyait
    aucun resultat (k consomme par les voisins non filtres).
    """
    store = VectorStore(tmp_path / "rag.db", dim=4)

    def unit(*values):
        arr = np.array(values, dtype="float32")
        return arr / np.linalg.norm(arr)

    # 20 messages A tres proches de la requete (tout le top-5 non filtre)
    store.replace_conversation(
        "a", "ca", "t", "v",
        [_msg(f"a{i}", "user") for i in range(20)],
        [unit(1.0, 0.05, 0, 0) for _ in range(20)],
    )
    # 5 messages B plus lointains, tous sous le top-5 non filtre
    store.replace_conversation(
        "b", "cb", "t", "v",
        [_msg(f"b{i}", "assistant") for i in range(5)],
        [unit(1.0, y, 0, 0) for y in (0.3, 0.5, 0.7, 1.0, 1.4)],
    )
    query = unit(1.0, 0.0, 0, 0)
    assert all(r["platform"] == "a" for r in store.search(query, k=5))

    only_b = store.search(query, k=5, platform="b")
    assert len(only_b) == 5
    assert {r["message_id"] for r in only_b} == {f"b{i}" for i in range(5)}
    # tries par similarite decroissante
    assert [r["message_id"] for r in only_b] == [f"b{i}" for i in range(5)]

    only_assistant = store.search(query, k=5, role="assistant")
    assert len(only_assistant) == 5
    both = store.search(query, k=3, platform="b", role="assistant")
    assert len(both) == 3
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
    # elision francaise : l'apostrophe separe les tokens
    assert tokenize("l'IA d'aujourd'hui") == ["ia", "aujourd", "hui"]
    assert keyword_score("Un serveur minecraft", ["serveur", "minecraft"]) == 1.0
    assert keyword_score("Un serveur web", ["serveur", "minecraft"]) == 0.5
    assert keyword_score("exact phrase ici", ["phrase", "ici"], phrase="exact phrase") > 0.5


def test_keyword_score_token_entier():
    """Le mot-cle matche un token entier, pas une sous-chaine."""
    from src.rag.search import keyword_score

    # faux positifs de la sous-chaine : « ia » dans « maison » / « social »
    assert keyword_score("une maison ancienne", ["ia"]) == 0.0
    assert keyword_score("un réseau social", ["ia"]) == 0.0
    assert keyword_score("l'IA générative", ["ia"]) == 1.0
    # token entier : « serveur » ne matche pas « serveurs »
    assert keyword_score("deux serveurs web", ["serveur"]) == 0.0
    # casse et accents
    assert keyword_score("LES DONNÉES du capteur", ["donnees"]) == 1.0
    assert keyword_score("les donnees du capteur", ["données"]) == 1.0
    assert keyword_score("SERVEUR", ["serveur"]) == 1.0


def test_keyword_score_phrase_exacte_sans_accents():
    from src.rag.search import keyword_score

    assert keyword_score("la théorie des cordes", ["theorie"], phrase="theorie des cordes") == 1.0
    # bonus phrase seul (terme absent) : phrase matchee malgre les accents
    assert keyword_score("une théorie ancienne", ["physique"], phrase="une theorie") == 0.3


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


def test_index_text_inclut_les_champs_pompes():
    from src.rag.indexer import index_text

    text = index_text(
        {
            "texte": "reponse",
            "reasoning": "pensee",
            "artifacts": ["a.html"],
            "sources": ["Titre — https://x"],
            "tools": ["Recherche web"],
        }
    )
    for marker in ("reponse", "[Raisonnement]", "pensee", "[Artefacts]",
                   "[Sources]", "[Outils]", "Recherche web"):
        assert marker in text
    assert index_text({"texte": ""}) == ""
    assert index_text({"texte": "", "reasoning": "pensee"}) == "[Raisonnement]\npensee"


def test_base_restreinte_au_proprietaire(tmp_path):
    """Base + WAL/SHM en 0600 : l'index n'est pas lisible par les autres."""
    import stat

    db_path = tmp_path / "rag.db"
    store = VectorStore(db_path, dim=4)
    try:
        for suffix in ("", "-wal", "-shm"):
            path = db_path.parent / f"{db_path.name}{suffix}"
            if path.exists():
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        store.close()


def test_as_text_inputs_embeddinggemma_vs_autres():
    """EmbeddingGemma recoit des dicts texte explicites, les autres modeles des str.

    Sans cela, le processor multimodal interprete toute URL nue comme un media
    a telecharger (crash yt_dlp sur les URL YouTube).
    """
    from src.rag.embeddings import Embedder

    gemma = Embedder("google/embeddinggemma-2")
    assert gemma.dim == 768
    assert gemma._as_text_inputs(["a", "b"]) == [{"text": "a"}, {"text": "b"}]

    autre = Embedder("fake-model")
    assert autre._as_text_inputs(["a"]) == ["a"]


def test_encode_restaure_ordre_apres_tri(monkeypatch):
    """Le tri par longueur (anti-padding) ne change pas l'ordre des vecteurs."""
    import numpy as np

    from src.rag.embeddings import Embedder

    embedder = Embedder("fake-model")

    class FakeModel:
        def encode(self, texts, **kwargs):
            # vecteur identifiable : longueur du texte
            return np.array([[float(len(t))] * 4 for t in texts], dtype="float32")

    monkeypatch.setattr(embedder, "_load", lambda: FakeModel())
    out = embedder.encode(["aaaa", "a", "aaa"])
    assert [int(v[0]) for v in out] == [4, 1, 3]


def test_texte_fautif_est_saute_sans_tuer_le_run(tmp_path):
    """Un texte qui fait echouer l'encodeur est saute (log) ; le run continue."""
    base = [
        {"message_id": "m1", "role": "user", "texte": "bonjour", "code_blocks": []},
        {"message_id": "m2", "role": "assistant", "texte": "POISON", "code_blocks": []},
        {"message_id": "m3", "role": "user", "texte": "au revoir", "code_blocks": []},
    ]
    root = tmp_path / "exports"
    _write_export(root, base)
    store = VectorStore(tmp_path / "rag.db", dim=4)

    class FlakyEmbedder(FakeEmbedder):
        def encode(self, texts):
            if any("POISON" in text for text in texts):
                raise RuntimeError("texte empoisonne")
            return super().encode(texts)

    stats = index_exports(store, FlakyEmbedder(), root, force=True)
    assert stats["vectors_failed"] == 1
    assert stats["vectors_computed"] == 2
    assert stats["messages"] == 2
    assert store.stats()["messages"] == 2
    store.close()
