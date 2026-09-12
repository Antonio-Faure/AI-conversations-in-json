#!/usr/bin/env python3
"""Genere les medias d'etalonnage (image, audio, video, pdf, csv, txt...) .

Les fichiers sont ecrits dans `<run_dir>/attachments/` (dossier runtime du bot).

    .venv/bin/python scripts/make_media.py --bot gemini
    .venv/bin/python scripts/make_media.py --bot grok --no-av
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.media import generate_media  # noqa: E402

RUN_ROOT = ROOT.parent / "aicv-run"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot", required=True)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--no-av", action="store_true",
                        help="ne generer que les fichiers texte/pdf (pas d'image/audio/video)")
    args = parser.parse_args()

    run_dir = args.run_dir or (RUN_ROOT / args.bot)
    out_dir = run_dir / "attachments"
    files = generate_media(out_dir, with_av=not args.no_av)
    print(f"{len(files)} fichiers generes dans {out_dir}")
    for path in files:
        print(f"  - {path.name} ({path.stat().st_size} o)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
