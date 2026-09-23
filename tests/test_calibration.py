"""Tests de l'extraction/compactage des conversations d'etalonnage."""

from __future__ import annotations

import json
from pathlib import Path

from src.utils.calibration import (
    bootstraps_from_export,
    build_payload,
    extract_tests,
    load_manifest,
    section_categories,
    select_minimal_cover,
    suite_from_export,
    tag_tests,
)

CAL_DIR = Path(__file__).resolve().parent.parent / "calibration"

SUITE = """
## 1. Formatage Markdown

### Test 1 — Titres
Capacité testée : Affichage de titres H1 et H2.
Préparation : Aucune.
Message utilisateur à envoyer :
```text
Affiche un titre H1 "T" et un H2 "S".
```
Résultat attendu : titres rendus.

### Test 2 — Tableau
Capacité testée : Création d'un tableau Markdown.
Préparation : Aucune.
Message utilisateur à envoyer :
```text
Affiche un tableau de 2 colonnes et 2 lignes.
```
Résultat attendu : tableau.

## 2. LaTeX

### Test 3 — Matrice
Capacité testée : Matrice présentée sous forme de tableau mathématique.
Préparation : Aucune.
Message utilisateur à envoyer :
```text
Affiche une matrice 2x2 et un tableau de valeurs en LaTeX.
```
Résultat attendu : math.

## 3. Images et fichiers

### Test 4 — Image jointe
Capacité testée : Décrire une image fournie.
Préparation : Image jointe.
Message utilisateur à envoyer :
```text
Décris ce que montre cette image.
```
Résultat attendu : description.
"""

FALLBACK = """
## 1. Images

Capacité testée : Décrire une capture d'écran.
Préparation : Aucune.
Message utilisateur à envoyer :
Décris cette capture d'écran en une phrase.
Résultat attendu : description.
"""


def _manifest():
    return load_manifest(CAL_DIR / "manifest.json")


def test_extraction_champs():
    tests = extract_tests(SUITE)
    assert [t.capability for t in tests][:2] == [
        "Affichage de titres H1 et H2.",
        "Création d'un tableau Markdown.",
    ]
    assert tests[0].section == "1. Formatage Markdown"
    assert tests[0].prompt.startswith("Affiche un titre H1")
    assert tests[3].preparation == "Image jointe."


def test_extraction_sans_fences():
    tests = extract_tests(FALLBACK)
    assert len(tests) == 1
    assert tests[0].prompt == "Décris cette capture d'écran en une phrase."


def test_template_ignore():
    template = """
Capacité testée : [une phrase]
Préparation : [fichier, image, audio]
Message utilisateur à envoyer :
```text
[message exact à copier-coller]
```
Résultat attendu : [une phrase]
"""
    assert extract_tests(template) == []


def test_section_filtre_les_domaines_forts():
    assert section_categories("2. LaTeX et mathématiques") == frozenset({"math"})
    assert section_categories("3. Images et fichiers") == frozenset({"media"})
    assert section_categories("1. Formatage Markdown") is None


def test_taggage_et_pieces_jointes():
    tests = extract_tests(SUITE)
    tag_tests(tests, _manifest())
    by_cap = {t.capability: t for t in tests}
    assert "headings" in by_cap["Affichage de titres H1 et H2."].capabilities
    assert "table" in by_cap["Création d'un tableau Markdown."].capabilities
    # section LaTeX : le mot "tableau" ne doit pas produire "table"
    matrix = by_cap["Matrice présentée sous forme de tableau mathématique."]
    assert "latex_matrix" in matrix.capabilities
    assert "table" not in matrix.capabilities
    image = by_cap["Décrire une image fournie."]
    assert "upload_image" in image.capabilities
    assert image.attachments == ["image.png"]


def test_set_cover_couvre_tout():
    tests = extract_tests(SUITE)
    manifest = _manifest()
    tag_tests(tests, manifest)
    required = {cid for t in tests for cid in t.capabilities}
    selected, gaps = select_minimal_cover(tests, required)
    covered = {cid for t in selected for cid in t.capabilities}
    assert not gaps
    assert required <= covered
    assert len(selected) <= len(tests)


def test_build_payload_structure():
    manifest = _manifest()
    tests = extract_tests(SUITE)
    payload = build_payload("demo", tests, manifest, mode="work", generated_from="x.json")
    assert payload["platform"] == "demo"
    assert payload["mode"] == "work"
    assert payload["messages"]
    ids = {c["id"] for c in manifest["capabilities"]}
    for message in payload["messages"]:
        assert set(message["exposes"]) <= ids
        assert message["text"]
    assert set(payload["capabilities_covered"]) <= ids


def test_suite_et_amorces_depuis_export():
    payload = {
        "messages": [
            {"role": "user", "texte": "Dresse une documentation complète de tout."},
            {"role": "assistant", "texte": "doc"},
            {"role": "user", "texte": "À partir de la documentation, crée une suite complète de messages utilisateur."},
            {"role": "assistant", "texte": "SUITE"},
        ]
    }
    assert suite_from_export(payload) == "SUITE"
    boot = bootstraps_from_export(payload)
    assert boot["documentation"].startswith("Dresse")
    assert "suite complète" in boot["suite"]


def test_manifest_coherent():
    manifest = _manifest()
    ids = [c["id"] for c in manifest["capabilities"]]
    assert len(ids) == len(set(ids))
    for cap in manifest["capabilities"]:
        assert cap["category"]
        assert cap["keywords"]
        assert isinstance(cap["parsed_today"], bool)
    assert manifest["media"]["image"] == "image.png"


def test_fichiers_calibration_valides():
    manifest = _manifest()
    ids = {c["id"] for c in manifest["capabilities"]}
    files = [p for p in CAL_DIR.glob("*.json") if p.name not in ("manifest.json", "bootstraps.json")]
    assert files
    for path in files:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload.get("platform")
        if payload.get("status") == "suite_a_generer":
            assert payload["messages"] == []
            continue
        assert payload["messages"], path.name
        for message in payload["messages"]:
            assert message["text"].strip()
            assert set(message["exposes"]) <= ids
