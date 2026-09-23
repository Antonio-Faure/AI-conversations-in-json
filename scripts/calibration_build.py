#!/usr/bin/env python3
"""Construit les conversations d'etalonnage compactees (offline).

Pour chaque bot, on recupere la suite de tests que le chatbot a generee
lui-meme, on la tague par capacite (calibration/manifest.json) et on garde le
plus petit ensemble couvrant tout. Sortie : calibration/<bot>.json + un rapport.

    .venv/bin/python scripts/calibration_build.py
    .venv/bin/python scripts/calibration_build.py --bot gemini --report
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils.calibration import (  # noqa: E402
    build_payload,
    bootstraps_from_export,
    extract_tests,
    load_manifest,
    suite_from_path,
)

CAL_DIR = ROOT / "calibration"
MANIFEST_PATH = CAL_DIR / "manifest.json"

#: conversation « documentation » par bot (suite generee par le chatbot)
SOURCES: Dict[str, Dict[str, Any]] = {
    "gemini": {
        "kind": "json",
        "path": "exports/gemini/documentation-des-capacites-de-gemini.json",
    },
    "grok": {
        "kind": "json",
        "path": "exports/grok/documentation-capabilities-data-tools-memory.json",
    },
    "perplexity": {
        "kind": "json",
        "path": "exports/perplexity/dresse-une-documentation-complete-et-structuree-de-tout-ce-que-tu-peux-faire-dan.json",
    },
    "mistral-work": {
        "kind": "json",
        "path": "exports/mistral/capacites-et-fonctionnalites-detaillees.json",
        "platform": "mistral",
        "mode": "work",
    },
    "mistral-chat": {
        "kind": "html",
        "path": "exports/mistral/capacites-et-fonctionnalites.html",
        "platform": "mistral",
        "mode": "chat",
        "json": "exports/mistral/capacites-et-fonctionnalites.json",
    },
}

#: bots dont la suite reste a generer (envoyer les amorces via browser-use)
PENDING = {
    "chatgpt": "exports/chatgpt/documentation-des-capacites.json",
    "claude": None,
}

#: conversation de reference pour extraire les deux amorces
BOOTSTRAP_SOURCE = SOURCES["perplexity"]["path"]


def _conversation_ref(spec: Dict[str, Any]) -> Optional[str]:
    json_path = ROOT / (spec.get("json") or spec["path"])
    if json_path.suffix != ".json" or not json_path.exists():
        return None
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload.get("url") or payload.get("conversation_id")


def _build_bot(name: str, spec: Dict[str, Any], manifest: Dict[str, Any]) -> Dict[str, Any]:
    suite = suite_from_path(spec["kind"], ROOT / spec["path"])
    tests = extract_tests(suite or "")
    platform = spec.get("platform") or name
    return build_payload(
        platform=platform,
        tests=tests,
        manifest=manifest,
        mode=spec.get("mode"),
        source_conversation=_conversation_ref(spec),
        generated_from=Path(spec["path"]).name,
    )


def _pending_payload(name: str, manifest: Dict[str, Any], path: Optional[str]) -> Dict[str, Any]:
    all_ids = [c["id"] for c in manifest.get("capabilities") or []]
    return {
        "platform": name,
        "status": "suite_a_generer",
        "source_conversation": _conversation_ref({"path": path}) if path else None,
        "messages": [],
        "capabilities_covered": [],
        "capabilities_missing": all_ids,
        "note": "Envoyer les amorces (calibration/bootstraps.json) puis relancer calibration_build.py.",
    }


def _write(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_bootstraps() -> Path:
    payload = json.loads((ROOT / BOOTSTRAP_SOURCE).read_text(encoding="utf-8"))
    boot = bootstraps_from_export(payload)
    boot["pending_bots"] = sorted(PENDING)
    boot["note"] = (
        "Deux amorces a envoyer une fois a chaque bot sans suite : documenter les "
        "capacites, puis generer la suite de tests."
    )
    path = CAL_DIR / "bootstraps.json"
    _write(path, boot)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot", action="append", help="ne traiter que ce(s) bot(s)")
    parser.add_argument("--report", action="store_true", help="afficher la couverture")
    args = parser.parse_args()

    manifest = load_manifest(MANIFEST_PATH)
    names = args.bot or list(SOURCES)
    CAL_DIR.mkdir(parents=True, exist_ok=True)

    for name in names:
        spec = SOURCES.get(name)
        if spec is None:
            print(f"{name}: source inconnue", file=sys.stderr)
            continue
        payload = _build_bot(name, spec, manifest)
        _write(CAL_DIR / f"{name}.json", payload)
        if args.report:
            covered = len(payload["capabilities_covered"])
            total = covered + len(payload["capabilities_missing"])
            print(
                f"{name:14} pool={payload['test_pool']:<4} retenus={len(payload['messages']):<3} "
                f"couverture={covered}/{total} "
                f"manquants={','.join(payload['capabilities_missing']) or '-'}"
            )

    if not args.bot:
        for name, path in PENDING.items():
            _write(CAL_DIR / f"{name}.json", _pending_payload(name, manifest, path))
        boot_path = _write_bootstraps()
        if args.report:
            print(f"bootstraps ecrits: {boot_path.relative_to(ROOT)} (bots: {', '.join(sorted(PENDING))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
