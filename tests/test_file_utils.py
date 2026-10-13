"""Tests des helpers fichiers partages (`src/utils/file_utils.py`).

`read_json` ne doit jamais avaler silencieusement un fichier corrompu : un
avertissement explicite (chemin + cause) est ecrit sur stderr, puis la valeur
par defaut est renvoyee (compatibilite des appelants tolerants) ; `strict=True`
propage l'erreur.
"""

from __future__ import annotations

import json

import pytest

from src.utils.file_utils import read_json


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
