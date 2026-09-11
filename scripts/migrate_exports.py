#!/usr/bin/env python3
"""Migration one-shot : anciens exports -> exports/<platform>/ (nouveau schema).

Convertit les conversations deja exportees (ancien schema : service/content/
metadata, arborescence exports/<date>/<service>/<id>.json) vers :

    exports/chatgpt/<nom conversation>.json
    exports/chatgpt/conversation_list.json

Les anciens fichiers ne sont pas supprimes (a nettoyer manuellement quand la
migration est verifiee). Le HTML n'existe pas dans l'ancien schema : il sera
produit au prochain `run.py --daily` (20 plus recentes) ou `--monthly`.

Usage:
    .venv/bin/python scripts/migrate_exports.py [--platform chatgpt]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.schema import Conversation, Message  # noqa: E402
from src.utils.file_utils import (  # noqa: E402
    conversation_list_path,
    conversation_paths,
    load_conversation_list,
    now_iso_z,
    read_json,
    save_conversation_list,
    unique_filename,
    write_json_atomic,
)

STATE_FILE = ROOT / ".state" / "state.json"
OUTPUT_DIR = ROOT / "exports"


def legacy_entries(platform: str) -> List[Dict[str, Any]]:
    """Entrees a migrer : union disque (ancien arbre) + .state.

    Le disque est la source la plus complete (certaines conversations ne sont
    pas dans .state) ; l'etat sert de complement pour les fichiers absents.
    """
    merged: Dict[str, Dict[str, Any]] = {}
    for path in sorted(OUTPUT_DIR.glob(f"*/{platform}/*.json")):
        data = read_json(path, default={}) or {}
        cid = str(data.get("conversation_id") or path.stem)
        merged[cid] = {
            "conversation_id": cid,
            "path": str(path),
            "title": data.get("title"),
            "last_message_at": data.get("last_message_at"),
        }
    state = read_json(STATE_FILE, default={}) or {}
    for cid, data in ((state.get("services") or {}).get(platform) or {}).items():
        merged.setdefault(str(cid), {"conversation_id": str(cid), **data})
    return list(merged.values())


def convert_legacy(data: Dict[str, Any], platform: str) -> Conversation:
    messages = []
    for old in data.get("messages") or []:
        if not isinstance(old, dict):
            continue
        meta = old.get("metadata") or {}
        role = old.get("role", "assistant")
        model = meta.get("model") or (data.get("model") if role == "assistant" else None)
        messages.append(
            Message(
                role=role,
                texte=old.get("content", old.get("texte", "")),
                model=model,
                timestamp=old.get("timestamp"),
                metadata=meta,
            )
        )
    conv = Conversation(
        platform=platform,
        conversation_id=str(data.get("conversation_id", "unknown")),
        title=data.get("title") or "",
        messages=messages,
        model=data.get("model"),
        started_at=data.get("started_at"),
        last_message_at=data.get("last_message_at"),
        exported_at=data.get("exported_at") or now_iso_z(),
    )
    return conv


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", default="chatgpt")
    args = parser.parse_args()
    platform = args.platform

    entries = legacy_entries(platform)
    if not entries:
        print(f"[{platform}] aucune conversation existante a migrer")
        return 1

    list_path = conversation_list_path(OUTPUT_DIR, platform)
    index = load_conversation_list(list_path)
    used: Dict[str, str] = {
        e["file"]: cid for cid, e in index.items() if e.get("file")
    }

    migrated = missing = failed = skipped = 0
    for entry in sorted(
        entries, key=lambda e: (e.get("last_message_at") or "", e.get("title") or ""),
        reverse=True,
    ):
        cid = str(entry.get("conversation_id"))
        existing = index.get(cid) or {}
        # deja present dans le nouveau format (scrape reel) : ne pas ecraser
        if existing.get("scraped") and existing.get("file"):
            skipped += 1
            continue
        old_path = entry.get("path")
        data = read_json(old_path, default=None) if old_path else None
        if not isinstance(data, dict):
            print(f"  manquant {cid} ({old_path}) -> ignore")
            missing += 1
            continue
        try:
            conv = convert_legacy(data, platform)
            stem = (index.get(cid) or {}).get("file") or unique_filename(
                conv.title, cid, used
            )
            used.setdefault(stem, cid)
            json_path, _ = conversation_paths(OUTPUT_DIR, platform, stem)
            write_json_atomic(json_path, conv.to_dict(validate=False))
            index[cid] = {
                "conversation_id": cid,
                "title": conv.title,
                "message_count": len(conv.messages),
                "last_message_at": conv.last_message_at,
                "has_code": conv.has_code,
                "scraped": True,
                "file": stem,
                "scraped_at": now_iso_z(),
            }
            migrated += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ECHEC {cid}: {exc}")

    save_conversation_list(list_path, platform, index)
    print(
        f"[{platform}] migration terminee: {migrated} converties, "
        f"{skipped} deja presentes, {missing} fichiers manquants, {failed} echecs"
    )
    print(f"[{platform}] inventaire: {list_path} ({len(index)} entrees)")
    print("Les anciens dossiers exports/<date>/ peuvent etre supprimes apres verification.")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
