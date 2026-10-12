"""Garde des references de forme : coherentes avec les payloads d'etalon.

Les references versionnees (`tests/fixtures/api_shapes/<platform>.shape.json`)
sont generees par `scripts/api_bench.py update-ref` (scope `corpus` : union des
payloads bruts de la plateforme, qui contient les conversations d'etalon). Ce
test detecte une edition accidentelle :
  - la signature doit etre le hash de la forme stockee ;
  - chaque payload d'etalon doit rester compatible avec la reference (score
    >= 95) : une reference qui ne couvre plus l'etalon est invalide.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.orchestrator import load_config
from src.utils.api_shape import (
    compare_shapes,
    shape_of,
    signature_of_shape,
)
from src.utils.etalon import etalons_file, load_etalons
from src.utils.file_utils import (
    conversation_list_path,
    load_conversation_list,
    read_json,
)
from src.utils.raw_payload import raw_payload_path

ROOT = Path(__file__).resolve().parent.parent
SHAPES_DIR = ROOT / "tests" / "fixtures" / "api_shapes"
PLATFORMS = ("chatgpt", "claude", "gemini", "grok", "mistral", "perplexity")

#: vocabulaire ferme d'un noeud de forme (anti-fuite : aucune valeur scalaire)
ALLOWED_TYPES = {"str", "int", "float", "bool", "null", "list", "object"}
ALLOWED_TAGS = {"uuid", "iso-date", "url", "email", "hash"}
ALLOWED_KEYS = {"t", "c", "i", "n", "fmt", "cut"}


def _walk_shape(node: dict, path: str) -> None:
    assert isinstance(node, dict), f"{path}: noeud non-objet"
    assert set(node) <= ALLOWED_KEYS, f"{path}: cles inattendues {set(node)}"
    kinds = set(str(node["t"]).split("+"))
    assert kinds and kinds <= ALLOWED_TYPES, f"{path}: type inattendu {node['t']}"
    if "fmt" in node:
        assert set(node["fmt"]) <= ALLOWED_TAGS, f"{path}: tag inattendu {node['fmt']}"
    for key, child in (node.get("c") or {}).items():
        _walk_shape(child, f"{path}.{key}")
    if isinstance(node.get("i"), dict):
        _walk_shape(node["i"], f"{path}[]")


def _reference(platform: str) -> dict:
    doc = read_json(SHAPES_DIR / f"{platform}.shape.json", default=None)
    assert isinstance(doc, dict), f"reference de forme manquante: {platform}"
    return doc


@pytest.mark.parametrize("platform", PLATFORMS)
def test_reference_presente(platform):
    doc = _reference(platform)
    assert doc["platform"] == platform
    assert isinstance(doc.get("shape"), dict)
    assert doc.get("signature")
    assert doc.get("scope") in ("from", "etalons", "corpus")


@pytest.mark.parametrize("platform", PLATFORMS)
def test_reference_value_free(platform):
    """Aucune valeur scalaire des payloads ne doit apparaitre dans la reference."""
    _walk_shape(_reference(platform)["shape"], "$")


@pytest.mark.parametrize("platform", PLATFORMS)
def test_signature_coherente_avec_la_forme(platform):
    """Une forme editee sans regenerer la signature doit faire echouer le test."""
    doc = _reference(platform)
    assert doc["signature"] == signature_of_shape(doc["shape"]), (
        f"{platform}: forme editee sans `update-ref` (signature incoherente)"
    )


def _etalon_payloads(platform: str) -> list:
    """Payloads bruts des conversations d'etalon (vide si absents localement)."""
    etalons = etalons_file()
    if etalons.name.endswith(".example.json"):
        pytest.skip("scripts/etalons.json absent (ids reels non versionnes)")
    entries = load_etalons(etalons).get(platform) or []
    if not entries:
        pytest.skip(f"{platform}: aucun etalon configure")
    config = load_config(ROOT / "config.yaml")
    output_dir = Path(config["output_dir"])
    index = load_conversation_list(conversation_list_path(output_dir, platform))
    payloads = []
    for entry in entries:
        stem = (index.get(str(entry.get("id"))) or {}).get("file")
        if not stem:
            continue
        payload = read_json(raw_payload_path(output_dir, platform, stem), default=None)
        if payload is not None:
            payloads.append(payload)
    if not payloads:
        pytest.skip(f"{platform}: payloads d'etalon absents localement")
    return payloads


@pytest.mark.parametrize("platform", PLATFORMS)
def test_reference_couvre_les_payloads_etalon(platform):
    """Chaque payload d'etalon doit rester compatible avec la reference."""
    doc = _reference(platform)
    for payload in _etalon_payloads(platform):
        report = compare_shapes(doc["shape"], shape_of(payload))
        assert report["score"] >= 95, (
            f"{platform}: payload d'etalon incompatible avec la reference "
            f"({report['score']}%, added={report['added'][:5]}) -> "
            f"regenerer avec `update-ref`"
        )
