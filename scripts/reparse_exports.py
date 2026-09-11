#!/usr/bin/env python3
"""Regenere les exports a partir des HTML sauvegardes (sans re-crawler).

- plateformes DOM (chatgpt, claude, gemini, mistral, perplexity) : re-parse le
  `.html` voisin avec le parser courant (correctifs LaTeX/code/nettoyage),
  puis fusionne les metadonnees absentes du HTML (timestamps/modele) depuis
  l'ancien JSON (par message_id, sinon par index) ;
- Grok (pas de HTML exploitable) : nettoyage direct du JSON (balises xAI) ;
- toutes : normalisation de l'alternance user/assistant ;
- met a jour conversation_list.json (message_count, has_code, last_message_at).

    .venv/bin/python scripts/reparse_exports.py
    .venv/bin/python scripts/reparse_exports.py --platform gemini
    .venv/bin/python scripts/reparse_exports.py --no-backup
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.orchestrator import load_config  # noqa: E402
from src.parsers import PARSER_CLASSES  # noqa: E402
from src.schema import Conversation, Message, extract_code_blocks, normalize_messages  # noqa: E402
from src.utils.cleanup import clean_text  # noqa: E402

DOM_PLATFORMS = ("chatgpt", "claude", "gemini", "mistral", "perplexity")


def _resolve(value) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _merge_old_metadata(conv: Conversation, old: Dict[str, Any]) -> Conversation:
    old_messages = old.get("messages") or []
    by_id = {
        m.get("message_id"): m for m in old_messages if m.get("message_id")
    }
    for index, message in enumerate(conv.messages):
        ref = by_id.get(message.message_id) if message.message_id else None
        if ref is None and index < len(old_messages):
            ref = old_messages[index]
        if not ref:
            continue
        if not message.timestamp and ref.get("timestamp"):
            message.timestamp = ref["timestamp"]
        if not message.model and ref.get("model"):
            message.model = ref["model"]
        if not message.message_id and ref.get("message_id"):
            message.message_id = ref["message_id"]
    conv.exported_at = old.get("exported_at") or conv.exported_at
    if not conv.model and old.get("model"):
        conv.model = old["model"]
    # les timestamps restaures doivent etre pris en compte pour started/last
    conv.derive_timestamps()
    return conv


def _reparse(platform: str, old: Dict[str, Any], html_path: Path) -> Conversation:
    parser = PARSER_CLASSES[platform]()
    conv = parser.parse(
        html_path.read_text(encoding="utf-8", errors="replace"),
        conversation_id=str(old.get("conversation_id") or ""),
        extra={"title_hint": old.get("title")},
    )
    return _merge_old_metadata(conv, old)


def _clean_json(platform: str, old: Dict[str, Any]) -> Conversation:
    messages = [Message.from_dict(m) for m in old.get("messages") or []]
    for message in messages:
        message.texte = clean_text(message.texte, platform)
        message.code_blocks = extract_code_blocks(message.texte)
    messages = normalize_messages(messages)
    return Conversation(
        platform=platform,
        conversation_id=str(old.get("conversation_id") or ""),
        title=old.get("title") or "",
        messages=messages,
        model=old.get("model"),
        started_at=old.get("started_at"),
        last_message_at=old.get("last_message_at"),
        exported_at=old.get("exported_at"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exports", type=Path)
    parser.add_argument("--platform", action="append")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    config = load_config(ROOT / "config.yaml")
    exports = _resolve(args.exports or config["output_dir"])
    platforms = args.platform or sorted(
        d.name for d in exports.iterdir()
        if d.is_dir() and (d / "conversation_list.json").exists()
    )

    totals = {"reparsed": 0, "cleaned": 0, "skipped": 0, "failed": 0}
    for platform in platforms:
        directory = exports / platform
        list_path = directory / "conversation_list.json"
        index: Dict[str, Dict[str, Any]] = {}
        if list_path.exists():
            data = json.loads(list_path.read_text(encoding="utf-8"))
            index = {
                str(e.get("conversation_id")): e for e in data.get("conversations") or []
            }
        html_stems = {p.stem for p in directory.glob("*.html")}
        for json_path in sorted(directory.glob("*.json")):
            if json_path.name == "conversation_list.json":
                continue
            try:
                old = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                totals["failed"] += 1
                continue
            # metadonnees d'origine (timestamps/model) : la sauvegarde .bak si
            # elle existe (elle precede un eventuel re-parse precedent)
            meta_path = json_path.with_suffix(".json.bak")
            old_meta = old
            if meta_path.exists():
                try:
                    old_meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    old_meta = old
            try:
                if platform in DOM_PLATFORMS and json_path.stem in html_stems:
                    conv = _reparse(platform, old_meta, json_path.with_suffix(".html"))
                    totals["reparsed"] += 1
                else:
                    conv = _clean_json(platform, old_meta)
                    totals["cleaned"] += 1
            except Exception as exc:  # noqa: BLE001
                print(f"[{platform}] ECHEC {json_path.name}: {exc}")
                totals["failed"] += 1
                continue
            if not args.no_backup:
                backup = json_path.with_suffix(".json.bak")
                if not backup.exists():
                    shutil.copy2(json_path, backup)
            json_path.write_text(
                json.dumps(conv.to_dict(validate=False), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            entry = index.get(conv.conversation_id)
            if entry is not None:
                entry["title"] = conv.title or entry.get("title") or ""
                entry["message_count"] = len(conv.messages)
                entry["has_code"] = conv.has_code
                entry["last_message_at"] = conv.last_message_at
        if index and list_path.exists():
            data = json.loads(list_path.read_text(encoding="utf-8"))
            data["conversations"] = sorted(
                index.values(),
                key=lambda e: (e.get("last_message_at") or "", e.get("title") or ""),
                reverse=True,
            )
            data["count"] = len(data["conversations"])
            list_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        print(f"[{platform}] ok")

    print(
        f"termine : {totals['reparsed']} re-parses, {totals['cleaned']} nettoyes, "
        f"{totals['failed']} echecs"
    )
    return 0 if totals["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
