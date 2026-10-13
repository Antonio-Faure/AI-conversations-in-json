"""Tests du CLI scripts/backfill_urls.py (URL + inventaire unifie)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "backfill_urls", ROOT / "scripts" / "backfill_urls.py"
)
backfill = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules.setdefault("backfill_urls", backfill)
spec.loader.exec_module(backfill)


def _run_main(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys, "argv",
        ["backfill_urls.py", "--exports", str(tmp_path)],
    )
    return backfill.main()


def test_main_renseigne_url_et_reecrit_inventaire(tmp_path, monkeypatch, capsys):
    """URL reconstruite dans le JSON et l'inventaire, au format du writer partage."""
    directory = tmp_path / "grok"
    directory.mkdir()
    json_path = directory / "t.json"
    json_path.write_text(
        json.dumps({
            "conversation_id": "g1",
            "platform": "grok",
            "title": "T",
            "url": "",
            "last_message_at": "2026-01-02T00:00:00Z",
            "messages": [{"role": "user", "texte": "bonjour"}],
        }),
        encoding="utf-8",
    )
    list_path = directory / "conversation_list.json"
    list_path.write_text(
        json.dumps({
            "platform": "grok",
            "updated_at": "2020-01-01T00:00:00Z",
            "count": 42,
            "conversations": [
                {"conversation_id": "g1", "title": "T", "file": "t",
                 "last_message_at": "2026-01-02T00:00:00Z"},
            ],
        }),
        encoding="utf-8",
    )

    assert _run_main(monkeypatch, tmp_path) == 0
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["url"] == "https://grok.com/chat/g1"
    data = json.loads(list_path.read_text(encoding="utf-8"))
    assert data["platform"] == "grok"
    assert data["count"] == 1
    assert data["updated_at"] != "2020-01-01T00:00:00Z"
    assert data["conversations"][0]["url"] == "https://grok.com/chat/g1"
    assert "1 URLs ajoutees/mises a jour" in capsys.readouterr().out


def test_main_url_deja_presente_inchangee(tmp_path, monkeypatch, capsys):
    """Une URL deja renseignee n'est pas reecrite (compteur a 0)."""
    directory = tmp_path / "grok"
    directory.mkdir()
    json_path = directory / "t.json"
    payload = {
        "conversation_id": "g1",
        "platform": "grok",
        "title": "T",
        "url": "https://grok.com/chat/g1",
        "messages": [],
    }
    json_path.write_text(json.dumps(payload), encoding="utf-8")
    before = json_path.read_text(encoding="utf-8")
    (directory / "conversation_list.json").write_text(
        json.dumps({
            "platform": "grok",
            "conversations": [{"conversation_id": "g1", "title": "T", "file": "t"}],
        }),
        encoding="utf-8",
    )

    assert _run_main(monkeypatch, tmp_path) == 0
    assert json_path.read_text(encoding="utf-8") == before
    assert "0 URLs ajoutees/mises a jour" in capsys.readouterr().out
