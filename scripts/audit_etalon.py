#!/usr/bin/env python3
"""Audit d'integrite des conversations d'etalonnage (union par plateforme).

Pour chaque plateforme, verifie que l'**union** des conversations declarees
couvre la file envoyee (tours utilisateur retrouves). Une plateforme peut avoir
plusieurs conversations (complements) : c'est l'union qui doit etre complete.

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


def _export_users(platform: str, conv_id: str) -> Optional[List[str]]:
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
            return [m.get("texte") or "" for m in payload.get("messages") or []
                    if m.get("role") == "user"]
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
        seen_union: List[str] = []
        for entry in entries:
            seen = _export_users(platform, entry.get("id") or "")
            if seen is not None:
                seen_union.extend(seen)
        print(f"{platform}:")
        for entry in entries:
            label = entry.get("label") or entry.get("id", "")[:8]
            if args.bot and label not in args.bot:
                continue
            queue = _queue_texts(label)
            seen = _export_users(platform, entry.get("id") or "")
            if seen is None:
                print(f"  [MANQUE] {label:18} aucun export pour {entry.get('id')}")
                problems += 1
                continue
            # couverture de la file de cette conversation par l'union plateforme
            coverage = queue_coverage(queue, seen_union)
            status = "ok" if not coverage["missing"] else "INCOMPLET"
            print(
                f"  [{status:8}] {label:18} tours={len(seen)}/{len(queue)} "
                f"manquants(union)={coverage['missing']}"
            )
            if coverage["missing"]:
                problems += 1
    if problems:
        print(f"{problems} conversation(s) incomplete(s)", file=sys.stderr)
        return 1
    print("tous les etalons sont complets (union par plateforme)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
