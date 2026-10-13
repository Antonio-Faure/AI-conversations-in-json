"""Tests des helpers fichiers partages (`src/utils/file_utils.py`).

`read_json` ne doit jamais avaler silencieusement un fichier corrompu : un
avertissement explicite (chemin + cause) est ecrit sur stderr, puis la valeur
par defaut est renvoyee (compatibilite des appelants tolerants) ; `strict=True`
propage l'erreur.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.utils.file_utils import (
    find_export_by_id,
    read_json,
    resolve_cli_path,
)


def test_read_json_absent_retourne_defaut(tmp_path):
    assert read_json(tmp_path / "absent.json", default=None) is None
    assert read_json(tmp_path / "absent.json", default={}) == {}


def test_read_json_valide(tmp_path):
    path = tmp_path / "ok.json"
    path.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert read_json(path, default=None) == {"a": 1}


def test_read_json_corrompu_avertit_sur_stderr(tmp_path, capsys):
    path = tmp_path / "corrompu.json"
    path.write_text('{"conversations": [', encoding="utf-8")
    assert read_json(path, default=None) is None
    err = capsys.readouterr().err
    assert "JSON illisible" in err
    assert str(path) in err


def test_read_json_strict_leve_au_lieu_de_masquer(tmp_path):
    path = tmp_path / "corrompu.json"
    path.write_text("pas du json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        read_json(path, default=None, strict=True)


def test_resolve_cli_path_relatif_absolu_et_expanduser(tmp_path):
    root = tmp_path / "depot"
    assert resolve_cli_path("exports", root) == root / "exports"
    assert resolve_cli_path(Path("/ailleurs/exports"), root) == Path("/ailleurs/exports")
    assert (
        resolve_cli_path("~/exports", root, expanduser=True)
        == Path("~/exports").expanduser()
    )
    assert resolve_cli_path("~/exports", root) == root / "~" / "exports"


def test_find_export_by_id_ignore_les_fichiers_reserves(tmp_path):
    platform = tmp_path / "grok"
    platform.mkdir()
    (platform / "conversation_list.json").write_text(
        json.dumps({"conversations": [{"conversation_id": "g1"}]}), encoding="utf-8"
    )
    export = platform / "t.json"
    export.write_text(
        json.dumps({"conversation_id": "g1", "messages": []}), encoding="utf-8"
    )
    assert find_export_by_id(tmp_path, "grok", "g1") == export
    assert find_export_by_id(tmp_path, "grok", "absent") is None
    assert find_export_by_id(tmp_path, "inconnue", "g1") is None


def test_find_export_by_id_ignore_un_json_illisible(tmp_path, capsys):
    platform = tmp_path / "grok"
    platform.mkdir()
    (platform / "corrompu.json").write_text('{"conversation_id": ', encoding="utf-8")
    export = platform / "t.json"
    export.write_text(json.dumps({"conversation_id": "g1"}), encoding="utf-8")
    assert find_export_by_id(tmp_path, "grok", "g1") == export
    assert "JSON illisible" in capsys.readouterr().err
