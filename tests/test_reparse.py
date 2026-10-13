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


def test_reparse_api_claude_depuis_le_payload():
    payload = json.loads(
        (FIXTURES / "claude_api_conversation.json").read_text(encoding="utf-8")
    )
    conv_id = payload["uuid"]
    old = {
        "conversation_id": conv_id,
        "url": "https://claude.ai/chat/" + conv_id,
    }
    conv = reparse._reparse_api("claude", old, payload)
    assert conv.conversation_id == conv_id
    assert len(conv.messages) == 50
    assert [m.role for m in conv.messages] == ["user", "assistant"] * 25
    assert conv.url == old["url"]


def test_reparse_api_mistral_depuis_le_payload():
    payload = json.loads(
        (FIXTURES / "mistral_api_conversation.json").read_text(encoding="utf-8")
    )
    conv_id = (payload.get("chat") or {}).get("id")
    conv = reparse._reparse_api("mistral", {"conversation_id": conv_id}, payload)
    assert conv.conversation_id == conv_id
    assert len(conv.messages) == 26
    assert [m.role for m in conv.messages][:2] == ["user", "assistant"]
    assert conv.url == "https://chat.mistral.ai/work/" + conv_id


def test_reparse_api_perplexity_depuis_le_payload():
    payload = json.loads(
        (FIXTURES / "perplexity_api_thread.json").read_text(encoding="utf-8")
    )
    conv_id = "f0000000-0000-4000-8000-7d3b48b1fa63"
    old = {
        "conversation_id": conv_id,
        "url": "https://www.perplexity.ai/search/" + conv_id,
    }
    conv = reparse._reparse_api("perplexity", old, payload)
    assert conv.conversation_id == conv_id
    assert len(conv.messages) == 14
    assert [m.role for m in conv.messages] == ["user", "assistant"] * 7
    assert conv.url == old["url"]


def test_reparse_api_gemini_depuis_le_payload():
    payload = json.loads(
        (FIXTURES / "gemini_api_conversation.json").read_text(encoding="utf-8")
    )
    conv_id = payload["conversation_id"]
    old = {
        "conversation_id": conv_id,
        "title": payload["title"],
        "url": "https://gemini.google.com/app/" + conv_id,
    }
    conv = reparse._reparse_api("gemini", old, payload)
    assert conv.conversation_id == conv_id
    assert conv.title == payload["title"]
    assert len(conv.messages) == 60
    assert [m.role for m in conv.messages][:2] == ["user", "assistant"]
    assert conv.url == old["url"]


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


def test_main_payload_api_prime_sur_un_html_residuel(tmp_path, monkeypatch, capsys):
    """Migration JSON-only : un .html d'un ancien scrape DOM ne doit pas gagner."""
    payload = json.loads(
        (FIXTURES / "mistral_api_conversation.json").read_text(encoding="utf-8")
    )
    conv_id = (payload.get("chat") or {}).get("id")
    json_path = _write_export(
        tmp_path, "mistral", "etalon",
        {
            "conversation_id": conv_id,
            "platform": "mistral",
            "title": "Etalon",
            "url": "https://chat.mistral.ai/work/" + conv_id,
            "messages": [],
        },
        raw=payload,
    )
    # HTML residuel invalide : s'il gagnait, le re-parse echouerait
    json_path.with_suffix(".html").write_text("<html>obsolète</html>", encoding="utf-8")
    _write_list(tmp_path / "mistral", [
        {"conversation_id": conv_id, "title": "Etalon", "file": "etalon"},
    ])
    assert _run_main(monkeypatch, tmp_path) == 0
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(data["messages"]) == 26
    out = capsys.readouterr().out
    assert "1 remappes" in out and "0 echecs" in out


def _grok_message(mid, role, texte, ts):
    """Message au schema complet (comme ecrit par l'orchestrateur)."""
    return {
        "conversation_id": "g1",
        "message_id": mid,
        "role": role,
        "platform": "grok",
        "model": None,
        "timestamp": ts,
        "texte": texte,
        "code_blocks": [],
        "reasoning": "",
        "artifacts": [],
        "sources": [],
        "tools": [],
        "variants": [],
    }


def _grok_conversation():
    return {
        "conversation_id": "g1",
        "platform": "grok",
        "title": "T",
        "url": "https://grok.com/chat/g1",
        "model": None,
        "started_at": "2026-05-01T09:00:00Z",
        "last_message_at": "2026-05-01T09:01:00Z",
        "exported_at": "2026-05-02T00:00:00Z",
        "messages": [
            _grok_message("r1", "user", "Bonjour", "2026-05-01T09:00:00Z"),
            _grok_message("r2", "assistant", "Salut", "2026-05-01T09:01:00Z"),
        ],
    }


def test_main_ne_touche_pas_les_json_non_conversation(tmp_path, monkeypatch, capsys):
    """`api_health.json` (et tout JSON sans id) reste intact."""
    json_path = _write_export(
        tmp_path, "grok", "t",
        _grok_conversation(),
    )
    health = {"platform": "grok", "n": 20, "mean_score": 100.0}
    (tmp_path / "grok" / "api_health.json").write_text(
        json.dumps(health), encoding="utf-8"
    )
    notes = {"note": "fichier etranger au schema"}
    (tmp_path / "grok" / "notes.json").write_text(json.dumps(notes), encoding="utf-8")
    _write_list(tmp_path / "grok", [
        {"conversation_id": "g1", "title": "T", "file": "t", "message_count": 2},
    ])
    assert _run_main(monkeypatch, tmp_path) == 0
    assert json.loads((tmp_path / "grok" / "api_health.json").read_text()) == health
    assert json.loads((tmp_path / "grok" / "notes.json").read_text()) == notes
    assert len(json.loads(json_path.read_text())["messages"]) == 2
    assert "1 ignores" in capsys.readouterr().out


def test_main_payload_tronque_conserve_le_json_complet(tmp_path, monkeypatch, capsys):
    """Un payload brut tronque (fenetre partielle) ne degrade pas l'archive."""
    json_path = _write_export(tmp_path, "grok", "t", _grok_conversation())
    raw = {
        "conversation": {
            "conversationId": "g1",
            "title": "T",
            "createTime": "2026-05-01T09:00:00Z",
            "modifyTime": "2026-05-01T09:01:00Z",
        },
        # scrape partiel : seule la reponse finale a ete recuperee
        "responses": [
            {"responseId": "r2", "sender": "assistant", "message": "Salut"},
        ],
    }
    raw_dir = tmp_path / "grok" / "raw"
    raw_dir.mkdir()
    (raw_dir / "t.json").write_text(json.dumps(raw), encoding="utf-8")
    _write_list(tmp_path / "grok", [
        {"conversation_id": "g1", "title": "T", "file": "t", "message_count": 2},
    ])
    assert _run_main(monkeypatch, tmp_path) == 0
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(data["messages"]) == 2
    assert data["messages"][0]["texte"] == "Bonjour"
    assert "1 conserves" in capsys.readouterr().out


def test_main_reparse_complet_met_a_jour_le_json(tmp_path, monkeypatch, capsys):
    """Non-regression : un payload complet continue de rafraichir l'archive."""
    old = _grok_conversation()
    old["messages"] = old["messages"][:1]  # archive partielle (1 message)
    json_path = _write_export(tmp_path, "grok", "t", old)
    raw = {
        "conversation": {
            "conversationId": "g1",
            "title": "T",
            "createTime": "2026-05-01T09:00:00Z",
            "modifyTime": "2026-05-01T09:01:00Z",
        },
        "responses": [
            {"responseId": "r1", "sender": "human", "message": "Bonjour"},
            {"responseId": "r2", "sender": "assistant", "message": "Salut"},
        ],
    }
    raw_dir = tmp_path / "grok" / "raw"
    raw_dir.mkdir()
    (raw_dir / "t.json").write_text(json.dumps(raw), encoding="utf-8")
    _write_list(tmp_path / "grok", [
        {"conversation_id": "g1", "title": "T", "file": "t", "message_count": 1},
    ])
    assert _run_main(monkeypatch, tmp_path) == 0
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(data["messages"]) == 2
    assert "0 conserves" in capsys.readouterr().out


def test_main_rappelle_la_reindexation_rag(tmp_path, monkeypatch, capsys):
    """Fin de run : rappel explicite de `rag_index.py --force` (pas d'auto-index)."""
    _write_export(
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
    out = capsys.readouterr().out
    assert "rag_index.py --force" in out


def test_main_clean_backups_purge_explicite(tmp_path, monkeypatch, capsys):
    """`--clean-backups` : purge les `.json.bak`, jamais automatique."""
    json_path = _write_export(tmp_path, "grok", "t", _grok_conversation())
    _write_list(tmp_path / "grok", [
        {"conversation_id": "g1", "title": "T", "file": "t"},
    ])
    backup = json_path.with_suffix(".json.bak")
    backup.write_text("sauvegarde", encoding="utf-8")

    # sans l'option : la sauvegarde reste (comportement par defaut)
    assert _run_main(monkeypatch, tmp_path) == 0
    assert backup.exists()

    # avec l'option : purge explicite, JSON de conversation intact
    before = json_path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv",
        ["reparse_exports.py", "--exports", str(tmp_path), "--clean-backups"],
    )
    assert reparse.main() == 0
    assert not backup.exists()
    assert json_path.read_text(encoding="utf-8") == before
    assert "1 sauvegarde" in capsys.readouterr().out


def test_main_reecrit_inventaire_au_format_partage(tmp_path, monkeypatch, capsys):
    """L'inventaire repasse par le writer partage : tri + metadonnees."""
    json_path = _write_export(
        tmp_path, "chatgpt", "b",
        {
            "conversation_id": "b",
            "platform": "chatgpt",
            "title": "B",
            "url": "https://chatgpt.com/c/b",
            "last_message_at": "2026-01-02T00:00:00Z",
            "messages": [{"role": "user", "texte": "bonjour"}],
        },
    )
    list_path = tmp_path / "chatgpt" / "conversation_list.json"
    list_path.write_text(
        json.dumps({
            "platform": "chatgpt",
            "updated_at": "2020-01-01T00:00:00Z",
            "count": 99,
            "conversations": [
                {"conversation_id": "a", "title": "A", "file": "a",
                 "last_message_at": "2026-01-03T00:00:00Z"},
                {"conversation_id": "b", "title": "B", "file": "b",
                 "last_message_at": "2026-01-02T00:00:00Z"},
            ],
        }),
        encoding="utf-8",
    )
    assert _run_main(monkeypatch, tmp_path) == 0
    data = json.loads(list_path.read_text(encoding="utf-8"))
    assert data["platform"] == "chatgpt"
    assert data["count"] == 2
    assert data["updated_at"] != "2020-01-01T00:00:00Z"
    assert [e["conversation_id"] for e in data["conversations"]] == ["a", "b"]
    entry = next(e for e in data["conversations"] if e["conversation_id"] == "b")
    assert entry["message_count"] == 1
    assert entry["last_message_at"] == "2026-01-02T00:00:00Z"
    assert json.loads(json_path.read_text(encoding="utf-8"))["messages"][0]["texte"] == "bonjour"
    assert "1 nettoyes" in capsys.readouterr().out
