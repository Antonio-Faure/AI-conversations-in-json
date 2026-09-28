#!/usr/bin/env python3
"""Tire une conversation au hasard (pour le jeu de correction).

Affiche la plateforme, le titre, l'URL d'origine et les fichiers locaux
(JSON + HTML) afin de comparer le site d'origine et notre reconstruction.

    .venv/bin/python scripts/random_conversation.py
    .venv/bin/python scripts/random_conversation.py --platform chatgpt
    .venv/bin/python scripts/random_conversation.py --open
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.orchestrator import load_config  # noqa: E402


def _resolve(value) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exports", type=Path)
    parser.add_argument("--platform", action="append")
    parser.add_argument("--open", action="store_true", help="ouvrir l'URL d'origine")
    args = parser.parse_args()

    config = load_config(ROOT / "config.yaml")
    exports = _resolve(args.exports or config["output_dir"])
    platforms = args.platform or sorted(
        d.name for d in exports.iterdir()
        if d.is_dir() and (d / "conversation_list.json").exists()
    )

    candidates = []
    for platform in platforms:
        for json_path in (exports / platform).glob("*.json"):
            if json_path.name == "conversation_list.json":
                continue
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("messages"):
                candidates.append((platform, json_path, data))

    if not candidates:
        print("aucune conversation disponible")
        return 1
    platform, json_path, data = random.choice(candidates)
    html_path = json_path.with_suffix(".html")
    print(f"plateforme   : {platform}")
    print(f"titre        : {data.get('title') or data.get('conversation_id')}")
    print(f"messages     : {len(data.get('messages') or [])}")
    print(f"url origine  : {data.get('url') or '(absente)'}")
    print(f"json local   : {json_path}")
    print(f"html local   : {html_path if html_path.exists() else '(absent)'}")
    if args.open and data.get("url"):
        webbrowser.open(data["url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
