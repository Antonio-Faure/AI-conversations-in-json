"""Tests de la regeneration des exports (HTML ou payload API brut)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "reparse_exports", ROOT / "scripts" / "reparse_exports.py"
)
reparse = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules.setdefault("reparse_exports", reparse)
spec.loader.exec_module(reparse)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _write_export(tmp_path, platform, stem, payload, raw=None):
    directory = tmp_path / platform
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stem}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    if raw is not None:
        raw_dir = directory / "raw"
        raw_dir.mkdir()
        (raw_dir / f"{stem}.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8"
        )
    return directory / f"{stem}.json"


def _write_list(directory, entries):
    (directory / "conversation_list.json").write_text(
        json.dumps(
            {"platform": directory.name, "count": len(entries), "conversations": entries}
        ),
        encoding="utf-8",
    )


def _run_main(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys, "argv",
        ["reparse_exports.py", "--exports", str(tmp_path), "--no-backup"],
    )
    return reparse.main()


def test_reparse_api_chatgpt_depuis_le_payload():
    payload = json.loads(
        (FIXTURES / "chatgpt_api_conversation.json").read_text(encoding="utf-8")
    )
    old = {
        "conversation_id": payload["conversation_id"],
        "url": "https://chatgpt.com/c/" + payload["conversation_id"],
    }
    conv = reparse._reparse_api("chatgpt", old, payload)
    assert conv.conversation_id == payload["conversation_id"]
    assert len(conv.messages) == 52
    assert [m.role for m in conv.messages] == ["user", "assistant"] * 26
    assert conv.url == old["url"]


def test_reparse_api_grok_depuis_le_payload():
    raw = {
        "conversation": {
            "conversationId": "g1",
            "title": "T",
            "createTime": "2026-05-01T09:00:00Z",
            "modifyTime": "2026-05-01T09:01:00Z",
        },
        "responses": [
            {"responseId": "r1", "sender": "human", "message": "Bonjour"},
            {
                "responseId": "r2",
                "sender": "assistant",
                "message": "Salut",
                "model": "grok-4",
            },
        ],
    }
    conv = reparse._reparse_api("grok", {"conversation_id": "g1"}, raw)
    assert [m.role for m in conv.messages] == ["user", "assistant"]
    assert conv.url == "https://grok.com/chat/g1"


def test_main_ignore_une_conversation_sans_artefact(tmp_path, monkeypatch, capsys):
    """Sans HTML ni payload brut : nettoyage direct, aucun plantage."""
    json_path = _write_export(
        tmp_path, "chatgpt", "sans-artefact",
        {
            "conversation_id": "c1",
            "platform": "chatgpt",
            "title": "T",
            "url": "https://chatgpt.com/c/c1",
            "messages": [{"role": "user", "texte": "bonjour"}],
        },
    )
    _write_list(tmp_path / "chatgpt", [
        {"conversation_id": "c1", "title": "T", "file": "sans-artefact"},
    ])
    assert _run_main(monkeypatch, tmp_path) == 0
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["messages"][0]["texte"] == "bonjour"
    assert "1 nettoyes" in capsys.readouterr().out


def test_main_remappe_depuis_le_payload_brut(tmp_path, monkeypatch, capsys):
    """Conversation migree en API (pas de HTML) : re-mapping depuis raw/."""
    payload = json.loads(
        (FIXTURES / "chatgpt_api_conversation.json").read_text(encoding="utf-8")
    )
    json_path = _write_export(
        tmp_path, "chatgpt", "formatage",
        {
            "conversation_id": payload["conversation_id"],
            "platform": "chatgpt",
            "title": "Formatage de texte",
            "url": "",
            "messages": [],
        },
        raw=payload,
    )
    _write_list(tmp_path / "chatgpt", [
        {
            "conversation_id": payload["conversation_id"],
            "title": "Formatage de texte",
            "file": "formatage",
        },
    ])
    assert _run_main(monkeypatch, tmp_path) == 0
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(data["messages"]) == 52
    assert data["url"] == "https://chatgpt.com/c/" + payload["conversation_id"]
    out = capsys.readouterr().out
    assert "1 remappes" in out
