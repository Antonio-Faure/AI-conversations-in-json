#!/usr/bin/env python3
"""Gele les conversations d'etalonnage en fixtures pour tests offline.

Copie l'artefact brut + le JSON de chaque conversation declaree dans
`tests/fixtures/etalons/<platform>/<label>.*` :

- plateformes API (chatgpt, claude, gemini, grok, mistral, perplexity) : payload
  brut `raw/<stem>.json` gele en `<label>.raw.json` (plus de HTML : la migration
  JSON-only a supprime l'artefact DOM) ;
- repli DOM (plateforme non migree ou sans payload) : page `.html` gelee en
  `<label>.html`.

Le test `tests/test_etalon_fixtures.py` reparse l'artefact gele (payload API
via `extra={"api": payload}` ou HTML) et compare au JSON : une regression de
parseur est detectee sans navigateur.

    .venv/bin/python scripts/freeze_etalon.py
    .venv/bin/python scripts/freeze_etalon.py --output /chemin/exports
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.etalon import etalons_file, load_etalons  # noqa: E402
from src.utils.file_utils import write_text_atomic  # noqa: E402
from scripts.sanitize import sanitize_text  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "etalons"

#: plateformes API : l'artefact gele est le payload brut (`raw/<stem>.json`)
API_PLATFORMS = ("chatgpt", "claude", "gemini", "grok", "mistral", "perplexity")


def _find_export(output_dir: Path, platform: str, conv_id: str) -> Optional[Path]:
    directory = output_dir / platform
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
    parser.add_argument("--config", type=Path, default=None,
                        help="defaut: scripts/etalons.json (sinon l'exemple)")
    parser.add_argument("--output", "-o", type=Path, default=ROOT / "exports",
                        help="dossier des exports (defaut: exports/ du depot)")
    parser.add_argument("--clean", action="store_true", help="vider les fixtures d'abord")
    args = parser.parse_args()

    config_path = etalons_file(args.config)
    if config_path.name.endswith(".example.json"):
        print(f"note: config locale absente, exemple utilise ({config_path.name})",
              file=sys.stderr)
    if args.clean and FIXTURES.exists():
        shutil.rmtree(FIXTURES)
    entries: Dict[str, List[Dict[str, Any]]] = load_etalons(config_path)
    frozen = 0
    for platform, etalons in entries.items():
        for etalon in etalons:
            label = etalon.get("label") or (etalon.get("id") or "")[:8]
            export = _find_export(args.output, platform, etalon.get("id") or "")
            if export is None:
                print(f"{platform}/{label}: export absent", file=sys.stderr)
                continue
            target_dir = FIXTURES / platform
            target_dir.mkdir(parents=True, exist_ok=True)
            copied = [target_dir / f"{label}.json"]
            shutil.copyfile(export, copied[0])
            if platform in API_PLATFORMS:
                # artefact brut API : raw/<stem>.json (remplace le HTML)
                raw = export.parent / "raw" / f"{export.stem}.json"
                if raw.exists():
                    copied.append(target_dir / f"{label}.raw.json")
                    shutil.copyfile(raw, copied[-1])
                else:
                    print(f"{platform}/{label}: payload brut absent "
                          f"({raw.relative_to(args.output)})", file=sys.stderr)
            else:
                html = export.with_suffix(".html")
                if html.exists():
                    copied.append(target_dir / f"{label}.html")
                    shutil.copyfile(html, copied[-1])
            # les fixtures gelees ne doivent contenir aucune trace personnelle
            for target in copied:
                text = target.read_text(encoding="utf-8", errors="replace")
                write_text_atomic(target, sanitize_text(text))
            frozen += 1
            print(f"{platform}/{label}: gele")
    print(f"{frozen} conversation(s) gelee(s) dans {FIXTURES.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
