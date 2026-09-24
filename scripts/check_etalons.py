#!/usr/bin/env python3
"""Controle de regression des conversations d'etalonnage.

Lit `scripts/etalons.json` (plateforme -> conversations etalons), retrouve
chaque export dans `exports/<platform>/` par son id, ressort ses metriques et
les compare a la baseline memorisee. Sortie non nulle si une conversation
manque, si l'alternance des roles casse ou si une capacite a chute.

    .venv/bin/python scripts/check_etalons.py
    .venv/bin/python scripts/check_etalons.py --update   # memorise la baseline
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.etalon import (  # noqa: E402
    DETECTABLE,
    FEATURES,
    conversation_metrics,
    detect_capabilities,
    regressions,
)


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


def _format_row(label: str, metrics: Dict[str, int], lost: List[str]) -> str:
    cells = " ".join(f"{key}={metrics.get(key, 0)}" for key in FEATURES)
    status = "DRIFT" if lost else "ok"
    return f"  [{status}] {label:22} {cells}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).parent / "etalons.json")
    parser.add_argument("--output", "-o", type=Path, default=ROOT / "exports")
    parser.add_argument("--update", action="store_true",
                        help="memorise les metriques courantes comme baseline")
    args = parser.parse_args()

    entries: Dict[str, List[Dict[str, Any]]] = json.loads(
        args.config.read_text(encoding="utf-8")
    )
    problems = 0
    for platform, etalons in entries.items():
        print(f"{platform}:")
        for etalon in etalons:
            conv_id = etalon.get("id") or ""
            label = etalon.get("label") or conv_id[:8]
            path = _find_export(args.output, platform, conv_id)
            if path is None:
                print(f"  [MANQUE] {label:22} aucun export pour {conv_id}")
                problems += 1
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            metrics = conversation_metrics(payload)
            lost = regressions(metrics, etalon.get("baseline") or {})
            print(_format_row(label, metrics, lost))
            if metrics["messages"] == 0 or metrics["consecutive_roles"] > 0:
                problems += 1
            if lost:
                problems += 1
            expected = set(etalon.get("expected_capabilities") or [])
            if expected:
                detected = detect_capabilities(payload)
                verifiable = expected & DETECTABLE
                missing = sorted(verifiable - detected)
                if missing:
                    print(f"    capacites absentes: {', '.join(missing)}")
                    problems += 1
                unchecked = sorted(expected - DETECTABLE)
                if unchecked:
                    print(f"    non verifiables auto ({len(unchecked)}): {', '.join(unchecked)}")
            if args.update:
                etalon["baseline"] = metrics
                if expected:
                    etalon["detected_capabilities"] = sorted(detect_capabilities(payload))
    if args.update:
        args.config.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"baseline mise a jour: {args.config}")
        return 0
    if problems:
        print(f"{problems} probleme(s) detecte(s)", file=sys.stderr)
        return 1
    print("tous les etalons sont couverts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
