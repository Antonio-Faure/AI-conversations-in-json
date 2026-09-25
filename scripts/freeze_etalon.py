#!/usr/bin/env python3
"""Gele les conversations d'etalonnage en fixtures pour tests offline.

Copie l'export HTML + JSON de chaque conversation declaree dans
tests/fixtures/etalons/<platform>/<label>.{html,json}. Le test
tests/test_etalon_fixtures.py reparse le HTML gele et compare au JSON : une
regression de parseur est detectee sans navigateur.

    .venv/bin/python scripts/freeze_etalon.py
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

FIXTURES = ROOT / "tests" / "fixtures" / "etalons"


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
    parser.add_argument("--config", type=Path, default=Path(__file__).parent / "etalons.json")
    parser.add_argument("--clean", action="store_true", help="vider les fixtures d'abord")
    args = parser.parse_args()

    if args.clean and FIXTURES.exists():
        shutil.rmtree(FIXTURES)
    entries: Dict[str, List[Dict[str, Any]]] = json.loads(
        args.config.read_text(encoding="utf-8")
    )
    frozen = 0
    for platform, etalons in entries.items():
        for etalon in etalons:
            label = etalon.get("label") or (etalon.get("id") or "")[:8]
            export = _find_export(platform, etalon.get("id") or "")
            if export is None:
                print(f"{platform}/{label}: export absent", file=sys.stderr)
                continue
            target_dir = FIXTURES / platform
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(export, target_dir / f"{label}.json")
            html = export.with_suffix(".html")
            if html.exists():
                shutil.copyfile(html, target_dir / f"{label}.html")
            frozen += 1
            print(f"{platform}/{label}: gele")
    print(f"{frozen} conversation(s) gelee(s) dans {FIXTURES.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
