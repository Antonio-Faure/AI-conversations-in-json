"""Atomicite des ecritures des scripts batch (tmp + `os.replace`).

Les scripts qui reecrivent des JSON d'archive/config (reparse, backfill,
check_etalons, calibration_build) et sanitize doivent passer par
`write_json_atomic`/`write_text_atomic` : si le remplacement final echoue
(disque plein, kill), l'ancien contenu doit rester intact. Le test casse
`os.replace` vu par `file_utils` (remplacement simule en echec) et verifie que
le fichier d'origine n'a pas ete touche.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from src.utils import file_utils

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


sanitize = _load("sanitize")
reparse_exports = _load("reparse_exports")
backfill_urls = _load("backfill_urls")
check_etalons = _load("check_etalons")
calibration_build = _load("calibration_build")


class _FakeOS:
    """os minimal : getpid reel, replace casse (simule un remplacement rate)."""

    getpid = staticmethod(os.getpid)

    @staticmethod
    def replace(*args, **kwargs):
        raise OSError("disque plein (simule)")


@pytest.fixture()
def replace_ko(monkeypatch):
    """Rend `os.replace` (vu par file_utils) systematiquement en echec."""
    monkeypatch.setattr(file_utils, "os", _FakeOS)


def _write_export(tmp_path: Path, platform: str, stem: str, payload) -> Path:
    directory = tmp_path / platform
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / f"{stem}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    (directory / "conversation_list.json").write_text(
        json.dumps(
            {
                "platform": platform,
                "conversations": [{"conversation_id": payload["conversation_id"], "file": stem}],
            }
        ),
        encoding="utf-8",
    )
    return json_path


def test_sanitize_ecriture_atomique(tmp_path, monkeypatch, replace_ko):
    target = tmp_path / "notes.md"
    original = "id 123e4567-e89b-12d3-a456-426614174000\n"
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(sanitize, "ROOT", tmp_path)
    monkeypatch.setattr(sanitize, "_targets", lambda: [target])
    assert sanitize.sanitize_text(original) != original  # la reecriture est due
    with pytest.raises(OSError):
        sanitize.run(apply=True)
    assert target.read_text(encoding="utf-8") == original


def test_reparse_ecriture_atomique(tmp_path, monkeypatch, replace_ko):
    payload = {
        "conversation_id": "g1",
        "platform": "grok",
        "title": "T",
        "url": "https://grok.com/chat/g1",
        "messages": [],
    }
    json_path = _write_export(tmp_path, "grok", "t", payload)
    original = json_path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv",
        ["reparse_exports.py", "--exports", str(tmp_path), "--no-backup"],
    )
    with pytest.raises(OSError):
        reparse_exports.main()
    assert json_path.read_text(encoding="utf-8") == original


def test_backfill_urls_ecriture_atomique(tmp_path, monkeypatch, replace_ko):
    payload = {
        "conversation_id": "g1",
        "platform": "grok",
        "title": "T",
        "url": "",
        "messages": [],
    }
    json_path = _write_export(tmp_path, "grok", "t", payload)
    original = json_path.read_text(encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["backfill_urls.py", "--exports", str(tmp_path)])
    with pytest.raises(OSError):
        backfill_urls.main()
    assert json_path.read_text(encoding="utf-8") == original


def test_check_etalons_ecriture_atomique(tmp_path, monkeypatch, replace_ko, capsys):
    config = tmp_path / "etalons.json"
    entries = {"grok": [{"id": "g1", "label": "L", "baseline": {}}]}
    config.write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv",
        ["check_etalons.py", "--config", str(config),
         "--output", str(tmp_path / "exports"), "--update"],
    )
    with pytest.raises(OSError):
        check_etalons.main()
    assert json.loads(config.read_text(encoding="utf-8")) == entries


def test_calibration_build_ecriture_atomique(tmp_path, replace_ko):
    target = tmp_path / "bot.json"
    target.write_text('{"ancien": true}', encoding="utf-8")
    with pytest.raises(OSError):
        calibration_build._write(target, {"nouveau": True})
    assert json.loads(target.read_text(encoding="utf-8")) == {"ancien": True}
