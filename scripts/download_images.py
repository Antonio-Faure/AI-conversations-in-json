#!/usr/bin/env python3
"""Telecharge les images de contenu des exports deja existants (best-effort).

Les URL signees/expirantes (ChatGPT `estuary`, Perplexity S3) ne sont
telechargeables que pendant le run de scraping (session authentifiee) : ce
script traite les URL encore valides (Gemini googleusercontent/gstatic, etc.)
et reecrit le markdown vers `images/<hash>.<ext>`.

    .venv/bin/python scripts/download_images.py
    .venv/bin/python scripts/download_images.py --platform gemini --platform perplexity
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
from src.utils.file_utils import write_json_atomic  # noqa: E402
from src.utils.images import IMAGES_SUBDIR, http_loader  # noqa: E402
from src.utils.images import download_conversation_images  # noqa: E402

PLATFORMS = ("chatgpt", "claude", "gemini", "perplexity", "grok", "mistral")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", action="append", choices=PLATFORMS)
    parser.add_argument("--exports", type=Path)
    args = parser.parse_args()

    config = load_config(ROOT / "config.yaml")
    exports = args.exports or Path(config["output_dir"])
    if not exports.is_absolute():
        exports = ROOT / exports
    platforms = args.platform or list(PLATFORMS)

    total = {"downloaded": 0, "cached": 0, "failed": 0}
    changed = 0
    for platform in platforms:
        platform_dir = exports / platform
        if not platform_dir.is_dir():
            continue
        for json_path in sorted(platform_dir.glob("*.json")):
            if json_path.name == "conversation_list.json":
                continue
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                conv = Conversation.from_dict(data, validate=False)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                print(f"[{platform}] {json_path.name}: illisible ({exc})")
                continue
            before = json.dumps(data.get("messages"), ensure_ascii=False, sort_keys=True)
            stats = download_conversation_images(
                conv, http_loader, platform_dir / IMAGES_SUBDIR
            )
            for key in total:
                total[key] += stats[key]
            after = json.dumps(conv.to_dict(validate=False).get("messages"),
                               ensure_ascii=False, sort_keys=True)
            if after != before:
                write_json_atomic(json_path, conv.to_dict(validate=False))
                changed += 1
        print(f"[{platform}] ok")

    print(
        f"termine : {changed} JSON modifies | "
        f"{total['downloaded']} telechargees, {total['cached']} en cache, "
        f"{total['failed']} echecs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
