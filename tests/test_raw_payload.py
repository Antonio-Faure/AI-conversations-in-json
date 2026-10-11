"""Tests du stockage du payload API brut (`exports/<platform>/raw/`)."""

from __future__ import annotations

import json

from src.utils.raw_payload import (
    load_raw_payload,
    raw_payload_dir,
    raw_payload_path,
    save_raw_payload,
)


def test_save_raw_payload_chemin_et_contenu(tmp_path):
    path = save_raw_payload(tmp_path, "grok", "ma-conv", {"titre": "é", "n": 1})
    assert path == tmp_path / "grok" / "raw" / "ma-conv.json"
    assert path == raw_payload_path(tmp_path, "grok", "ma-conv")
    assert raw_payload_dir(tmp_path, "grok") == tmp_path / "grok" / "raw"
    assert json.loads(path.read_text(encoding="utf-8")) == {"titre": "é", "n": 1}
    # ecriture atomique : aucun temporaire residuel
    assert list(path.parent.glob("*.tmp*")) == []


def test_load_raw_payload_absent_ou_present(tmp_path):
    assert load_raw_payload(tmp_path, "grok", "absent") is None
    save_raw_payload(tmp_path, "grok", "c", [1, 2, 3])
    assert load_raw_payload(tmp_path, "grok", "c") == [1, 2, 3]


def test_load_raw_payload_illisible(tmp_path):
    path = raw_payload_path(tmp_path, "chatgpt", "cassee")
    path.parent.mkdir(parents=True)
    path.write_text("{pas du json", encoding="utf-8")
    assert load_raw_payload(tmp_path, "chatgpt", "cassee") is None
