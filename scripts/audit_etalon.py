#!/usr/bin/env python3
"""Audit d'integrite des conversations d'etalonnage.

Compare, pour chaque conversation declaree dans scripts/etalons.json :
  - la file des messages envoyes (calibration_queue.json),
  - l'export scrape (tours utilisateur presents).
Sortie non nulle s'il manque des tours.

    .venv/bin/python scripts/audit_etalon.py
    .venv/bin/python scripts/audit_etalon.py --bot mistral-work
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.etalon import queue_coverage  # noqa: E402

RUN_ROOT = Path(os.environ.get("AICV_RUN_DIR") or "/home/odin/Documents/code/aicv-run")


def _queue_texts(label: str) -> List[str]:
    path = RUN_ROOT / label / "calibration_queue.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [m.get("text") or "" for m in data.get("messages") or []]


def _find_export(platform: str, conv_id: str) -> Optional[Path]:
    directory = ROOT / "exports" / platform
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob("*.json")):
        if path.name == "conversation_list.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("conversation_id") == conv_id:
            return path
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot", action="append", help="ne traiter que ce(s) bot(s)")
    parser.add_argument("--config", type=Path, default=Path(__file__).parent / "etalons.json")
    args = parser.parse_args()

    etalons: Dict[str, List[Dict[str, Any]]] = json.loads(
        args.config.read_text(encoding="utf-8")
    )
    problems = 0
    for platform, entries in etalons.items():
        print(f"{platform}:")
        for entry in entries:
            label = entry.get("label") or entry.get("id", "")[:8]
            if args.bot and label not in args.bot:
                continue
            texts = _queue_texts(label)
            path = _find_export(platform, entry.get("id") or "")
            if path is None:
                print(f"  [MANQUE] {label:14} aucun export pour {entry.get('id')}")
                problems += 1
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            seen = [m.get("texte") or "" for m in payload.get("messages") or [] if m.get("role") == "user"]
            coverage = queue_coverage(texts, seen)
            status = "ok" if not coverage["missing"] else "INCOMPLET"
            print(
                f"  [{status:8}] {label:14} export={len(payload.get('messages') or []):<4} "
                f"tours={len(seen)}/{len(texts)} manquants={coverage['missing']}"
            )
            if coverage["missing"]:
                problems += 1
    if problems:
        print(f"{problems} conversation(s) incomplete(s)", file=sys.stderr)
        return 1
    print("tous les etalons sont complets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
