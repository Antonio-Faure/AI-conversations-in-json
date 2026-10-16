#!/usr/bin/env python3
"""Telecharge les medias « seuls » pointes par des URL dans les messages.

Trois cas traites (best-effort, URL encore valides) :
  - image markdown `![alt](url)` (meme sans extension dans l'URL) ;
  - lien markdown `[nom](url.ext)` quand l'extension annonce un media ;
  - URL nue dans la prose (`... https://site/photo.jpg ...`).

Le fichier est range dans `images/` (images) ou `files/` (audio/video), verifie
avant ecriture (vraie image/document, pas une page HTML) et l'URL est remplacee
par le chemin local : embed `![nom](images/<hash>.<ext>)` pour une image, lien
`[nom](files/<hash>.<ext>)` pour un audio/video. Les liens web restent intacts.

    .venv/bin/python scripts/download_media_links.py            # plan (aucune ecriture)
    .venv/bin/python scripts/download_media_links.py --apply
    .venv/bin/python scripts/download_media_links.py --platform gemini --apply
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
from src.schema import Conversation  # noqa: E402
from src.utils.file_utils import (  # noqa: E402
    CONVERSATION_LIST_NAME,
    resolve_cli_path,
    write_json_atomic,
)
from src.utils.images import (  # noqa: E402
    FILES_SUBDIR,
    IMAGES_SUBDIR,
    download_conversation_media_links,
    http_loader,
)

PLATFORMS = ("chatgpt", "claude", "gemini", "perplexity", "grok", "mistral")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", action="append", choices=PLATFORMS)
    parser.add_argument("--exports", type=Path)
    parser.add_argument(
        "--apply", action="store_true", help="ecrit les fichiers et les JSON"
    )
    args = parser.parse_args()

    config = load_config(ROOT / "config.yaml")
    exports = resolve_cli_path(args.exports or config["output_dir"], ROOT)
    platforms = args.platform or list(PLATFORMS)

    total = {"downloaded": 0, "cached": 0, "failed": 0, "candidates": 0}
    changed = 0
    details: list = []
    for platform in platforms:
        platform_dir = exports / platform
        if not platform_dir.is_dir():
            continue
        for json_path in sorted(platform_dir.glob("*.json")):
            if json_path.name in (CONVERSATION_LIST_NAME, "api_health.json"):
                continue
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                conv = Conversation.from_dict(data, validate=False)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                print(f"[{platform}] {json_path.name}: illisible ({exc})")
                continue
            before = json.dumps(
                data.get("messages"), ensure_ascii=False, sort_keys=True
            )
            stats = download_conversation_media_links(
                conv,
                http_loader,
                platform_dir / IMAGES_SUBDIR,
                platform_dir / FILES_SUBDIR,
                dry_run=not args.apply,
            )
            for key in total:
                total[key] += stats[key]
            for kind, url, relpath in stats.get("files", []):
                details.append((platform, kind, url, relpath))
            after = json.dumps(
                conv.to_dict(validate=False).get("messages"),
                ensure_ascii=False,
                sort_keys=True,
            )
            if args.apply and after != before:
                write_json_atomic(json_path, conv.to_dict(validate=False))
                changed += 1
        print(f"[{platform}] ok")

    mode = "applique" if args.apply else "plan (dry-run)"
    for platform, kind, url, relpath in details:
        print(f"  [{platform}] {kind:5} {relpath}  <- {url[:80]}")
    print(
        f"{mode} : {changed} JSON modifies | "
        f"{total['candidates']} URL media candidates, {total['downloaded']} "
        f"telechargees, {total['cached']} en cache, {total['failed']} echecs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
