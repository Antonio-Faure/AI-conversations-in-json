"""Tests du CLI scripts/check_etalons.py (resolution des chemins)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "check_etalons", ROOT / "scripts" / "check_etalons.py"
)
check_etalons = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules.setdefault("check_etalons", check_etalons)
spec.loader.exec_module(check_etalons)


def test_output_relatif_resolu_depuis_la_racine(tmp_path, monkeypatch, capsys):
    """`--output relatif` doit pointer dans le depot, pas depuis le CWD."""
    monkeypatch.setattr(check_etalons, "ROOT", tmp_path)
    ailleurs = tmp_path / "ailleurs"
    ailleurs.mkdir()
    monkeypatch.chdir(ailleurs)

    export_dir = tmp_path / "mes-exports" / "grok"
    export_dir.mkdir(parents=True)
    (export_dir / "t.json").write_text(
        json.dumps(
            {
                "conversation_id": "g1",
                "platform": "grok",
                "title": "T",
                "url": "https://grok.com/chat/g1",
                "messages": [
                    {"role": "user", "texte": "bonjour", "code_blocks": []},
                ],
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "etalons.json"
    config.write_text(json.dumps({"grok": [{"id": "g1", "label": "L"}]}), encoding="utf-8")

    monkeypatch.setattr(
        sys, "argv",
        ["check_etalons.py", "--config", str(config), "--output", "mes-exports"],
    )
    code = check_etalons.main()
    out = capsys.readouterr().out
    assert "MANQUE" not in out
    assert code == 0
