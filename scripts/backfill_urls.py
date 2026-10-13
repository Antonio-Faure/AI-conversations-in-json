#!/usr/bin/env python3
"""Renseigne le champ `url` des conversations (JSON + conversation_list.json).

Sans re-crawler : URL reconstruite par plateforme, et lue depuis le
`<link rel="canonical">` du HTML sauvegarde quand il est disponible (Mistral
permet ainsi de distinguer /chat de /work ; les URLs /chat sont desormais
reecrites en /work par `normalize_conversation_url`).

    .venv/bin/python scripts/backfill_urls.py
    .venv/bin/python scripts/backfill_urls.py --platform mistral
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.orchestrator import load_config  # noqa: E402
from src.utils.file_utils import resolve_cli_path, write_json_atomic  # noqa: E402
from src.utils.urls import (  # noqa: E402
    canonical_url_from_html,
    conversation_url,
    normalize_conversation_url,
)


def _url_for(platform: str, conversation_id: str, html_path: Path) -> str:
    if html_path.exists():
        try:
            canonical = canonical_url_from_html(
                html_path.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            canonical = None
        if canonical and conversation_id in canonical:
            return normalize_conversation_url(canonical)
    return normalize_conversation_url(conversation_url(platform, conversation_id))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exports", type=Path)
    parser.add_argument("--platform", action="append")
    args = parser.parse_args()

    config = load_config(ROOT / "config.yaml")
    exports = resolve_cli_path(args.exports or config["output_dir"], ROOT)
    platforms = args.platform or sorted(
        d.name for d in exports.iterdir()
        if d.is_dir() and (d / "conversation_list.json").exists()
    )

    total = 0
    for platform in platforms:
        directory = exports / platform
        list_path = directory / "conversation_list.json"
        index = {}
        if list_path.exists():
            data = json.loads(list_path.read_text(encoding="utf-8"))
            index = {
                str(e.get("conversation_id")): e for e in data.get("conversations") or []
            }
        for json_path in sorted(directory.glob("*.json")):
            if json_path.name == "conversation_list.json":
                continue
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            conversation_id = str(payload.get("conversation_id") or "")
            if not conversation_id:
                continue
            url = payload.get("url") or _url_for(
                platform, conversation_id, json_path.with_suffix(".html")
            )
            if payload.get("url") != url:
                payload["url"] = url
                write_json_atomic(json_path, payload)
                total += 1
            entry = index.get(conversation_id)
            if entry is not None:
                entry["url"] = url
        if index and list_path.exists():
            data = json.loads(list_path.read_text(encoding="utf-8"))
            data["conversations"] = list(index.values())
            write_json_atomic(list_path, data)
        print(f"[{platform}] ok")

    print(f"termine : {total} URLs ajoutees/mises a jour")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
