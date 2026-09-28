"""Tests de la generation locale de medias (src/utils/media.py)."""

from __future__ import annotations

import csv
import io
import json

from src.utils.media import generate_media, make_pdf


def test_generate_media_sans_av(tmp_path):
    files = generate_media(tmp_path, with_av=False)
    names = {path.name for path in files}
    assert {"document.pdf", "donnees.csv", "notes.txt", "notes.md", "donnees.json"} <= names
    assert all(path.exists() for path in files)


def test_pdf_valide(tmp_path):
    path = make_pdf(tmp_path / "d.pdf", "Titre", ["ligne (test) \\ ok"])
    data = path.read_bytes()
    assert data.startswith(b"%PDF-1.4")
    assert data.rstrip().endswith(b"%%EOF")


def test_csv_et_json(tmp_path):
    files = {p.name: p for p in generate_media(tmp_path, with_av=False)}
    rows = list(csv.reader(io.StringIO(files["donnees.csv"].read_text(encoding="utf-8"))))
    assert rows[0] == ["nom", "quantite", "prix"]
    assert len(rows) == 3
    data = json.loads(files["donnees.json"].read_text(encoding="utf-8"))
    assert data["etalon"] is True
