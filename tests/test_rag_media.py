"""Tests de l'indexation medias (scan, references, store). Aucun modele charge."""

from __future__ import annotations

import json
import os

import numpy as np

from src.rag.media import (
    MediaStore,
    build_reference_index,
    index_media,
    iter_media_files,
    media_kind,
)


class FakeEncoder:
    """Encodeur factice : 1 vecteur constant par media."""

    def __init__(self):
        self.calls = 0
        self.encoded = 0

    def __call__(self, payloads):
        self.calls += 1
        self.encoded += len(payloads)
        return np.array([[1.0, 0, 0, 0] for _ in payloads], dtype="float32")


def _write_media(root, platform="alpha"):
    images = root / platform / "images"
    files = root / platform / "files"
    images.mkdir(parents=True, exist_ok=True)
    files.mkdir(parents=True, exist_ok=True)
    (images / "img1.jpg").write_bytes(b"jpeg")
    (files / "clip.mp3").write_bytes(b"mp3")
    (files / "clip.mp4").write_bytes(b"mp4")
    (files / "doc.pdf").write_bytes(b"pdf")  # piece jointe non media
    (files / "data.csv").write_bytes(b"csv")
    (root / platform / "conversation_list.json").write_text("{}", encoding="utf-8")
    (root / platform / "conv1.json").write_text(
        json.dumps(
            {
                "conversation_id": "c1",
                "platform": platform,
                "title": "t",
                "exported_at": "v1",
                "messages": [
                    {
                        "message_id": "m1",
                        "role": "user",
                        "texte": "Voici ![un chat](images/img1.jpg) et [clip](files/clip.mp3)",
                        "code_blocks": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_media_kind():
    from pathlib import Path

    assert media_kind(Path("a.mp3")) == "audio"
    assert media_kind(Path("a.MP4")) == "video"
    assert media_kind(Path("a.jpg")) is None  # les images viennent du dossier images/
    assert media_kind(Path("a.pdf")) is None


def test_iter_media_files_filtre_les_non_medias(tmp_path):
    _write_media(tmp_path)
    found = [(p, k, f.name) for p, k, f in iter_media_files(tmp_path)]
    assert ("alpha", "image", "img1.jpg") in found
    assert ("alpha", "audio", "clip.mp3") in found
    assert ("alpha", "video", "clip.mp4") in found
    assert all(f not in ("doc.pdf", "data.csv") for _, _, f in found)


def test_build_reference_index(tmp_path):
    _write_media(tmp_path)
    refs = build_reference_index(tmp_path / "alpha")
    assert refs[("images", "img1.jpg")] == {
        "conversation_id": "c1",
        "message_id": "m1",
        "texte": "un chat",
    }
    assert refs[("files", "clip.mp3")]["conversation_id"] == "c1"
    assert refs[("files", "clip.mp3")]["texte"] == "clip"


def test_index_media_incremental(tmp_path):
    _write_media(tmp_path)
    store = MediaStore(tmp_path / "rag.db", dim=4)
    encoder = FakeEncoder()

    first = index_media(store, encoder, tmp_path, "fake-model")
    assert first["indexed"] == 3 and first["skipped"] == 0
    assert first["per_kind"] == {"image": 1, "audio": 1, "video": 1}
    assert encoder.encoded == 3
    assert store.stats()["total"] == 3

    # relance sans modification : tout est repris du cache
    second = index_media(store, encoder, tmp_path, "fake-model")
    assert second["indexed"] == 0 and second["skipped"] == 3
    assert encoder.encoded == 3

    # fichier modifie (mtime) : seul ce media est recalcule
    os.utime(tmp_path / "alpha" / "images" / "img1.jpg", (1, 1))
    third = index_media(store, encoder, tmp_path, "fake-model")
    assert third["indexed"] == 1 and third["skipped"] == 2
    assert store.stats()["total"] == 3
    store.close()


def test_media_search_et_metadonnees(tmp_path):
    _write_media(tmp_path)
    store = MediaStore(tmp_path / "rag.db", dim=4)
    index_media(store, FakeEncoder(), tmp_path, "fake-model")
    results = store.search(np.array([1.0, 0, 0, 0], dtype="float32"), k=5)
    assert len(results) == 3
    assert results[0]["score"] > 0.99
    by_kind = {r["kind"]: r for r in results}
    assert by_kind["image"]["conversation_id"] == "c1"
    assert by_kind["image"]["path"] == "alpha/images/img1.jpg"
    assert by_kind["audio"]["texte"] == "clip"
    only_images = store.search(np.array([1.0, 0, 0, 0], dtype="float32"), k=5, kind="image")
    assert [r["kind"] for r in only_images] == ["image"]
    store.close()


def test_index_media_media_fautif_est_saute(tmp_path):
    """Un media qui echoue a l'encodage ne bloque pas les suivants."""
    _write_media(tmp_path)
    store = MediaStore(tmp_path / "rag.db", dim=4)

    class FlakyEncoder(FakeEncoder):
        def __call__(self, payloads):
            if any(str(p).endswith("img1.jpg") for _, p in payloads):
                raise RuntimeError("image corrompue")
            return super().__call__(payloads)

    stats = index_media(store, FlakyEncoder(), tmp_path, "fake-model")
    assert stats["indexed"] == 2 and stats["failed"] == 1
    assert store.stats()["total"] == 2
    store.close()
